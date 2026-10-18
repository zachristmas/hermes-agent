"""Summary correction for an idle card, split out of ``kanban_db.py`` to keep that facade under its
line cap -- behaviour, not a re-export shim.

``request_review`` refuses a card already in review and ``edit_task`` only backfills ``done``
results without keeping the old text, so a summary saved wrong (a raw terminal capture, say) had
no audited fix.
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def edit_summary(
    conn: sqlite3.Connection, task_id: str, *, summary: str, editor: Optional[str] = None,
    board: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Correct the summary a card shows (its newest non-empty run summary) without moving it.

    Works on any idle card (review, done, blocked, ...). Rewrites ONLY that run's summary, plus a
    done card's ``result`` when it held the same text. Status, assignee, claim, runs and earlier
    events stay as they are; the replaced text is kept verbatim in a ``summary_edited`` event. A
    card with any live or open run (claim lock, current run, worker pid) is refused: a worker or
    reviewer still owns it and may write its own handoff. Returns ``(ok, reason)``.
    """
    from hermes_cli import kanban_db as _kb

    summary = str(_kb.redact_review_value(summary or "")).strip()
    if not summary:
        return False, "summary is required"
    fields = ["summary"]
    with _kb.write_txn(conn):
        trow = conn.execute(
            "SELECT status, result, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if trow is None:
            return False, "task not found"
        if trow["claim_lock"] is not None or trow["current_run_id"] is not None or trow["worker_pid"]:
            return False, "task still has an active claim or run; wait for it to finish or reclaim it first"
        # Same ordering as kanban_db.latest_summary: the run whose summary the card displays.
        run = conn.execute(
            "SELECT id, summary FROM task_runs WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
            "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if run is None:
            return False, "task has no run summary to correct"
        conn.execute("UPDATE task_runs SET summary = ? WHERE id = ?", (summary, run["id"]))
        payload = {
            "summary": _kb._first_line(summary, 400) or None, "previous_summary": run["summary"],
            "status": trow["status"], "editor": editor or None,
        }
        if trow["status"] == "done" and trow["result"] and trow["result"].strip() == run["summary"].strip():
            conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (summary, task_id))
            payload["previous_result"] = trow["result"]
            fields.append("result")
        _kb._append_event(conn, task_id, "summary_edited", payload, run_id=int(run["id"]))
    _kb.notify_task_updated(conn, task_id, fields, board=board)
    return True, None
