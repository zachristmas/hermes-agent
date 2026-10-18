"""Deliverable-contract declaration through the supported CLI, then Review/Done enforcement end to end.

Temporary HERMES_HOME only; no real board or card is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_settlement as ks


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _running(conn):
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid, kb._current_run_id(conn, tid)


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def _declare(tid, extra=""):
    return kc.run_slash(f"contract declare {tid} --require report.md:200 --contains 'report.md=## Findings' {extra}")


def test_declare_show_roundtrip_through_the_cli(kanban_home):
    with kbc.connect() as conn:
        tid, _ = _running(conn)
    assert "Declared 1 required deliverable" in _declare(tid, "--json-keys report.md=a,b")
    shown = json.loads(kc.run_slash(f"contract show {tid} --json"))
    assert shown == [{"name": "report.md", "min_bytes": 200, "must_contain": ["## Findings"], "json_keys": ["a", "b"]}]
    assert "already has a contract" in _declare(tid)  # no silent second declaration


@pytest.mark.parametrize("bad", [
    "--require ../escape.md", "--require report.md:0", "--require report.md:x",
    "--require report.md --contains other.md=x", "--require report.md --contains report.md",
])
def test_invalid_declarations_are_rejected_and_write_nothing(kanban_home, bad):
    with kbc.connect() as conn:
        tid, _ = _running(conn)
    out = kc.run_slash(f"contract declare {tid} {bad}")
    assert "kanban contract declare" in out or "error" in out.lower()
    with kbc.connect() as conn:
        assert ks.load_artifact_contract(conn, tid) == []


def test_declared_contract_refuses_partial_missing_and_prose_then_accepts_the_full_deliverable(kanban_home, tmp_path):
    with kbc.connect() as conn:
        tid, run_id = _running(conn)
    _declare(tid)
    with kbc.connect() as conn:
        ws = Path(kb._task_workspace_for_review(conn, tid))  # Review verifies deliverables inside the task workspace
    ws.mkdir(parents=True, exist_ok=True)
    partial = ws / "report.md"
    partial.write_text("## Findings\nstarted")
    with kbc.connect() as conn:
        for meta in ({"artifacts": [str(partial)]}, {"artifacts": [str(tmp_path / "other.md")]}, None):
            ok, reason = kb.request_review(conn, tid, summary=f"done, see {partial}", metadata=meta,
                                           expected_run_id=run_id, with_reason=True)
            assert not ok and reason.startswith(ks.UNMET_PREFIX)
        assert not kb.complete_task(conn, tid, summary="done", expected_run_id=run_id)
        assert _status(conn, tid) == "running"
        partial.write_text("## Findings\n" + "detail " * 60)
        ok, _ = kb.request_review(conn, tid, summary="ok", metadata={"artifacts": [str(partial)]},
                                  expected_run_id=run_id, with_reason=True)
        assert ok and _status(conn, tid) == "review"


def test_a_dispatched_worker_cannot_declare_or_strip_the_contract(kanban_home, monkeypatch):
    with kbc.connect() as conn:
        tid, _ = _running(conn)
    _declare(tid)
    with kbc.connect() as conn:
        ref_id = conn.execute("SELECT id FROM task_refs WHERE task_id=?", (tid,)).fetchone()["id"]
        tid2, _ = _running(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert "operator-only" in _declare(tid2)
    assert "operator-only" in kc.run_slash(f"ref rm {ref_id}")
    assert "operator-only" in kc.run_slash(f"ref add {tid2} --type review_artifact_contract --ref '{{}}'")
    with kbc.connect() as conn:
        assert len(ks.load_artifact_contract(conn, tid)) == 1
        assert ks.load_artifact_contract(conn, tid2) == []
