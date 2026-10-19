"""Run-failure accounting and the circuit breaker (``_record_task_failure``) plus the error
grouping it feeds (``_error_fingerprint``), split out of ``kanban_db_dispatch.py`` to keep that
facade under its line cap. Facade names
(``_kb``, ``DEFAULT_FAILURE_LIMIT``) are late-imported inside the function: this module loads
mid-way through ``kanban_db_dispatch``, before either facade is complete. Callers and patches
keep using ``kanban_db_dispatch._record_task_failure``.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group."""
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int | None = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
    infrastructure: bool = False,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a non-success outcome and maybe trip the circuit breaker; every
    non-success path funnels through here so ``consecutive_failures`` stays
    consistent. Returns True when the task was auto-blocked.

    ``release_claim=True, end_run=True``: spawn-failure path (task still
    running with an open run — restore source phase or ``blocked``, release
    claim, close run). Both False: timeout/crash path (caller already restored
    the phase and closed the run; only the counter moves, a trip flips to
    ``blocked`` + ``gave_up``). Threshold: per-task ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``. ``force_trip`` trips
    unconditionally (caller applied its own bounded-retry policy).

    ``infrastructure=True``: the host refused the spawn (no restart-safe scope,
    #114720) — nothing about the card ran, so the run and event are recorded
    with ``infrastructure: true`` but ``consecutive_failures`` is left alone and
    the breaker never trips; the card stays retryable and
    :func:`check_respawn_guard` spaces the retries.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_db_dispatch import DEFAULT_FAILURE_LIMIT

    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    error = error[:500]
    failure_run_id = expected_run_id
    with _kb.write_txn(conn):
        where = "WHERE id = ?"
        params: tuple[Any, ...] = (task_id,)
        if expected_run_id is not None:
            where += " AND status = 'running' AND current_run_id = ?"
            params = (task_id, int(expected_run_id))
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, current_run_id "
            "FROM tasks " + where, params,
        ).fetchone()
        if row is None:
            return False
        retry_status = (
            _kb._retry_status_for_run(conn, task_id, row["current_run_id"])
            if release_claim
            else ("review" if row["status"] == "review" else "ready")
        )
        failures = int(row["consecutive_failures"]) + (0 if infrastructure else 1)

        # Per-task override wins over caller-supplied and default thresholds.
        task_override = _kb._row_get(row, "max_retries")
        if task_override is not None:
            effective_limit, limit_source = int(task_override), "task"
        else:
            effective_limit, limit_source = int(failure_limit), "dispatcher"

        if infrastructure or not (force_trip or failures >= effective_limit):
            if release_claim:
                # Restore the claimed source phase + clear only this fenced run's claim.
                claim_guard = " AND current_run_id = ?" if expected_run_id is not None else ""
                claim_params = (retry_status, failures, error, task_id, int(expected_run_id)) if expected_run_id is not None else (retry_status, failures, error, task_id)
                claim_cursor = conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'" + claim_guard,
                    claim_params,
                )
                if expected_run_id is not None and claim_cursor.rowcount != 1:
                    return False
            else:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error, task_id),
                )
            # Timeout/crash path's caller already emitted its own event.
            if end_run:
                open_run = _kb._current_run_id(conn, task_id)
                if expected_run_id is not None and open_run != int(expected_run_id):
                    return False
                detail = {"failures": failures, "retry_status": retry_status}
                if infrastructure:
                    detail["infrastructure"] = True
                failure_run_id = _kb._end_run(
                    conn, task_id, outcome=outcome, status=outcome, error=error, metadata=detail,
                )
                _kb._append_event(conn, task_id, outcome, {"error": error, **detail}, run_id=failure_run_id)
            return False

        # Spawn path (release_claim) is still running and also clears claim
        # state; the timeout/crash path already did.
        failure_run_id = None
        claim_guard = " AND current_run_id = ?" if expected_run_id is not None else ""
        claim_params = (failures, error, task_id, int(expected_run_id)) if expected_run_id is not None else (failures, error, task_id)
        updated = conn.execute(
            "UPDATE tasks SET status = 'blocked', "
            + ("claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
               if release_claim else "")
            + "consecutive_failures = ?, last_failure_error = ? "
            "WHERE id = ? AND status IN ('running', 'ready', 'review')" + claim_guard,
            claim_params,
        )
        if expected_run_id is not None and updated.rowcount != 1:
            return False  # fenced settle lost the run (claim moved on): write nothing
        payload = {
            "failures": failures,
            "effective_limit": effective_limit,
            "limit_source": limit_source,
            "error": error,
            "trigger_outcome": outcome,
            "retry_status": retry_status,
        }
        if end_run:
            # Only the spawn path has an open run to close.
            failure_run_id = _kb._end_run(
                conn, task_id, outcome="gave_up", status="gave_up", error=error,
                metadata={
                    "failures": failures,
                    "trigger_outcome": outcome,
                    "effective_limit": effective_limit,
                    "limit_source": limit_source,
                    "retry_status": retry_status,
                },
            )
        if force_trip:
            # The caller applied its own bounded policy, so the counter cannot
            # judge this block: ``recompute_ready`` holds it for an operator.
            payload["sticky"] = True
        if event_payload_extra:
            payload.update(event_payload_extra)
        # Retry exhaustion is a coordination problem, not a business block (t_00c0e992): queue a
        # durable owner/coordinator reconciliation with the evidence and one explicit next action,
        # in this same transaction so a restart can never leave the card Blocked without it.
        from hermes_cli import kanban_settlement
        assignee = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
        queued = kanban_settlement.queue_reconciliation(
            conn, task_id, failure_run_id if failure_run_id is not None else expected_run_id,
            trigger=outcome, error=error, assignee=assignee["assignee"] if assignee else None)
        payload["reconciliation"] = queued["next_action"]
        _kb._append_event(conn, task_id, "gave_up", payload, run_id=failure_run_id)
        return True
