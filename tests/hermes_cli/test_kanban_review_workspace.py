"""request_review with expected_run_id on the REAL schema: the workspace comes from tasks.workspace_*,
a refusal after the transaction opened rolls back, and a stale run writes nothing. Temp HERMES_HOME only."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


def _running(conn):
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid, kb._current_run_id(conn, tid)


def _snapshot(conn, tid):
    return (
        tuple(conn.execute("SELECT status, current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()),
        conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0],
        tuple(tuple(r) for r in conn.execute("SELECT id, status, outcome, ended_at FROM task_runs WHERE task_id=?", (tid,))),
        conn.execute("SELECT COUNT(*) FROM task_attachments WHERE task_id=?", (tid,)).fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_attachments'").fetchone() else 0,
    )


def test_task_runs_really_has_no_workspace_column(conn):
    assert "workspace_path" not in {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}


def test_active_matching_run_enters_review_without_artifacts(conn):
    tid, run_id = _running(conn)
    ok, reason = kb.request_review(conn, tid, summary="notes ready", expected_run_id=run_id, with_reason=True)
    assert ok, reason
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "review"


def test_active_matching_run_with_a_verified_workspace_deliverable_enters_review(conn):
    tid, run_id = _running(conn)
    ws = kb.workspaces_root(board="default") / tid
    ws.mkdir(parents=True)
    deliverable = ws / "DELIVERABLE.md"
    deliverable.write_text("# Result\n" + "substantive content " * 40)
    ok, reason = kb.request_review(
        conn, tid, summary=f"Deliverable written to {deliverable}", metadata={"artifacts": [str(deliverable)]},
        expected_run_id=run_id, with_reason=True)
    assert ok, reason
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "review"


def test_stale_run_fails_without_writes(conn):
    tid, run_id = _running(conn)
    before = _snapshot(conn, tid)
    ok, _ = kb.request_review(conn, tid, summary="late", expected_run_id=run_id + 99, with_reason=True)
    assert ok is False
    assert _snapshot(conn, tid) == before


def test_unverifiable_declared_artifact_is_refused_and_rolled_back(conn):
    tid, run_id = _running(conn)
    before = _snapshot(conn, tid)
    outside = conn.execute("SELECT 1").fetchone() and Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent / "stray.md"
    outside.write_text("x" * 600)
    ok, reason = kb.request_review(
        conn, tid, summary=f"Deliverable at {outside}", metadata={"artifacts": [str(outside)]},
        expected_run_id=run_id, with_reason=True)
    assert ok is False and "Review refused" in reason
    assert _snapshot(conn, tid) == before  # the status flip did not commit with the refusal
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='review_requested'", (tid,)).fetchone()


def test_bare_declared_file_names_are_verified_inside_the_workspace_only(tmp_path):
    from hermes_cli.kanban_settlement import _verified_review_artifacts

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret deliverable")
    (ws / "REPORT.md").write_text("real deliverable")
    (ws / "linked.md").symlink_to(outside)  # symlink escaping the root
    report = "deliverable ready: REPORT.md linked.md outside.md ../outside.md missing.md notes.exe"
    assert _verified_review_artifacts(report, str(ws)) == [str((ws / "REPORT.md").resolve())]
    assert _verified_review_artifacts("see REPORT.md", "") == []
    # handoff-only prose still refuses an ordinary bare file (only the named deliverable files pass)
    (ws / "notes.md").write_text("working notes")
    assert _verified_review_artifacts("handoff pending, wrote notes.md", str(ws)) == []
