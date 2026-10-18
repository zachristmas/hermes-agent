"""Direct-Herdr bridge settlement: the bridge only writes a durable receipt; the dispatcher settles.

Temporary HERMES_HOME only; the Herdr CLI is never invoked (``_mark_card`` is the card boundary).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_direct_herdr_worker as bridge
from hermes_cli import kanban_settlement as ks


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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


def _events(conn, tid, kind):
    return conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind)).fetchall()


@pytest.fixture(autouse=True)
def _settled_native_agent(monkeypatch):
    """The bridge's native-agent fence stays ON: these tests supply a terminal agent whose own
    report is the one recorded (the caller-supplied text is never trusted)."""
    monkeypatch.setattr(bridge, "agent_status", lambda name, machine_id=None: "idle")
    monkeypatch.setattr(bridge, "read_agent", lambda name, machine_id=None: bridge._TEST_REPORT)


def _bridge_result(tid, run_id, ws, report):
    bridge._TEST_REPORT = report
    return bridge._mark_card(tid, None, run_id, True, "ignored caller text", workspace_dir=str(ws),
                             settlement_owner=True, agent_name="native-1")


def test_bridge_writes_receipt_only_and_the_sweep_enters_review(kanban_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "out.md").write_text("result " * 50)
    with kbc.connect() as conn:
        tid, run_id = _running(conn)
        ok, _ = _bridge_result(tid, run_id, ws, f"finished, wrote {ws}/out.md")
        assert ok and _status(conn, tid) == "running"  # bridge itself does not transition the card
        receipt = json.loads(_events(conn, tid, "worker_result_receipt")[0]["payload"])
        assert receipt["success"] and receipt["artifacts"] == [str((ws / "out.md").resolve())]
        spawned: list = []
        kbd.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: spawned.append(1))
        assert _status(conn, tid) == "review" and not spawned


def test_coordinator_death_after_result_is_settled_by_a_later_sweep(kanban_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "out.md").write_text("result " * 50)
    with kbc.connect() as conn:
        tid, run_id = _running(conn)
        _bridge_result(tid, run_id, ws, f"finished, wrote {ws}/out.md")
    # the bridge/coordinator process is gone; a fresh connection (new dispatcher process) settles it
    with kbc.connect() as conn2:
        kbd.dispatch_once(conn2, max_spawn=0, spawn_fn=lambda *a, **k: None)
        assert _status(conn2, tid) == "review"


def test_declared_contract_miss_settles_as_partial_not_review(kanban_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "report.md").write_text("tiny")
    with kbc.connect() as conn:
        tid, run_id = _running(conn)
        ks.declare_artifact_contract(conn, tid, [{"name": "report.md", "min_bytes": 500}])
        _bridge_result(tid, run_id, ws, f"done, see {ws}/report.md")
        kbd.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None)
        assert _status(conn, tid) == "ready"  # retryable partial, not Review
        detail = json.loads(_events(conn, tid, "partial_result_detail")[0]["payload"])
        assert detail["unmet"] and detail["next_owner"] == "worker"
        assert not _events(conn, tid, "review_requested")


def test_unowned_bridge_transition_is_refused(kanban_home, tmp_path):
    with kbc.connect() as conn:
        tid, run_id = _running(conn)
        with pytest.raises(bridge.BridgeError):
            bridge._mark_card(tid, None, run_id, True, "x", workspace_dir=str(tmp_path), agent_name="native-1")
        assert _status(conn, tid) == "running"
