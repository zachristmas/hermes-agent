"""``hermes kanban reopen-done``: audited operator recovery of an incorrectly auto-Done card.

Isolated fixtures only (temp ``HERMES_HOME``); every card goes through the real lifecycle
(``create_task`` -> ``complete_task``) so its ``completed`` event and run history are genuine.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_settlement as ks


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _done(conn, *, title="auto-done card", parents=()):
    tid = kb.create_task(conn, title=title, body="original body", assignee="worker", parents=list(parents))
    if kb.get_task(conn, tid).status == "ready":
        assert kb.complete_task(conn, tid, result="retained result", summary="retained summary")
    else:
        conn.execute("UPDATE tasks SET status='done', result='retained result' WHERE id=?", (tid,))
    assert kb.get_task(conn, tid).status == "done"
    return tid


def _kinds(conn, tid):
    return [e.kind for e in kb.list_events(conn, tid)]


def _reopen(conn, tid, reason="auto Done was wrong", **kw):
    return kb.reopen_done_task(conn, tid, actor="op", reason=reason, **kw)


def test_reopen_done_recovers_to_ready_and_review_route_still_gated(conn, tmp_path):
    tid = _done(conn)
    kb.add_comment(conn, tid, "someone", "keep me")
    att = tmp_path / "evidence.txt"
    att.write_text("evidence")
    kb.add_attachment(conn, tid, filename=att.name, stored_path=str(att), size=8)
    before = (kb.get_task(conn, tid), _kinds(conn, tid), [c.body for c in kb.list_comments(conn, tid)],
              [a.filename for a in kb.list_attachments(conn, tid)], len(kb.list_runs(conn, tid)))

    ok, err, info = _reopen(conn, tid)
    assert ok and err is None and info["to_status"] == "ready"

    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.completed_at is None
    assert (task.id, task.title, task.body, task.result) == (tid, before[0].title, "original body", "retained result")
    assert _kinds(conn, tid)[:len(before[1])] == before[1]
    assert [c.body for c in kb.list_comments(conn, tid)] == before[2]
    assert [a.filename for a in kb.list_attachments(conn, tid)] == before[3]
    assert len(kb.list_runs(conn, tid)) == before[4]
    audit = [e for e in kb.list_events(conn, tid) if e.kind == "done_recovered"]
    assert len(audit) == 1
    assert audit[0].payload["actor"] == "op" and audit[0].payload["reason"] == "auto Done was wrong"
    assert audit[0].payload["from_status"] == "done" and audit[0].payload["to_status"] == "ready"
    assert audit[0].payload["prior_completed_at"] == before[0].completed_at

    # The recovered card re-enters through the ordinary evidence-gated route; Done is not granted.
    assert kb.request_review(conn, tid, summary="re-reviewed with retained evidence")
    assert kb.get_task(conn, tid).status == "review"


def test_dry_run_runs_checks_without_mutation(conn):
    tid = _done(conn)
    child = kb.create_task(conn, title="child", parents=[tid], assignee="w")
    snapshot = (kb.get_task(conn, tid), _kinds(conn, tid), kb.get_task(conn, child).status)
    ok, err, info = _reopen(conn, tid, dry_run=True)
    assert ok and err is None and info == {"to_status": "ready", "invalidate": [child]}
    assert (kb.get_task(conn, tid), _kinds(conn, tid), kb.get_task(conn, child).status) == snapshot
    ok, err, _ = _reopen(conn, tid, reason="  ", dry_run=True)  # dry-run applies the same refusals
    assert not ok and "reason" in err


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_blank_reason_refused(conn, bad):
    tid = _done(conn)
    ok, err, _ = kb.reopen_done_task(conn, tid, actor="op", reason=bad)
    assert not ok and "reason" in err
    assert kb.get_task(conn, tid).status == "done" and "done_recovered" not in _kinds(conn, tid)


def test_wrong_state_and_missing_task_refused(conn):
    ready = kb.create_task(conn, title="r", assignee="w")
    triage = kb.create_task(conn, title="t", body="b", triage=True)
    archived = _done(conn, title="a")
    conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (archived,))
    for tid, status in ((ready, "ready"), (triage, "triage"), (archived, "archived")):
        ok, err, _ = _reopen(conn, tid)
        assert not ok and "only applies to 'done'" in err
        assert kb.get_task(conn, tid).status == status
    assert "not found" in _reopen(conn, "t_missing")[1]


@pytest.mark.parametrize("column, value", [("claim_lock", "host:1"), ("worker_pid", 4242), ("current_run_id", 9)])
def test_live_claim_or_run_refused(conn, column, value):
    tid = _done(conn)
    conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, tid))
    ok, err, _ = _reopen(conn, tid)
    assert not ok and "claim or open run" in err
    assert kb.get_task(conn, tid).status == "done" and "done_recovered" not in _kinds(conn, tid)


def test_open_hold_refused_and_resolved_hold_allowed(conn):
    tid = _done(conn)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "recovery_hold", {"reason": "uncertain effect"}, run_id=3)
    ok, err, _ = _reopen(conn, tid)
    assert not ok and "open holds" in err and kb.get_task(conn, tid).status == "done"
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "recovery_hold_resolved", {"decision": "owner_stopped"}, run_id=3)
    assert _reopen(conn, tid)[0]


def test_protected_human_acceptance_board_refused_with_no_override(conn):
    tid = _done(conn)
    ks.write_policy(["default"], "scrypt$x$y")
    ok, err, _ = _reopen(conn, tid)
    assert not ok and "protects trusted human acceptance" in err
    assert kb.get_task(conn, tid).status == "done" and "done_recovered" not in _kinds(conn, tid)
    with pytest.raises(TypeError):
        kb.reopen_done_task(conn, tid, actor="op", reason="r", force=True)  # no bypass parameter exists


def test_unfinished_parent_lands_in_todo(conn):
    tid = _done(conn)
    open_parent = kb.create_task(conn, title="still open", assignee="w")
    kb.link_tasks(conn, open_parent, tid)
    ok, _, info = _reopen(conn, tid)
    assert ok and info["to_status"] == "todo" and kb.get_task(conn, tid).status == "todo"


def test_descendants_are_invalidated_like_the_dashboard_reopen(conn):
    tid = _done(conn)
    ready_child = kb.create_task(conn, title="ready child", parents=[tid], assignee="w")
    done_child = _done(conn, title="done child", parents=[tid])
    grandchild = kb.create_task(conn, title="grandchild", parents=[ready_child], assignee="w")
    ok, _, info = _reopen(conn, tid)
    assert ok and info["invalidate"] == sorted([ready_child, done_child])
    for cid in (ready_child, done_child):
        assert kb.get_task(conn, cid).status == "todo"
        assert "descendant_invalidated" in _kinds(conn, cid)
        assert any("was reopened" in c.body for c in kb.list_comments(conn, cid))
    assert kb.get_task(conn, grandchild).status == "todo"  # was never started; stays gated
    audit = next(e for e in kb.list_events(conn, tid) if e.kind == "done_recovered")
    assert audit.payload["invalidated"] == info["invalidate"]


def test_running_descendant_refused_and_nothing_killed(conn):
    tid = _done(conn)
    child = kb.create_task(conn, title="child", parents=[tid], assignee="w")
    conn.execute("UPDATE tasks SET status='running', claim_lock='host:2', worker_pid=77 WHERE id=?", (child,))
    ok, err, _ = _reopen(conn, tid)
    assert not ok and child in err
    assert kb.get_task(conn, tid).status == "done" and kb.get_task(conn, child).status == "running"


def test_claimed_review_descendant_refused(conn):
    tid = _done(conn)
    child = kb.create_task(conn, title="review child", parents=[tid], assignee="w")
    conn.execute(
        "UPDATE tasks SET status='review', claim_lock='host:3', current_run_id=5, worker_pid=88 WHERE id=?",
        (child,),
    )
    ok, err, _ = _reopen(conn, tid)
    assert not ok and child in err and "claim, run or worker" in err
    assert kb.get_task(conn, tid).status == "done" and "done_recovered" not in _kinds(conn, tid)
    row = kb.get_task(conn, child)
    assert (row.status, row.claim_lock, row.worker_pid) == ("review", "host:3", 88)
    assert "descendant_invalidated" not in _kinds(conn, child)


def test_ownerless_running_descendant_refused(conn):
    tid = _done(conn)
    child = kb.create_task(conn, title="child", parents=[tid], assignee="w")
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))  # no claim/pid/run metadata
    ok, err, _ = _reopen(conn, tid)
    assert not ok and child in err
    assert kb.get_task(conn, tid).status == "done" and kb.get_task(conn, child).status == "running"
    assert "done_recovered" not in _kinds(conn, tid)


def test_worker_context_cannot_reopen_closed_tasks(conn, monkeypatch):
    tid = _done(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_some_running_worker")
    for dry_run in (False, True):
        ok, err, _ = _reopen(conn, tid, dry_run=dry_run)
        assert not ok and "operator-only" in err
    assert kb.get_task(conn, tid).status == "done" and "done_recovered" not in _kinds(conn, tid)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert _reopen(conn, tid)[0]


def test_failure_mid_transition_rolls_back_everything(conn, monkeypatch):
    tid = _done(conn)
    child = kb.create_task(conn, title="child", parents=[tid], assignee="w")
    before = (kb.get_task(conn, tid), _kinds(conn, tid), kb.get_task(conn, child).status, _kinds(conn, child))

    def boom(*a, **kw):
        raise RuntimeError("invalidate failed")

    monkeypatch.setattr(kb, "invalidate_descendants_for_parent_reopen", boom)
    with pytest.raises(RuntimeError):
        _reopen(conn, tid)
    assert (kb.get_task(conn, tid), _kinds(conn, tid), kb.get_task(conn, child).status, _kinds(conn, child)) == before


def test_preconditions_rechecked_inside_the_write_txn(conn, monkeypatch):
    import contextlib

    tid = _done(conn)
    real = kb.write_txn

    @contextlib.contextmanager
    def racing(c, **kw):
        c.execute("UPDATE tasks SET worker_pid=4242 WHERE id=?", (tid,))
        with real(c, **kw):
            yield

    monkeypatch.setattr(kb, "write_txn", racing)
    ok, err, _ = _reopen(conn, tid)
    assert not ok and "claim or open run" in err
    assert kb.get_task(conn, tid).status == "done" and "done_recovered" not in _kinds(conn, tid)


def _ns(task_id, reason, *, dry_run=False, as_json=False):
    return argparse.Namespace(task_id=task_id, reason=list(reason), dry_run=dry_run, json=as_json)


def test_cli_dry_run_then_real_and_blank_reason(kanban_home, capsys):
    with kbc.connect() as c:
        tid = _done(c)
    assert kb_cli._cmd_reopen_done(_ns(tid, ["  "])) == 1
    assert kb_cli._cmd_reopen_done(_ns(tid, ["wrongly", "auto-done"], dry_run=True)) == 0
    assert "Would reopen" in capsys.readouterr().out
    with kbc.connect() as c:
        assert kb.get_task(c, tid).status == "done"
    assert kb_cli._cmd_reopen_done(_ns(tid, ["wrongly", "auto-done"], as_json=True)) == 0
    assert '"reopened": true' in capsys.readouterr().out
    with kbc.connect() as c:
        assert kb.get_task(c, tid).status == "ready"


def test_cli_is_fenced_for_delegated_children(kanban_home, monkeypatch):
    assert kb_cli._is_delegated_child_cli_mutation(argparse.Namespace(kanban_action="reopen-done")) is False
    with kbc.connect() as c:
        tid = _done(c)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    assert kb_cli._is_delegated_child_cli_mutation(argparse.Namespace(kanban_action="reopen-done")) is True
    with kbc.connect() as c:
        with pytest.raises(PermissionError):
            _reopen(c, tid)
        assert kb.get_task(c, tid).status == "done"
