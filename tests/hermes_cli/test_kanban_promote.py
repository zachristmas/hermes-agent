"""Tests for the kanban `promote` verb (issue #28822).

The realistic bug scenario from #28822 is: a child task ends up in
``todo`` with all its parents already ``done`` (because the
auto-promote daemon hasn't run, or a manual close raced it).
Direct-SQL setup is used to construct that state deterministically.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


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


def _stuck_todo(conn, *, parents_done=True, n_parents=1):
    """Build the #28822 scenario: child in 'todo' whose parents may
    have closed as 'done' without the auto-promote logic firing.
    """
    parent_ids = [
        kb.create_task(conn, title=f"parent{i}", assignee="setup")
        for i in range(n_parents)
    ]
    child_id = kb.create_task(
        conn, title="child", parents=parent_ids, assignee="setup"
    )
    assert kb.get_task(conn, child_id).status == "todo"
    if parents_done:
        for pid in parent_ids:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (pid,)
            )
    return child_id, parent_ids


def test_promote_stuck_todo_succeeds(conn):
    child, _ = _stuck_todo(conn, parents_done=True)
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"


def test_promote_refuses_undone_parent_and_names_the_real_remedy(conn):
    # #106195: promotion must never report a 'ready' that the first claim reverts.
    child, (parent,) = _stuck_todo(conn, parents_done=False)
    ok, err = kb.promote_task(conn, child, actor="tester", reason="recovery")
    assert not ok
    assert err and parent in err
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None  # still gated; nothing pretended




# ---------------------------------------------------------------------------
# CLI `_cmd_promote` — bulk via `--ids` (the issue's anti-respawn use case:
# promote all children of a closed parent in one command).
# ---------------------------------------------------------------------------


def _promote_ns(task_id, *, ids=None, reason=None, force=False,
                dry_run=False, as_json=False):
    return argparse.Namespace(
        task_id=task_id,
        reason=list(reason or []),
        ids=list(ids or []) or None,
        force=force,
        dry_run=dry_run,
        json=as_json,
    )


def test_cli_promote_bulk_ids_promotes_all(kanban_home, capsys):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        children = [
            kb.create_task(conn, title=f"c{i}", parents=[parent])
            for i in range(3)
        ]
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    rc = kb_cli._cmd_promote(_promote_ns(children[0], ids=children[1:]))
    assert rc == 0
    out = capsys.readouterr().out
    for c in children:
        assert c in out
    with kbc.connect() as conn:
        for c in children:
            assert kb.get_task(conn, c).status == "ready"




# ---------------------------------------------------------------------------
# `promote --from-triage`: explicit operator route for already-specified triage cards.
# ---------------------------------------------------------------------------


def _triage_card(conn, *, body="Specified body", parents=()):
    tid = kb.create_task(conn, title="Spec'd card", body=body, parents=list(parents),
                         assignee="setup", triage=True)
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def _snapshot(conn, tid):
    t = kb.get_task(conn, tid)
    return (t.title, t.body, t.assignee, [e.kind for e in kb.list_events(conn, tid)])


def test_default_promote_still_refuses_triage(conn):
    tid = _triage_card(conn)
    ok, err = kb.promote_task(conn, tid, actor="t")
    assert not ok and "--from-triage" in err
    assert kb.get_task(conn, tid).status == "triage"


def test_from_triage_promotes_and_preserves_card(conn):
    tid = _triage_card(conn)
    kb.add_comment(conn, tid, "someone", "keep me")
    before = _snapshot(conn, tid)
    ok, err = kb.promote_task(conn, tid, actor="op", reason="spec reviewed", from_triage=True)
    assert ok and err is None
    after = _snapshot(conn, tid)
    assert kb.get_task(conn, tid).status == "ready"
    assert after[:3] == before[:3]
    assert after[3][:len(before[3])] == before[3] and after[3][-1] == "promoted_manual"
    ev = [e for e in kb.list_events(conn, tid) if e.kind == "promoted_manual"][-1]
    assert ev.payload == {"actor": "op", "reason": "spec reviewed", "from_status": "triage"}
    assert [c.body for c in kb.list_comments(conn, tid)] == ["keep me"]


def test_from_triage_dry_run_validates_without_mutation(conn):
    tid = _triage_card(conn)
    before = _snapshot(conn, tid)
    assert kb.promote_task(conn, tid, actor="op", reason="r", from_triage=True, dry_run=True) == (True, None)
    assert _snapshot(conn, tid) == before and kb.get_task(conn, tid).status == "triage"
    empty = _triage_card(conn, body="  ")
    ok, err = kb.promote_task(conn, empty, actor="op", reason="r", from_triage=True, dry_run=True)
    assert not ok and "usable spec" in err


def test_from_triage_refuses_non_triage_and_empty_spec(conn):
    child, _ = _stuck_todo(conn)
    ok, err = kb.promote_task(conn, child, actor="op", reason="r", from_triage=True)
    assert not ok and "only applies to 'triage'" in err
    empty = _triage_card(conn, body="")
    assert not kb.promote_task(conn, empty, actor="op", reason="r", from_triage=True)[0]
    assert kb.get_task(conn, empty).status == "triage"


def test_from_triage_refuses_unfinished_parent(conn):
    parent = kb.create_task(conn, title="p", assignee="setup")
    tid = _triage_card(conn, parents=[parent])
    ok, err = kb.promote_task(conn, tid, actor="op", reason="r", from_triage=True)
    assert not ok and parent in err
    assert kb.get_task(conn, tid).status == "triage"


def test_from_triage_refuses_live_claim_and_open_hold(conn):
    claimed = _triage_card(conn)
    conn.execute("UPDATE tasks SET claim_lock='host:1' WHERE id=?", (claimed,))
    ok, err = kb.promote_task(conn, claimed, actor="op", reason="r", from_triage=True)
    assert not ok and "live claim" in err

    held = _triage_card(conn)
    with kb.write_txn(conn):
        kb._append_event(conn, held, "recovery_hold", {"reason": "uncertain"}, run_id=7)
    ok, err = kb.promote_task(conn, held, actor="op", reason="r", from_triage=True)
    assert not ok and "hold" in err
    assert kb.get_task(conn, held).status == "triage"


def test_cli_from_triage_requires_reason_and_bulk_is_honest(kanban_home, capsys):
    with kbc.connect() as conn:
        good = _triage_card(conn)
        bad = _triage_card(conn, body="")
    ns = _promote_ns(good, ids=[bad])
    ns.from_triage = True
    assert kb_cli._cmd_promote(ns) != 0  # no reason
    ns = _promote_ns(good, ids=[bad], reason=["reviewed"], as_json=True)
    ns.from_triage = True
    assert kb_cli._cmd_promote(ns) == 1  # partial failure is nonzero
    out = capsys.readouterr().out
    assert '"promoted": true' in out and '"promoted": false' in out
    with kbc.connect() as conn:
        assert kb.get_task(conn, good).status == "ready"
        assert kb.get_task(conn, bad).status == "triage"


def test_from_triage_denied_in_worker_context(kanban_home, monkeypatch):
    with kbc.connect() as conn:
        tid = _triage_card(conn)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    ns = argparse.Namespace(kanban_action="promote")
    assert kb_cli._is_delegated_child_cli_mutation(ns) is True
    with kbc.connect() as conn:
        with pytest.raises(PermissionError):
            kb.promote_task(conn, tid, actor="child", reason="x", from_triage=True)
        assert kb.get_task(conn, tid).status == "triage"


def test_from_triage_rechecks_preconditions_inside_the_write_txn(conn, monkeypatch):
    # A writer lands between the pre-check and the transaction: the in-txn recheck must refuse.
    import contextlib

    tid = _triage_card(conn)
    real = kb.write_txn

    @contextlib.contextmanager
    def racing(c, **kw):
        c.execute("UPDATE tasks SET worker_pid=4242 WHERE id=?", (tid,))
        with real(c, **kw):
            yield

    monkeypatch.setattr(kb, "write_txn", racing)
    ok, err = kb.promote_task(conn, tid, actor="op", reason="r", from_triage=True)
    assert not ok and "live claim" in err
    assert kb.get_task(conn, tid).status == "triage"
    assert not [e for e in kb.list_events(conn, tid) if e.kind == "promoted_manual"]


def test_from_triage_kernel_requires_reason(conn):
    tid = _triage_card(conn)
    for bad in (None, "", "   "):
        ok, err = kb.promote_task(conn, tid, actor="op", reason=bad, from_triage=True)
        assert not ok and "reason" in err
    assert kb.promote_task(conn, tid, actor="op", reason="r", from_triage=True, dry_run=True)[0]
    assert kb.get_task(conn, tid).status == "triage"


def test_resolved_recovery_hold_no_longer_blocks_but_open_one_does(conn):
    from hermes_cli.kanban_settlement import _open_holds

    tid = _triage_card(conn)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "recovery_hold", {"reason": "u"}, run_id=3)
    assert _open_holds(conn, tid)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "recovery_hold_resolved", {"decision": "owner_stopped"}, run_id=3)
    assert _open_holds(conn, tid) == []
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "recovery_hold", {"reason": "again"}, run_id=4)
    assert _open_holds(conn, tid)
