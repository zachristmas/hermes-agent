"""Handoff-summary correction for an idle ``review`` card, split out of ``kanban_db.py`` to keep
that facade under its line cap -- behaviour, not a re-export shim.

``request_review`` refuses a card already in review and ``edit_task`` only backfills ``done``
cards, so a summary saved wrong (a raw terminal capture, say) had no supported fix.
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def edit_review_summary(
    conn: sqlite3.Connection, task_id: str, *, summary: str, board: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Correct the handoff summary of an idle ``review`` card without moving it.

    Rewrites ONLY the latest ``review_requested`` run's summary. Status, assignee, claim, runs and
    earlier events stay as they are; the replaced text is kept verbatim in a
    ``review_summary_edited`` event. A card with any live or open run (claim lock, current run,
    worker pid) is refused: a worker or reviewer still owns it and may write its own handoff.
    Returns ``(ok, reason)``.
    """
    from hermes_cli import kanban_db as _kb

    summary = str(_kb.redact_review_value(summary or "")).strip()
    if not summary:
        return False, "summary is required"
    with _kb.write_txn(conn):
        trow = conn.execute(
            "SELECT status, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if trow is None:
            return False, "task not found"
        if trow["status"] != "review":
            return False, (
                f"task is {trow['status']}, not review; only an idle review card's summary "
                f"can be corrected"
            )
        if trow["claim_lock"] is not None or trow["current_run_id"] is not None or trow["worker_pid"]:
            return False, "task still has an active claim or run; wait for it to finish or reclaim it first"
        run = conn.execute(
            "SELECT id, summary FROM task_runs WHERE task_id = ? AND outcome = 'review_requested' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if run is None:
            return False, "task has no review handoff run to correct"
        conn.execute("UPDATE task_runs SET summary = ? WHERE id = ?", (summary, run["id"]))
        _kb._append_event(
            conn, task_id, "review_summary_edited",
            {"summary": _kb._first_line(summary, 400) or None, "previous_summary": run["summary"]},
            run_id=int(run["id"]),
        )
    _kb.notify_task_updated(conn, task_id, ["summary"], board=board)
    return True, None
