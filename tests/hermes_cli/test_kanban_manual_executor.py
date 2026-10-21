"""A manually claimed run settles through the executor it was bound to; stale or foreign agents never settle it.

Temporary HERMES_HOME only; the Herdr CLI is never invoked (``agent_info`` is an injected fixture).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_manual_executor as me


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _manual(conn):
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="coordinator-shell") is not None
    return tid, kb._current_run_id(conn, tid)


def _agent(tid, *, status="done", seq=10, native="native-A", tags=None):
    return {"agent_status": status, "completion_seq": seq, "agent_session": {"value": native},
            "tokens": {"kanban_task": tid, "kanban_board": "default", "kanban_run": "run-99", **(tags or {})}}


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def _receipts(conn, tid):
    return conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_result_receipt'",
                        (tid,)).fetchall()


def _bind(conn, tid, run, **kw):
    return me.bind_manual_executor(conn, tid, run, agent="exec-1", native_session="native-A",
                                   completion_seq=kw.pop("completion_seq", 10), board="default")


def test_finished_bound_executor_records_one_receipt_and_the_sweep_settles_it(kanban_home):
    with kbc.connect() as conn:
        tid, run = _manual(conn)
        assert _bind(conn, tid, run) == "bound"
        assert me.reconcile_manual_executors(conn, board="default", agent_info=lambda n: _agent(tid, seq=11)) == [tid]
        receipt = json.loads(_receipts(conn, tid)[0]["payload"])
        assert receipt["success"] is False and receipt["block_kind"] == "needs_input"
        assert "no run-scoped final report" in receipt["report"]
        # idempotent: a second sweep records nothing more, and settlement follows from the receipt alone
        assert me.reconcile_manual_executors(conn, board="default", agent_info=lambda n: _agent(tid, seq=12)) == []
        assert len(_receipts(conn, tid)) == 1
        kbd.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None)
        assert _status(conn, tid) == "blocked"
        blocked = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchall()
        assert blocked and json.loads(blocked[-1]["payload"]).get("kind") == "needs_input"


def test_lost_completion_event_is_recovered_by_a_restarted_dispatcher(kanban_home):
    with kbc.connect() as conn:
        tid, run = _manual(conn)
        _bind(conn, tid, run)
    with kbc.connect() as conn2:  # coordinator and first dispatcher are gone; the pane finished meanwhile
        assert me.reconcile_manual_executors(conn2, board="default", agent_info=lambda n: _agent(tid, seq=11)) == [tid]


@pytest.mark.parametrize("info", [
    None,                                                         # agent gone: no evidence
    {"agent_status": "working", "completion_seq": 99},            # still working
    {"agent_status": "done", "completion_seq": 10},               # nothing completed since bind (stale agent)
    {"agent_status": "done", "completion_seq": 11, "agent_session": {"value": "native-OTHER"}},   # replaced agent
    {"agent_status": "done", "completion_seq": 11, "tokens": {"kanban_task": "t_other"}},         # foreign card
])
def test_unfenced_or_unfinished_executor_changes_nothing(kanban_home, info):
    with kbc.connect() as conn:
        tid, run = _manual(conn)
        _bind(conn, tid, run)
        base = _agent(tid, seq=11)
        probe = None if info is None else {**base, **info}
        assert me.reconcile_manual_executors(conn, board="default", agent_info=lambda n: probe) == []
        assert not _receipts(conn, tid) and _status(conn, tid) == "running"


def test_unbound_run_is_never_settled_from_a_pane_alone(kanban_home):
    with kbc.connect() as conn:
        tid, _run = _manual(conn)
        assert me.reconcile_manual_executors(conn, board="default", agent_info=lambda n: _agent(tid, seq=99)) == []
        assert not _receipts(conn, tid)


def test_bind_is_fenced_one_per_run_and_refused_for_a_dispatcher_run(kanban_home):
    with kbc.connect() as conn:
        tid, run = _manual(conn)
        assert _bind(conn, tid, run) == "bound"
        assert _bind(conn, tid, run) == "already_bound"
        assert me.bind_manual_executor(conn, tid, run, agent="other", native_session="native-B",
                                       completion_seq=1) == "conflict"
        assert me.bind_manual_executor(conn, tid, run + 1, agent="exec-1", native_session="native-A",
                                       completion_seq=1) == "not_manual"
        tid2, run2 = _manual(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid2, "spawned", {"pid": 1}, run_id=run2)
        assert _bind(conn, tid2, run2) == "not_manual"


def test_a_run_scoped_final_report_is_judged_by_the_bridge_not_by_this_module(kanban_home, tmp_path):
    from hermes_cli import kanban_direct_herdr_report as run_report
    with kbc.connect() as conn:
        tid, run = _manual(conn)
        _bind(conn, tid, run)
        _, report = run_report.fence_paths(tid, None, f"run-{run}")
        report.write_text("## Status\ncomplete\n## Reply\nnone\n## Deliverables\n- [x] the output\n## Summary\ndone\n",
                          encoding="utf-8")
        calls = []

        def judge(*args, **kwargs):
            calls.append((args[2], kwargs["agent_name"], kwargs["report_path"]))
            return True, "judged by the bridge"
        assert me.reconcile_manual_executors(conn, board=None, agent_info=lambda n: _agent(tid, seq=11),
                                             record=judge) == [tid]
        assert calls == [(run, "exec-1", report)]


def test_bind_executor_command_binds_the_current_run_and_refuses_a_foreign_agent(kanban_home, monkeypatch, capsys):
    import argparse

    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_direct_herdr_worker as bridge
    with kbc.connect() as conn:
        tid, run = _manual(conn)
    live = {"exec-1": _agent(tid, seq=7)}
    monkeypatch.setattr(bridge, "_agent_info", lambda name, machine_id=None: live.get(name))
    args = argparse.Namespace(task_id=tid, agent="exec-1", run=None, json=True)
    assert kb_cli._cmd_bind_executor(args) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "bound"
    with kbc.connect() as conn:
        bound = json.loads(me._event(conn, tid, me.BOUND_EVENT, run)["payload"])
    assert bound["completion_seq"] == 7 and bound["native_session"] == "native-A"
    live["intruder"] = _agent("t_other", seq=1)
    assert kb_cli._cmd_bind_executor(argparse.Namespace(task_id=tid, agent="intruder", run=None, json=False)) == 1
    assert kb_cli._cmd_bind_executor(argparse.Namespace(task_id=tid, agent="missing", run=None, json=False)) == 1
