"""Closeout settles the execution attempt apart from the business task.

An executor problem (a push approval, a missing report) must not erase what the task accomplished:
progress is recorded on the card, the status follows the REMAINING work, and nothing re-queues the
card for a second production write.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_direct_herdr_report as run_report
from hermes_cli import kanban_direct_herdr_worker as bridge
from hermes_cli import kanban_settlement as ks

PUSH_PROMPT = "\x1b[1mgit push origin main\x1b[0m\n  > Allow this command? (y/n)\n$ ls -la\ntotal 48\n" * 20


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as c:
        yield c


def _running(conn):
    tid = kb.create_task(conn, title="first SAP order", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid, int(claimed.current_run_id)


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _progress(wait_kind="external_event", **extra):
    return {
        "finished": ["production change applied to the storefront config (commit abc1234)"],
        "remaining": ["confirm the first SAP order arrives"],
        "next_owner": "worker",
        "trigger": "the first SAP order arriving",
        "wait_kind": wait_kind,
        "recheck": {"how": "read the SAP order list; change nothing", "interval_seconds": 600, "max_checks": 2},
        **extra,
    }


def _receipt(conn, tid, run_id, *, wait_kind="external_event", problem="agent titan-t_x is blocked on an approval/question dialog and needs a person to answer it."):
    assert ks.record_result_receipt(
        conn, tid, run_id, success=False, report=problem, block_kind="transient",
        closeout={"progress": _progress(wait_kind), "executor_problem": problem, "diagnostics": "herdr-transcript-run1.txt"},
    )


def test_push_approval_after_a_production_change_waits_in_scheduled_without_a_second_write(conn):
    tid, run_id = _running(conn)
    _receipt(conn, tid, run_id)

    assert ks.settle_from_receipt(conn, tid) == "scheduled"

    task = kb.get_task(conn, tid)
    assert task.status == "scheduled"
    assert task.consecutive_failures == 0
    progress = _events(conn, tid, "progress_recorded")[-1].payload
    assert progress["finished"] == ["production change applied to the storefront config (commit abc1234)"]
    summary = progress["summary"]
    assert len(summary) < 700 and "\x1b" not in summary and "git push" not in summary
    assert "Remaining: confirm the first SAP order arrives" in summary
    assert "Triggered by: the first SAP order arriving" in summary
    problem = _events(conn, tid, "executor_closeout_problem")[-1].payload
    assert problem["task_status_unaffected"] is True
    assert _events(conn, tid, "block_loop_detected") == []

    # The scheduled card is not dispatchable: nothing can repeat the production write.
    spawned: list[str] = []
    kbd.dispatch_once(conn, spawn_fn=lambda t, w, board=None: spawned.append(t.id) or 4242)
    assert spawned == []
    assert kb.get_task(conn, tid).status == "scheduled"


def test_recheck_is_bounded_read_only_and_carries_the_recorded_progress(conn):
    tid, run_id = _running(conn)
    _receipt(conn, tid, run_id)
    ks.settle_from_receipt(conn, tid)

    assert ks.reconcile_rechecks(conn, now=int(time.time())) == []  # not due yet
    later = int(time.time()) + 601
    assert ks.reconcile_rechecks(conn, now=later) == [tid]
    context = kb.build_worker_context(conn, tid)
    assert "Do NOT repeat it" in context and "commit abc1234" in context
    assert "READ-ONLY recheck" in context

    # Two rechecks used, the awaited event never happened: the wait is handed over once, still scheduled.
    kb.claim_task(conn, tid, claimer="worker")
    run = kb.get_task(conn, tid).current_run_id
    ks.record_result_receipt(conn, tid, run, success=True, report="still waiting", closeout={
        "progress": _progress(), "executor_problem": None})
    ks.settle_from_receipt(conn, tid)
    assert ks.reconcile_rechecks(conn, now=later + 10_000) == [tid]
    kb.claim_task(conn, tid, claimer="worker")
    run = kb.get_task(conn, tid).current_run_id
    ks.record_result_receipt(conn, tid, run, success=True, report="still waiting", closeout={
        "progress": _progress(), "executor_problem": None})
    ks.settle_from_receipt(conn, tid)
    assert ks.reconcile_rechecks(conn, now=later + 20_000) == []
    diagnostics = _events(conn, tid, "block_loop_detected")
    assert [d.payload["kind"] for d in diagnostics] == ["recheck_exhausted"]
    assert ks.reconcile_rechecks(conn, now=later + 30_000) == []
    assert len(_events(conn, tid, "block_loop_detected")) == 1
    assert kb.get_task(conn, tid).status == "scheduled"


def test_recurring_executor_problems_stop_rechecks_but_keep_the_waiting_state(conn):
    tid, run_id = _running(conn)
    _receipt(conn, tid, run_id)
    ks.settle_from_receipt(conn, tid)
    now = int(time.time()) + 601
    for round_ in range(ks.EXECUTOR_PROBLEM_LIMIT - 1):
        assert ks.reconcile_rechecks(conn, now=now + round_ * 1000) == [tid]
        kb.claim_task(conn, tid, claimer="worker")
        run = kb.get_task(conn, tid).current_run_id
        _receipt(conn, tid, run)
        ks.settle_from_receipt(conn, tid)
    assert ks.reconcile_rechecks(conn, now=now + 50_000) == []
    kinds = [d.payload["kind"] for d in _events(conn, tid, "block_loop_detected")]
    assert kinds == ["recurring_executor_problem"]
    task = kb.get_task(conn, tid)
    assert task.status == "scheduled"
    assert task.consecutive_failures == 0


def test_a_missing_approval_blocks_with_progress_kept(conn):
    tid, run_id = _running(conn)
    _receipt(conn, tid, run_id, wait_kind="approval")
    assert ks.settle_from_receipt(conn, tid) == "blocked"
    task = kb.get_task(conn, tid)
    assert (task.status, task.block_kind) == ("blocked", "needs_input")
    assert _events(conn, tid, "progress_recorded")


def test_a_recurring_block_stops_retrying_but_never_moves_to_triage(conn):
    tid, _ = _running(conn)
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
        assert kb.block_task(conn, tid, reason="needs a credential", kind="capability")
        assert kb.get_task(conn, tid).status == "blocked"
        kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid, claimer="worker")
    assert kb.block_task(conn, tid, reason="needs a credential", kind="capability")
    assert kb.get_task(conn, tid).status == "blocked"
    loop = _events(conn, tid, "block_loop_detected")[-1].payload
    assert loop["retries_stopped"] is True
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, tid).status == "blocked"


def test_a_scope_question_is_the_only_block_that_routes_to_triage(conn):
    tid, _ = _running(conn)
    assert kb.block_task(conn, tid, reason="which entity owns this order?", kind="scope_question")
    assert kb.get_task(conn, tid).status == "triage"


def test_blocked_agent_summary_is_short_and_the_capture_is_kept_as_a_diagnostic(monkeypatch):
    monkeypatch.setattr(bridge, "_wait_until_settled", lambda name, machine_id=None: "blocked")
    monkeypatch.setattr(bridge, "_read_agent_for_report", lambda name, machine_id=None: PUSH_PROMPT)
    bridge._DIAGNOSTICS.clear()
    bridge._APPROVAL_PENDING = False
    ok, report, preserved = bridge._settle_and_close("titan-t_x", "w:p1")
    assert (ok, preserved) == (False, True)
    assert "approval/question dialog" in report and "needs a person" in report
    assert len(report) < 300 and "git push" not in report and "\x1b" not in report
    assert bridge._APPROVAL_PENDING is True
    assert bridge._DIAGNOSTICS == [("blocked on an approval/question dialog", PUSH_PROMPT)]


def test_unsettled_agent_summary_does_not_embed_the_capture(monkeypatch):
    monkeypatch.setattr(bridge, "_wait_until_settled", lambda name, machine_id=None: "idle")
    statuses = iter(["blocked"])
    monkeypatch.setattr(bridge, "agent_status", lambda name, machine_id=None: next(statuses))
    monkeypatch.setattr(bridge, "_read_agent_for_report", lambda name, machine_id=None: PUSH_PROMPT)
    bridge._DIAGNOSTICS.clear()
    ok, report, _ = bridge._settle_and_close("titan-t_x", "w:p1")
    assert ok is False and "git push" not in report and len(report) < 300
    assert len(bridge._DIAGNOSTICS) == 1


def test_a_waiting_report_after_a_push_approval_becomes_a_scheduled_closeout(tmp_path, monkeypatch):
    report_path = tmp_path / "run-1.report.md"
    report_path.write_text(
        "## Status\nwaiting\n## Deliverables\n- [x] production change applied - commit abc1234\n"
        "## Summary\nChange is live.\n## Remaining\n- first SAP order not seen yet\n"
        "## Next\nowner: worker | trigger: the first SAP order arriving\n"
        "## Recheck\nread the SAP order list | every 10 minutes | up to 2 checks\n", encoding="utf-8")
    monkeypatch.setattr(bridge, "_APPROVAL_PENDING", True)
    closeout = bridge._closeout_from_report(report_path, "agent titan-t_x is blocked on an approval", "diag.txt")
    progress = closeout["progress"]
    assert progress["wait_kind"] == "external_event"
    assert progress["finished"] == ["production change applied - commit abc1234"]
    assert progress["remaining"] == ["first SAP order not seen yet"]
    assert progress["trigger"] == "the first SAP order arriving"
    assert progress["recheck"] == {"how": "read the SAP order list", "interval_seconds": 600, "max_checks": 2}
    assert closeout["executor_problem"].startswith("agent titan-t_x is blocked")


def test_partial_report_stopped_at_an_approval_is_filed_as_an_approval_wait(tmp_path, monkeypatch):
    report_path = tmp_path / "run-2.report.md"
    report_path.write_text(
        "## Status\npartial\n## Deliverables\n- [x] code change - commit abc1234\n- [ ] push - needs approval\n"
        "## Summary\nPush pending.\n", encoding="utf-8")
    monkeypatch.setattr(bridge, "_APPROVAL_PENDING", True)
    progress = bridge._closeout_from_report(report_path, "blocked", None)["progress"]
    assert progress["wait_kind"] == "approval"
    assert progress["remaining"] == ["push - needs approval"]


def test_no_report_means_no_closeout(tmp_path):
    assert bridge._closeout_from_report(tmp_path / "missing.md", "blocked", None) is None
    assert run_report.progress_from_report(run_report.FinalReport({"status": "complete"})) is None
