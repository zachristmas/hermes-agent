"""Failure-injection tests for kanban lifecycle enforcement (hermes_cli/kanban_settlement.py).

Everything runs on a temporary HERMES_HOME database; no live board is touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_settlement as ks


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    c = kbc.connect()
    yield c
    c.close()


def _running(conn, title="t", claimer="worker"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer=claimer) is not None
    return tid, kb._current_run_id(conn, tid)


def _events(conn, tid, kind):
    return conn.execute("SELECT payload, run_id FROM task_events WHERE task_id=? AND kind=?", (tid, kind)).fetchall()


def _row(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def _ambiguous_owner(conn, tid, run_id, tmp_path):
    """An owner receipt whose ledger is unavailable: owner_reclaim_paused must fail closed."""
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "owner_admitted", {
            "db": str(tmp_path / "missing-owner-ledger.db"), "session_id": "s", "request_id": "r",
            "pid": 424242, "birth": 1.0}, run_id=run_id)


def _expire(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 10, tid))


def _never_spawn(calls):
    def spawn(task, workspace, board=None):
        calls.append(task.id)
    return spawn


# --- 1. unknown owner: durable hold, no requeue, no spawn, no counter ---------------------------

def test_owner_ledger_unavailable_holds_without_duplicate_spawn(conn, tmp_path):
    tid, run_id = _running(conn)
    _ambiguous_owner(conn, tid, run_id, tmp_path)
    _expire(conn, tid)
    spawned: list = []
    for _ in range(3):  # several sweeps, spawning disabled like the real pause
        kbd.dispatch_once(conn, max_spawn=0, spawn_fn=_never_spawn(spawned))
    row = _row(conn, tid)
    assert row["status"] == "running" and row["claim_lock"] == "worker"
    assert row["consecutive_failures"] == 0 and spawned == []
    assert kb._current_run_id(conn, tid) == run_id  # the run was not terminalized or replaced
    holds = _events(conn, tid, "recovery_hold")
    assert len(holds) == 1 and holds[0]["run_id"] == run_id
    payload = json.loads(holds[0]["payload"])
    assert payload["reason"] and "native session s" in payload["owner"] and payload["resolution"]


def test_hold_escalates_once_and_operator_resolution_requires_evidence(conn, tmp_path):
    tid, run_id = _running(conn)
    _ambiguous_owner(conn, tid, run_id, tmp_path)
    _expire(conn, tid)
    kb.release_stale_claims(conn)
    first_seen = json.loads(_events(conn, tid, "recovery_hold")[0]["payload"])["first_seen"]
    late = first_seen + ks.OWNER_HOLD_ESCALATE_SECONDS + 1
    assert ks.record_recovery_hold(conn, tid, reason="x", now=late)["escalated"] is True
    assert ks.record_recovery_hold(conn, tid, reason="x", now=late + 5)["escalated"] is False
    assert len(_events(conn, tid, "recovery_hold_escalated")) == 1
    assert not ks.resolve_recovery_hold(conn, tid, decision="owner_stopped", actor="zach", evidence="  ")
    assert _row(conn, tid)["status"] == "running"
    assert ks.resolve_recovery_hold(conn, tid, decision="owner_stopped", actor="zach", evidence="pid gone, session ended")
    row = _row(conn, tid)
    assert row["status"] == "ready" and row["consecutive_failures"] == 0
    assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()["outcome"] == "reclaimed"


# --- 2. coordinator death after worker result, lost notification, restart ----------------------

def _artifact(tmp_path, name="report.md", text="x" * 300):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_result_receipt_settles_card_when_coordinator_dies_before_settlement(conn, tmp_path):
    tid, run_id = _running(conn)
    art = _artifact(tmp_path)
    assert ks.record_result_receipt(conn, tid, run_id, success=True, report="done, see " + art, artifacts=[art])
    assert _row(conn, tid)["status"] == "running"  # the coordinator died here
    spawned: list = []
    kbd.dispatch_once(conn, max_spawn=0, spawn_fn=_never_spawn(spawned))  # admission disabled
    assert _row(conn, tid)["status"] == "review" and spawned == []
    assert len(_events(conn, tid, "settled_from_receipt")) == 1
    kbd.dispatch_once(conn, max_spawn=0)  # idempotent on the next tick
    assert len(_events(conn, tid, "settled_from_receipt")) == 1
    assert not ks.record_result_receipt(conn, tid, run_id, success=True, report="again")  # run-scoped, once


def test_lost_notifier_and_dispatcher_restart_keep_the_follow_up(conn, kanban_home, tmp_path):
    from hermes_cli import kanban_db_notify as notify
    tid, run_id = _running(conn)
    notify.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="c1")
    art = _artifact(tmp_path)
    ks.record_result_receipt(conn, tid, run_id, success=True, report="ok", artifacts=[art])
    conn.close()  # dispatcher restart: a brand-new connection does the settlement
    fresh = kbc.connect()
    try:
        kbd.dispatch_once(fresh, max_spawn=0)
        _cursor, events = notify.unseen_events_for_sub(fresh, task_id=tid, platform="discord", chat_id="c1")
        kinds = [e.kind for e in events]  # nothing delivered and the cursor never advanced
        assert "review_requested" in kinds and "settled_from_receipt" in kinds
    finally:
        fresh.close()


def test_failed_result_blocks_with_the_reported_kind(conn):
    tid, run_id = _running(conn)
    ks.record_result_receipt(conn, tid, run_id, success=False, report="needs a decision", block_kind="needs_input")
    assert ks.reconcile_settlements(conn) == [tid]
    row = _row(conn, tid)
    assert row["status"] == "blocked" and row["block_kind"] == "needs_input"


def test_settlement_error_budget_becomes_a_visible_outcome(conn, monkeypatch):
    tid, run_id = _running(conn)
    ks.record_result_receipt(conn, tid, run_id, success=True, report="ok", artifacts=[])
    monkeypatch.setattr(kb, "request_review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db busy")))
    for _ in range(ks.SETTLE_ERROR_BUDGET + 2):
        ks.reconcile_settlements(conn)
    assert _row(conn, tid)["status"] == "running"
    assert len(_events(conn, tid, "settlement_error")) >= ks.SETTLE_ERROR_BUDGET
    assert len(_events(conn, tid, "settlement_escalated")) == 1


# --- 3. stale claimant cannot settle a newer run -------------------------------------------------

def test_stale_claimant_cannot_settle_newer_run(conn, tmp_path):
    tid, old_run = _running(conn)
    _expire(conn, tid)
    kb.release_stale_claims(conn)  # no owner receipt: normal reclaim
    assert kb.claim_task(conn, tid, claimer="worker2") is not None
    new_run = kb._current_run_id(conn, tid)
    assert new_run != old_run
    ok, _reason = kb.request_review(conn, tid, summary="late", expected_run_id=old_run, with_reason=True)
    assert not ok and not kb.complete_task(conn, tid, summary="late", expected_run_id=old_run)
    assert _row(conn, tid)["status"] == "running" and _row(conn, tid)["claim_lock"] == "worker2"
    # a receipt for the old run is ignored when settling the current one
    ks.record_result_receipt(conn, tid, old_run, success=True, report="stale")
    assert ks.settle_from_receipt(conn, tid) is None and _row(conn, tid)["status"] == "running"


# --- 4. typed operator pause, outside counters, sticky, restart-safe ---------------------------

def test_repeated_operator_pause_never_trips_counters_or_triage(conn):
    tid, _run = _running(conn)
    for _ in range(4):
        assert ks.operator_pause(conn, tid, reason="reboot", actor="zach", resume_condition="after reboot") in {"paused", "already_paused"}
        row = _row(conn, tid)
        assert row["status"] == "blocked" and row["block_kind"] == "operator_pause"
        assert row["block_recurrences"] == 0 and row["consecutive_failures"] == 0
        kbd.dispatch_once(conn, max_spawn=0)  # sweep + recompute_ready must not auto-resume it
        assert _row(conn, tid)["status"] == "blocked"
        assert ks.operator_resume(conn, tid, actor="zach")
        assert _row(conn, tid)["status"] == "ready"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
    assert not _events(conn, tid, "block_loop_detected")


def test_pause_waits_for_owner_evidence_and_survives_restart(conn, kanban_home, tmp_path):
    tid, run_id = _running(conn)
    _ambiguous_owner(conn, tid, run_id, tmp_path)
    assert ks.operator_pause(conn, tid, reason="stop", actor="zach", resume_condition="r") == "pending_owner_evidence"
    conn.close()
    fresh = kbc.connect()
    try:
        assert ks.reconcile_pending_pauses(fresh) == []  # owner still unknown: nothing is closed
        row = _row(fresh, tid)
        assert row["status"] == "running" and kb._current_run_id(fresh, tid) == run_id
        assert len(_events(fresh, tid, "operator_pause_pending")) == 1
    finally:
        fresh.close()


# --- 5. declared artifact contract at the shared Review/Done boundary --------------------------

def _contract(conn, tid):
    ks.declare_artifact_contract(conn, tid, [{"name": "report.md", "min_bytes": 200, "must_contain": ["## Findings"]}])


def test_partial_or_prose_only_deliverable_cannot_satisfy_review(conn, tmp_path):
    tid, run_id = _running(conn)
    _contract(conn, tid)
    partial = _artifact(tmp_path, text="## Findings\nonly a start")
    ok, reason = kb.request_review(conn, tid, summary=f"complete, see {partial}", metadata={"artifacts": [partial]},
                                   expected_run_id=run_id, with_reason=True)
    assert not ok and reason.startswith(ks.UNMET_PREFIX) and "bytes" in reason
    ok, reason = kb.request_review(conn, tid, summary=f"complete, see {partial}", expected_run_id=run_id, with_reason=True)
    assert not ok and reason.startswith(ks.UNMET_PREFIX)  # a prose mention alone is not identity
    assert not kb.complete_task(conn, tid, summary="done", expected_run_id=run_id)
    assert _row(conn, tid)["status"] == "running"
    assert _events(conn, tid, "completion_refused_unmet_contract")


def test_complete_deliverable_enters_review_and_partial_settles_truthfully(conn, tmp_path):
    tid, run_id = _running(conn)
    _contract(conn, tid)
    ws = Path(kb._task_workspace_for_review(conn, tid))  # Review verifies deliverables inside the task workspace
    ws.mkdir(parents=True, exist_ok=True)
    full = _artifact(ws, text="## Findings\n" + "detail " * 60)
    ok, _ = kb.request_review(conn, tid, summary="ok", metadata={"artifacts": [full]}, expected_run_id=run_id, with_reason=True)
    assert ok and _row(conn, tid)["status"] == "review"

    tid2, run2 = _running(conn, title="p")
    _contract(conn, tid2)
    assert ks.settle_partial(conn, tid2, run2, summary="half done", unmet=["report.md: 20 bytes < required 200"]) == "partial"
    row = _row(conn, tid2)
    assert row["status"] == "ready" and row["consecutive_failures"] == 1  # retry stays bounded
    detail = json.loads(_events(conn, tid2, "partial_result_detail")[0]["payload"])
    assert detail["unmet"] == ["report.md: 20 bytes < required 200"] and detail["next_owner"] == "worker"
    assert "half done" in detail["partial_summary"]
    assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run2,)).fetchone()["outcome"] == "partial_result"


def test_task_without_contract_is_unchanged(conn, tmp_path):
    tid, run_id = _running(conn)
    ok, _ = kb.request_review(conn, tid, summary="investigation notes", expected_run_id=run_id, with_reason=True)
    assert ok and _row(conn, tid)["status"] == "review"


# --- 6. trusted-human Done on review-required boards --------------------------------------------

@pytest.fixture
def review_board(monkeypatch):
    verifier = ks.make_verifier("correct horse")
    monkeypatch.setattr(ks, "_acceptance_config", lambda: {
        "boards": [kb.get_current_board()], "verifier": verifier})
    return verifier


def test_unauthorized_writers_cannot_produce_done(conn, review_board):
    tid, run_id = _running(conn)
    assert not kb.complete_task(conn, tid, summary="worker says done", expected_run_id=run_id)
    assert not kb.complete_task(conn, tid, summary="alternate writer", force=True)
    assert _row(conn, tid)["status"] == "running"
    assert kb.request_review(conn, tid, summary="ready for review", expected_run_id=run_id)
    assert not kb.complete_task(conn, tid, summary="no proof")  # reviewer without acceptance
    assert not kb.complete_task(conn, tid, summary="forged", human_acceptance=SimpleNamespace(principal="zach", source="x"))
    assert not kb.complete_task(conn, tid, summary="forced", force=True)
    assert _row(conn, tid)["status"] == "review"
    assert len(_events(conn, tid, "completion_refused_human_acceptance")) >= 3
    with pytest.raises(PermissionError):
        ks.HumanAcceptance("zach", "forged")
    assert ks.dashboard_acceptance() is None  # no request-derived proof: fail closed


def test_trusted_human_acceptance_still_works(conn, review_board):
    tid, run_id = _running(conn)
    kb.request_review(conn, tid, summary="ready", expected_run_id=run_id)
    assert ks.authenticate_passphrase("wrong", review_board) is None
    token = ks.authenticate_passphrase("correct horse", review_board, principal="zach")
    assert token is not None and kb.complete_task(conn, tid, summary="approved", human_acceptance=token)
    assert _row(conn, tid)["status"] == "done"


def test_dashboard_acceptance_needs_request_derived_proof_and_ignores_the_old_global_flag(conn, monkeypatch, review_board):
    monkeypatch.setattr(ks, "_acceptance_config", lambda: {
        "boards": [kb.get_current_board()], "verifier": review_board, "dashboard_session_trusted": True})
    tid, run_id = _running(conn)
    kb.request_review(conn, tid, summary="ready", expected_run_id=run_id)
    assert ks.dashboard_acceptance() is None  # the flag alone proves nothing
    assert ks.dashboard_acceptance(passphrase="wrong") is None
    assert not kb.complete_task(conn, tid, summary="x", force=True, human_acceptance=ks.dashboard_acceptance())
    assert kb.complete_task(conn, tid, summary="approved", force=True,
                            human_acceptance=ks.dashboard_acceptance(passphrase="correct horse"))


def test_dashboard_gated_session_issues_acceptance(conn, review_board):
    tid, run_id = _running(conn)
    kb.request_review(conn, tid, summary="ready", expected_run_id=run_id)
    assert kb.complete_task(conn, tid, summary="approved", force=True,
                            human_acceptance=ks.dashboard_acceptance(gated_session=True))


def test_unconfigured_board_keeps_existing_done_behavior(conn):
    tid, run_id = _running(conn)
    assert kb.complete_task(conn, tid, summary="done", expected_run_id=run_id)
    assert _row(conn, tid)["status"] == "done"


def test_unreadable_policy_fails_closed(conn, monkeypatch):
    def boom():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(ks, "_acceptance_config", boom)
    tid, run_id = _running(conn)
    assert not kb.complete_task(conn, tid, summary="done", expected_run_id=run_id)


# --- settle_partial: replayable intent across a crash between its two transactions ---------------

def test_crash_between_partial_intent_and_settle_is_completed_by_restart_sweep(conn, kanban_home, monkeypatch):
    tid, run_id = _running(conn)
    real = kbd._record_task_failure

    def crash(*_a, **_k):
        raise RuntimeError("process died between the intent and the settle")
    monkeypatch.setattr(kbd, "_record_task_failure", crash)
    with pytest.raises(RuntimeError):
        ks.settle_partial(conn, tid, run_id, summary="half", unmet=["report.md: missing"])
    # Crash window state: run still open, intent durable, nothing half-applied.
    assert _row(conn, tid)["status"] == "running" and _row(conn, tid)["current_run_id"] == run_id
    assert len(_events(conn, tid, "partial_settle_intent")) == 1
    assert conn.execute("SELECT ended_at FROM task_runs WHERE id=?", (run_id,)).fetchone()["ended_at"] is None
    monkeypatch.setattr(kbd, "_record_task_failure", real)
    conn.close()
    fresh = kbc.connect()  # a restarted dispatcher process
    try:
        spawned: list = []
        kbd.dispatch_once(fresh, max_spawn=0, spawn_fn=_never_spawn(spawned))  # admission stays disabled
        row = _row(fresh, tid)
        assert row["status"] == "ready" and row["claim_lock"] is None and row["current_run_id"] is None
        assert fresh.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()["outcome"] == "partial_result"
        detail = json.loads(_events(fresh, tid, "partial_result_detail")[0]["payload"])
        assert detail["next_owner"] == "worker" and "report.md" in detail["unmet"][0]
        assert not spawned
        before = fresh.execute("SELECT COUNT(*) c FROM task_events WHERE task_id=?", (tid,)).fetchone()["c"]
        kbd.dispatch_once(fresh, max_spawn=0, spawn_fn=_never_spawn(spawned))  # replay is idempotent
        assert fresh.execute("SELECT COUNT(*) c FROM task_events WHERE task_id=?", (tid,)).fetchone()["c"] == before
    finally:
        fresh.close()


def test_partial_settle_at_failure_limit_blocks_and_closes_the_run_together(conn):
    tid, run_id = _running(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET consecutive_failures = ? WHERE id=?", (kbd.DEFAULT_FAILURE_LIMIT - 1, tid))
    assert ks.settle_partial(conn, tid, run_id, summary="half", unmet=["x"]) == "partial"
    row = _row(conn, tid)
    assert row["status"] == "blocked" and row["claim_lock"] is None and row["current_run_id"] is None
    run = conn.execute("SELECT status, ended_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert run["ended_at"] is not None
    assert len(_events(conn, tid, "gave_up")) == 1


def test_stale_partial_settle_for_an_older_run_writes_nothing(conn):
    tid, run_id = _running(conn)
    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome="reclaimed")
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="other") is not None
    newer = kb._current_run_id(conn, tid)
    assert newer != run_id
    assert ks.settle_partial(conn, tid, run_id, summary="old", unmet=["x"]) == "stale"
    assert _row(conn, tid)["status"] == "running" and _row(conn, tid)["current_run_id"] == newer
    assert not _events(conn, tid, "partial_settle_intent")


# --- alternate Done writers: swarm root (system-only) --------------------------------------------

def test_swarm_root_inline_done_is_system_only_and_cannot_flip_real_work(conn):
    from hermes_cli import kanban_swarm as sw
    tid, _run = _running(conn)  # has an attempt: real work
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
        assert sw._activate_root_inline(conn, tid, summary="x", metadata={}) is False
    assert _row(conn, tid)["status"] == "blocked"


# --- the gate binds to the CONNECTION's board, never the ambient one ------------------------------

@pytest.fixture
def prot_board(kanban_home, monkeypatch):
    kb.create_board("prot")
    verifier = ks.make_verifier("correct horse")
    monkeypatch.setattr(ks, "_acceptance_config", lambda: {"boards": ["prot"], "verifier": verifier})
    kb.create_board("other")
    return verifier


def _prot_review_card():
    conn = kbc.connect(board="prot")
    tid, run_id = _running(conn)
    assert kb.request_review(conn, tid, summary="ready", expected_run_id=run_id)
    return conn, tid


def test_connection_board_identity_is_derived_from_the_database_file(prot_board):
    with kbc.connect(board="prot") as p, kbc.connect(board="other") as o, kbc.connect() as d:
        with kb.scoped_current_board("other"):
            assert ks.board_of_connection(p) == "prot"
        assert ks.board_of_connection(o) == "other" and ks.board_of_connection(d) == "default"
    import sqlite3
    assert ks.board_of_connection(sqlite3.connect(":memory:")) is None


def test_ambient_board_cannot_bypass_the_protected_boards_acceptance(prot_board):
    conn, tid = _prot_review_card()
    for ambient in ("other", "default", "prot"):
        with kb.scoped_current_board(ambient):
            assert kb.complete_task(conn, tid, summary="no proof") is False
    assert _row(conn, tid)["status"] == "review"
    assert len(_events(conn, tid, "completion_refused_human_acceptance")) == 3
    with kb.scoped_current_board("other"):  # real human acceptance still works, whatever the ambient board
        token = ks.authenticate_passphrase("correct horse", prot_board, principal="zach")
        assert kb.complete_task(conn, tid, summary="approved", human_acceptance=token)
    assert _row(conn, tid)["status"] == "done"


def test_unprotected_board_is_unaffected_by_an_ambient_protected_board(prot_board):
    with kbc.connect(board="other") as oconn:
        tid, run_id = _running(oconn)
        with kb.scoped_current_board("prot"):
            assert kb.complete_task(oconn, tid, summary="fine", expected_run_id=run_id)


def test_unknown_connection_identity_fails_closed_when_any_board_is_protected(prot_board, tmp_path):
    custom = kbc.connect(db_path=tmp_path / "elsewhere.db")
    try:
        tid, run_id = _running(custom)
        assert ks.board_of_connection(custom) is None
        assert not kb.complete_task(custom, tid, summary="unknown identity", expected_run_id=run_id)
        assert _row(custom, tid)["status"] == "running"
    finally:
        custom.close()


def test_unknown_connection_identity_is_unchanged_when_no_board_is_protected(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "_acceptance_config", lambda: {"boards": [], "verifier": ""})
    custom = kbc.connect(db_path=tmp_path / "elsewhere.db")
    try:
        tid, run_id = _running(custom)
        assert kb.complete_task(custom, tid, summary="ok", expected_run_id=run_id)
    finally:
        custom.close()


def test_tool_level_kanban_complete_on_a_protected_board_is_refused_from_an_env_less_orchestrator(prot_board, monkeypatch):
    import json
    from tools import kanban_tools
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB"):
        monkeypatch.delenv(var, raising=False)
    conn, tid = _prot_review_card()
    with kb.scoped_current_board("other"):
        out = kanban_tools._handle_complete({"task_id": tid, "board": "prot", "summary": "orchestrator says done"})
    assert _row(conn, tid)["status"] == "review"
    assert json.loads(out).get("ok") is not True
    assert len(_events(conn, tid, "completion_refused_human_acceptance")) == 1


def _swarm_spec(kanban_swarm):
    return dict(goal="g", workers=[kanban_swarm.SwarmWorkerSpec(profile="worker", title="w", body="b")],
                verifier_assignee="v", synthesizer_assignee="s")


def _counts(conn):
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("tasks", "task_runs", "task_events", "task_links"))


def test_protected_board_refuses_swarm_creation_with_no_partial_writes(prot_board):
    from hermes_cli import kanban_swarm
    with kbc.connect(board="prot") as conn:
        before = _counts(conn)
        with pytest.raises(kanban_swarm.SwarmRefusedError):
            kanban_swarm.create_swarm(conn, **_swarm_spec(kanban_swarm))
        assert _counts(conn) == before  # no graph, root, run or event left behind


def test_inline_root_writer_cannot_reach_done_on_a_protected_board(prot_board):
    from hermes_cli import kanban_swarm
    with kbc.connect(board="prot") as conn:
        tid = kb.create_task(conn, title="root", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
            assert kanban_swarm._activate_root_inline(conn, tid, summary="x", metadata={}) is False
        assert _row(conn, tid)["status"] == "blocked"


def test_unprotected_board_swarm_root_behavior_is_unchanged(prot_board):
    from hermes_cli import kanban_swarm
    with kbc.connect(board="other") as conn:
        created = kanban_swarm.create_swarm(conn, **_swarm_spec(kanban_swarm))
        assert _row(conn, created.root_id)["status"] == "done"
        assert not _events(conn, created.root_id, "acceptance_exempt_system_root")


def test_inline_root_writer_still_refuses_a_task_with_run_history(kanban_home, monkeypatch):
    from hermes_cli import kanban_swarm
    monkeypatch.setattr(ks, "_acceptance_config", lambda: {"boards": [], "verifier": ""})
    with kbc.connect() as conn:
        tid, _ = _running(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
            assert kanban_swarm._activate_root_inline(conn, tid, summary="x", metadata={}) is False


def test_swarm_cli_on_a_protected_board_prints_a_refusal_and_writes_nothing(prot_board, monkeypatch):
    from hermes_cli import kanban as kc
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "prot")
    with kbc.connect(board="prot") as conn:
        before = _counts(conn)
    out = kc.run_slash("swarm g --worker worker:w --verifier v --synthesizer s")
    with kbc.connect(board="prot") as conn:
        assert _counts(conn) == before
    assert "not supported on a human-acceptance board" in out
