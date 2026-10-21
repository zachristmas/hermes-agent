"""A manually claimed run finishes through the executor it was bound to, not through someone remembering to settle it.

A run the dispatcher spawned is closed by its bridge process (``kanban_direct_herdr_worker``: a run-scoped
report, then a durable result receipt). A run a coordinator claimed by hand has no such process: its
Herdr executor can finish and the run stays Running under a TTL claim (connect t_9a22c463, run 100:
the adopted pane was still fenced to ``run-99``, no run-scoped report existed, nothing recorded a
result). Settling it from the pane alone would trust a stale or foreign agent, so completion is only
accepted from an executor the claimant explicitly bound to THIS run:

1. :func:`bind_manual_executor` records which live agent (name, native session, completion count at bind
   time) is working the run. Fenced by the current run, refused for a dispatcher-spawned run, one bind per
   run, idempotent for the same agent.
2. :func:`reconcile_manual_executors` runs in the dispatcher sweep. When the bound agent is the same native
   session, carries the card's tags, is settled (``idle``/``done``) and has completed MORE turns than at bind
   time, it records the run's durable ``worker_result_receipt``. Later sweeps and a restarted dispatcher
   settle from that receipt exactly as for a bridge run, and the existing notifier delivers the resulting
   event to the card's origin with its cursor, so the handoff is durable, acknowledged and idempotent.

Never retried, never a business decision: a run-scoped final report is judged by the bridge's own
``_mark_card``; without one the receipt is ``needs_input`` (a person decides), never a transient failure
the dispatcher would re-run. A missing or different agent is no evidence either way and changes nothing.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

SETTLED_STATUSES = ("idle", "done")
BOUND_EVENT = "executor_bound"


def _kb():
    from hermes_cli import kanban_db
    return kanban_db


def _event(conn, task_id: str, kind: str, run_id: int):
    return conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? AND run_id = ? "
        "ORDER BY id DESC LIMIT 1", (task_id, kind, run_id)).fetchone()


def _is_manual_open_run(conn, task_id: str, run_id: int) -> bool:
    row = conn.execute(
        "SELECT t.status, t.current_run_id, r.worker_pid FROM tasks t "
        "JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "running" or row["current_run_id"] != int(run_id):
        return False
    return row["worker_pid"] is None and _event(conn, task_id, "spawned", int(run_id)) is None


def _native_session(info: dict) -> Optional[str]:
    session = info.get("agent_session")
    value = session.get("value") if isinstance(session, dict) else None
    return str(value) if value else None


def bind_manual_executor(conn, task_id: str, run_id: int, *, agent: str, native_session: str,
                         completion_seq: int, board: Optional[str] = None) -> str:
    """Record the executor of a manually claimed run. Returns ``bound``, ``already_bound``,
    ``not_manual`` (no such open manual run: a dispatcher run settles itself) or ``conflict`` (another
    agent already holds this run's bind)."""
    kb = _kb()
    with kb.write_txn(conn):
        if not _is_manual_open_run(conn, task_id, int(run_id)):
            return "not_manual"
        existing = _event(conn, task_id, BOUND_EVENT, int(run_id))
        if existing is not None:
            bound = json.loads(existing["payload"])
            same = bound.get("agent") == agent and bound.get("native_session") == native_session
            return "already_bound" if same else "conflict"
        kb._append_event(conn, task_id, BOUND_EVENT, {
            "agent": agent, "native_session": native_session, "completion_seq": int(completion_seq),
            "board": board, "bound_at": int(time.time()),
        }, run_id=int(run_id))
    return "bound"


def _bound_runs(conn) -> list:
    return conn.execute(
        "SELECT t.id AS task_id, t.current_run_id AS run_id, t.workspace_path FROM tasks t "
        "WHERE t.status = 'running' AND t.current_run_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM task_events b WHERE b.task_id = t.id AND b.run_id = t.current_run_id "
        "AND b.kind = ?) "
        "AND NOT EXISTS (SELECT 1 FROM task_events r WHERE r.task_id = t.id AND r.run_id = t.current_run_id "
        "AND r.kind = 'worker_result_receipt')", (BOUND_EVENT,)).fetchall()


def _finished_after_bind(info: Optional[dict], bound: dict, task_id: str, board: Optional[str]) -> bool:
    if info is None or info.get("agent_status") not in SETTLED_STATUSES:
        return False
    tokens = info.get("tokens") or {}
    if tokens.get("kanban_task") != task_id or (board and tokens.get("kanban_board") != board):
        return False
    if _native_session(info) != bound.get("native_session"):
        return False
    try:
        return int(info.get("completion_seq")) > int(bound.get("completion_seq"))
    except (TypeError, ValueError):
        return False


def reconcile_manual_executors(
    conn, *, board: Optional[str], agent_info: Callable[[str], Optional[dict]],
    record: Optional[Callable[..., tuple]] = None,
) -> list[str]:
    """Record the result receipt of every bound manual run whose executor has finished. Returns the
    ids of the tasks it recorded for. Isolated per task: one failing lookup never stops the sweep."""
    from hermes_cli import kanban_direct_herdr_report as run_report
    from hermes_cli import kanban_direct_herdr_worker as bridge

    recorded: list[str] = []
    for row in _bound_runs(conn):
        task_id, run_id = row["task_id"], int(row["run_id"])
        try:
            bound = json.loads(_event(conn, task_id, BOUND_EVENT, run_id)["payload"])
            if not _is_manual_open_run(conn, task_id, run_id):
                continue
            try:
                info = agent_info(str(bound.get("agent") or ""))
            except Exception:  # health: allow BLE001 -- Herdr unreachable is no evidence; try again next sweep
                _kb()._log.debug("manual executor lookup failed for %s", task_id, exc_info=True)
                continue
            if not _finished_after_bind(info, bound, task_id, board or bound.get("board")):
                continue
            agent = str(bound["agent"])
            _, report_path = run_report.fence_paths(task_id, board or bound.get("board"), f"run-{run_id}")
            if run_report.read_final_report(report_path) is not None:
                ok, _ = (record or bridge._mark_card)(
                    task_id, board or bound.get("board"), run_id, True, "",
                    workspace_dir=row["workspace_path"] or "", settlement_owner=True,
                    agent_name=agent, report_path=report_path)
            else:
                ok, _ = bridge._record_receipt(
                    task_id, board or bound.get("board"), run_id, False,
                    f"Executor {agent} finished (completion {info.get('completion_seq')}) a run claimed by "
                    f"hand, but no run-scoped final report exists at {report_path}. Nothing is retried: "
                    f"read the executor's output and settle this run.",
                    (), "needs_input", False)
            if ok:
                recorded.append(task_id)
        except Exception:  # health: allow BLE001 -- one task's lookup must not stop the board's sweep
            _kb()._log.exception("manual executor reconcile failed for %s", task_id)
    return recorded
