"""Lifecycle enforcement for kanban attempts: recovery holds, operator pause,
receipt settlement, declared-artifact contracts, and trusted-human acceptance.

Task status and run state are distinct. A RUN is either terminal, or it has a
verified live owner under a bounded renewable lease, or (owner/execution
unknown) it sits in a durable, visible *recovery hold*: the run stays open and
fenced (never requeued, never killed blind) until evidence or an operator
resolves it. Everything here persists as ``task_events`` / ``task_refs`` rows,
which the existing notifier already tails with a per-subscription cursor, so a
lost notifier or a dead coordinator loses nothing and every function is
idempotent per run.

Late-bound ``_kb`` access (import-cycle breaking), same convention as the other
``kanban_db_*`` modules.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# An unresolved owner receipt may hold a run this long before the hold is
# escalated (a second, louder durable event); the run is still never auto-freed.
OWNER_HOLD_ESCALATE_SECONDS = 3600
# Settlement attempts that fail for the same run before the failure itself
# becomes a durable visible outcome instead of a silent retry loop.
SETTLE_ERROR_BUDGET = 3
ARTIFACT_CONTRACT_REF = "review_artifact_contract"
# What _review_summary returns for a report that only hands the work off.
HANDOFF_ONLY_SUMMARY = "Worker handoff only. Requested deliverable remains incomplete."
ACCEPTANCE_MAX_AGE_SECONDS = 600
# A card waiting in ``scheduled`` for an external event is re-checked read-only at most this many
# times, this far apart, before the wait is handed to the coordinator as a diagnostic.
RECHECK_DEFAULT_INTERVAL_SECONDS = 1800
RECHECK_DEFAULT_MAX_CHECKS = 8
# Executor problems in a row (no clean recheck between) before automatic rechecks stop.
EXECUTOR_PROBLEM_LIMIT = 2
WAIT_EXTERNAL = "external_event"
# An open coordinator reconciliation (retries exhausted) is escalated once after this long.
RECONCILE_ESCALATE_SECONDS = 4 * 3600


def _kb():
    from hermes_cli import kanban_db
    return kanban_db


def _json(value: Any) -> dict:
    try:
        parsed = json.loads(value) if isinstance(value, (str, bytes)) else value
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _run_event(conn, task_id: str, kind: str, run_id: Optional[int]):
    return conn.execute(
        "SELECT id, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = ? AND run_id IS ? ORDER BY id DESC LIMIT 1",
        (task_id, kind, run_id),
    ).fetchone()


# --------------------------------------------------------------------------- #
# 1. Recovery hold: unknown owner/execution fails closed, visibly and durably.
# --------------------------------------------------------------------------- #

def _owner_description(conn, task_id: str) -> str:
    row = conn.execute(
        "SELECT e.payload FROM task_events e JOIN tasks t ON t.current_run_id=e.run_id "
        "WHERE t.id=? AND e.task_id=t.id AND e.kind='owner_admitted' ORDER BY e.id DESC LIMIT 1",
        (task_id,)).fetchone()
    if row is None:
        return "no owner receipt recorded for this run"
    binding = _json(row["payload"])
    return f"native session {binding.get('session_id', '?')} (pid {binding.get('pid', '?')})"


def record_recovery_hold(conn, task_id: str, *, reason: str, now: Optional[int] = None) -> dict:
    """Idempotently record that this task's open run has an unknown owner.

    Appends one ``recovery_hold`` event per run (reason, owner, resolution
    condition) and, once the hold outlives ``OWNER_HOLD_ESCALATE_SECONDS``, one
    ``recovery_hold_escalated`` event. Never changes status, claim or run: a
    possibly executing worker is not relabelled as stopped.
    """
    kb = _kb()
    now = int(now if now is not None else time.time())
    out = {"recorded": False, "escalated": False}
    with kb.write_txn(conn):
        run_id = kb._current_run_id(conn, task_id)
        if run_id is None:
            return out
        hold = _run_event(conn, task_id, "recovery_hold", run_id)
        if hold is None:
            kb._append_event(conn, task_id, "recovery_hold", {
                "reason": reason,
                "owner": _owner_description(conn, task_id),
                "resolution": (
                    "Confirm the owner is gone (or alive) and call resolve_recovery_hold / "
                    "`hermes kanban recovery-resolve`; the run stays fenced until then."
                ),
                "first_seen": now,
            }, run_id=run_id)
            out["recorded"] = True
            return out
        first_seen = int(_json(hold["payload"]).get("first_seen") or hold["created_at"] or now)
        if now - first_seen >= OWNER_HOLD_ESCALATE_SECONDS and \
                _run_event(conn, task_id, "recovery_hold_escalated", run_id) is None:
            kb._append_event(conn, task_id, "recovery_hold_escalated", {
                "reason": reason, "held_seconds": now - first_seen,
                "owner": _owner_description(conn, task_id),
                "resolution": "Operator decision required; the run is still fenced.",
            }, run_id=run_id)
            out["escalated"] = True
    return out


def owner_hold_or_continue(conn, task_id: str) -> bool:
    """Keep an unverified owner fenced until its exact current-run receipt is reconciled."""
    kb = _kb()
    from hermes_cli.kanban_owner_recovery import owner_reclaim_paused
    if not owner_reclaim_paused(conn, task_id):
        return False
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    run_id = int(row["current_run_id"]) if row and row["current_run_id"] is not None else None
    if run_id is not None and _run_event(conn, task_id, "worker_result_receipt", run_id) is not None:
        # A result exists, so do not count a stale-claim failure until the receipt boundary consumes it.
        return False
    try:
        record_recovery_hold(conn, task_id, reason="owner receipt missing or ambiguous at claim expiry")
    except sqlite3.Error:
        # The run stays fenced either way; only the visible hold row is missing, so keep the traceback.
        kb._log.exception("kanban: recovery hold for %s not recorded", task_id)
    return True


def resolve_recovery_hold(
    conn, task_id: str, *, decision: str, actor: str, evidence: str,
) -> bool:
    """Operator resolution of a recovery hold.

    ``owner_stopped`` (evidence required) closes the run as ``reclaimed`` and
    returns the task to its retry phase without touching failure counters.
    ``owner_alive`` renews the lease. Anything else is refused.
    """
    kb = _kb()
    if decision not in {"owner_stopped", "owner_alive"} or not (evidence or "").strip():
        return False
    now = int(time.time())
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] is None:
            return False
        run_id = int(row["current_run_id"])
        if _run_event(conn, task_id, "recovery_hold", run_id) is None:
            return False
        payload = {"decision": decision, "actor": actor, "evidence": evidence.strip()[:500]}
        if decision == "owner_alive":
            expires = now + kb._resolve_claim_ttl_seconds(None)
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (expires, task_id))
            kb._extend_run_claim(conn, task_id, expires)
            kb._append_event(conn, task_id, "recovery_hold_resolved", payload, run_id=run_id)
            return True
        retry = kb._retry_status_for_run(conn, task_id, run_id)
        cur = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "worker_started_at = NULL WHERE id = ? AND status = 'running' AND current_run_id = ?",
            (retry, task_id, run_id))
        if cur.rowcount != 1:
            return False
        ended = kb._end_run(conn, task_id, outcome="reclaimed", status="reclaimed",
                            error="recovery hold resolved: owner stopped (operator evidence)",
                            metadata=payload)
        kb._append_event(conn, task_id, "recovery_hold_resolved", payload, run_id=ended or run_id)
        return True


# --------------------------------------------------------------------------- #
# 2. Typed operator pause, outside failure and block-loop counters.
# --------------------------------------------------------------------------- #

def operator_pause(
    conn, task_id: str, *, reason: str, actor: str, resume_condition: str,
    signal_fn: Optional[Callable] = None,
) -> str:
    """Pause one task until an explicit :func:`operator_resume`.

    Returns ``paused``, ``already_paused``, ``pending_owner_evidence`` or
    ``refused``. A running task is only closed after stop evidence: if the owner
    is unknown or survives termination, the run stays open under a recorded
    ``operator_pause_pending`` event and :func:`reconcile_pending_pauses` retries
    on every tick; nothing is requeued or killed blind.
    """
    kb = _kb()
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli.kanban_owner_recovery import owner_reclaim_paused
    row = conn.execute(
        "SELECT status, block_kind, worker_pid, worker_started_at, claim_lock, current_run_id "
        "FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return "refused"
    if row["status"] == "blocked" and row["block_kind"] == "operator_pause":
        return "already_paused"
    if row["status"] not in ("running", "ready"):
        return "refused"
    run_id = row["current_run_id"]
    meta = {"actor": actor, "reason": reason, "resume_condition": resume_condition}
    if row["status"] == "running":
        survived = False
        if owner_reclaim_paused(conn, task_id):
            survived = True
        elif row["worker_pid"]:
            termination = kbd._terminate_reclaimed_worker(
                row["worker_pid"], row["claim_lock"], signal_fn=signal_fn,
                started_at=row["worker_started_at"])
            survived = kbd._worker_survived_termination(termination)
        if survived:
            with kb.write_txn(conn):
                if _run_event(conn, task_id, "operator_pause_pending", run_id) is None:
                    kb._append_event(conn, task_id, "operator_pause_pending", {
                        **meta, "waiting_for": "owner stop evidence",
                    }, run_id=run_id)
            return "pending_owner_evidence"
    if not kb.block_task(conn, task_id, reason=f"operator pause: {reason}",
                         kind="operator_pause", expected_run_id=run_id if row["status"] == "running" else None):
        return "refused"
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "operator_pause", meta)
    return "paused"


def reconcile_pending_pauses(conn, *, signal_fn: Optional[Callable] = None) -> list[str]:
    """Retry pauses that were waiting for owner stop evidence (restart-safe)."""
    settled: list[str] = []
    rows = conn.execute(
        "SELECT t.id, e.payload FROM tasks t JOIN task_events e "
        "ON e.task_id = t.id AND e.run_id = t.current_run_id AND e.kind = 'operator_pause_pending' "
        "WHERE t.status = 'running'").fetchall()
    for row in rows:
        meta = _json(row["payload"])
        outcome = operator_pause(
            conn, row["id"], reason=meta.get("reason", ""), actor=meta.get("actor", "operator"),
            resume_condition=meta.get("resume_condition", ""), signal_fn=signal_fn)
        if outcome == "paused":
            settled.append(row["id"])
    return settled


def operator_resume(conn, task_id: str, *, actor: str) -> bool:
    """Explicit resume of an operator-paused task; the only exit from the pause."""
    kb = _kb()
    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "blocked" or row["block_kind"] != "operator_pause":
        return False
    if not kb.unblock_task(conn, task_id):
        return False
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "operator_resume", {"actor": actor})
    return True


# --------------------------------------------------------------------------- #
# 3. Result receipts and deterministic settlement.
# --------------------------------------------------------------------------- #

def record_result_receipt(
    conn, task_id: str, run_id: Optional[int], *, success: bool, report: str,
    artifacts: Iterable[str] = (), block_kind: str = "transient",
    require_artifacts: bool = False, unmet: Iterable[str] = (), closeout: Optional[dict] = None,
    follow_ups: Iterable[str] = (), blocker: Optional[dict] = None,
) -> bool:
    """Persist a finished attempt's result BEFORE any card transition.

    ``closeout``: ``{"progress": {...}, "executor_problem": str | None}``. The attempt is settled
    separately from the business task (see :func:`settle_closeout`): recorded progress survives an
    executor problem, and the card's status follows the work that remains.

    ``require_artifacts``: the producer (e.g. the Herdr bridge) asks settlement to hold back a
    success whose report is a handoff only (no files attached) as a partial result.
    ``unmet``: requested deliverables the worker's own final report says it did not deliver (a
    requested PR with no PR URL, say); settlement keeps the card with its owner as a partial
    instead of letting the rest of the work carry it to Review.
    ``follow_ups``: optional work outside the card's acceptance; recorded, never counted as unmet.
    ``blocker``: the validated typed prerequisite of a business block (``kanban_report_policy``).

    Idempotent per run. If the coordinator dies after this commit, the next
    sweep settles the card from the receipt (see :func:`reconcile_settlements`).
    """
    kb = _kb()
    if run_id is None:
        return False
    with kb.write_txn(conn):
        if _run_event(conn, task_id, "worker_result_receipt", int(run_id)) is not None:
            return False
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] != int(run_id):
            return False
        kb._append_event(conn, task_id, "worker_result_receipt", {
            "success": bool(success), "report": (report or "")[:8000],
            "artifacts": [str(a) for a in artifacts], "block_kind": block_kind,
            "require_artifacts": bool(require_artifacts), "unmet": [str(u) for u in unmet],
            "closeout": closeout or None, "follow_ups": [str(f) for f in follow_ups][:20],
            "blocker": blocker or None,
            "required_next_action": "human review" if success else "operator or retry per block kind",
        }, run_id=int(run_id))
    return True


def _settle_error(conn, task_id: str, run_id: int, reason: str) -> None:
    kb = _kb()
    with kb.write_txn(conn):
        errors = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND run_id=? AND kind='settlement_error'",
            (task_id, run_id)).fetchone()[0]
        kb._append_event(conn, task_id, "settlement_error", {"reason": reason[:300], "n": errors + 1},
                         run_id=run_id)
        if errors + 1 >= SETTLE_ERROR_BUDGET and \
                _run_event(conn, task_id, "settlement_escalated", run_id) is None:
            kb._append_event(conn, task_id, "settlement_escalated", {
                "reason": reason[:300], "attempts": errors + 1,
                "resolution": "Settle by hand: review the receipt event and use request_review/block.",
            }, run_id=run_id)


def settle_from_receipt(conn, task_id: str) -> Optional[str]:
    """Apply one run-scoped receipt using the lifecycle APIs' compare-and-swap fences."""
    kb = _kb()
    row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "running" or row["current_run_id"] is None:
        return None
    run_id = int(row["current_run_id"])
    if _run_event(conn, task_id, "settled_from_receipt", run_id) is not None:
        return "already_settled"
    receipt = _run_event(conn, task_id, "worker_result_receipt", run_id)
    if receipt is None:
        return None
    data = _json(receipt["payload"])
    report = str(data.get("report") or "")
    if isinstance(data.get("closeout"), dict) and data["closeout"].get("progress"):
        return settle_closeout(conn, task_id, run_id, data["closeout"], report)
    if data.get("success"):
        # A finished turn needs no file to reach Review: the summary can be the deliverable (board
        # edits, a merged PR). Only a handoff-only report is held back; a declared contract or a
        # claimed file is enforced by request_review below.
        if data.get("unmet"):
            return settle_partial(conn, task_id, run_id, summary=report.strip()[:1200],
                                  unmet=[str(u) for u in data["unmet"]])
        if data.get("require_artifacts") and not data.get("artifacts") and \
                report.strip().startswith(HANDOFF_ONLY_SUMMARY):
            return settle_partial(
                conn, task_id, run_id, summary=report.strip()[:1200],
                unmet=["worker reported a handoff only; the requested work is not done"])
        concise = (report or "").strip()
        if len(concise) > 1200:
            concise = concise[:1197].rstrip() + "..."
        ok, reason = kb.request_review(
            conn, task_id, summary=concise or "Verified deliverable ready for review.",
            metadata={"artifacts": list(data.get("artifacts") or [])},
            expected_run_id=run_id, with_reason=True)
        if ok:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, "settled_from_receipt", {
                    "disposition": "review", "next_action": "human review"}, run_id=run_id)
            return "review"
        if isinstance(reason, str) and reason.startswith(UNMET_PREFIX):
            return settle_partial(conn, task_id, run_id, summary=report, unmet=[reason])
        _settle_error(conn, task_id, run_id, reason or "request_review refused")
        return None
    kind = data.get("block_kind") if data.get("block_kind") in kb.VALID_BLOCK_KINDS else "transient"
    blocker = data.get("blocker") if isinstance(data.get("blocker"), dict) else None
    if blocker:
        report = f"{prerequisite_line(blocker)}\n\n{report}"
    if kb.block_task(conn, task_id, reason=report or "worker reported failure", kind=kind,
                     expected_run_id=run_id):
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "settled_from_receipt", {
                "disposition": "blocked", "prerequisite": blocker,
                "next_action": prerequisite_line(blocker) if blocker else f"resolve {kind} block"},
                run_id=run_id)
        return "blocked"
    _settle_error(conn, task_id, run_id, "block_task refused")
    return None


# --------------------------------------------------------------------------- #
# 3b. Closeout: settle the execution attempt separately from the business task.
# --------------------------------------------------------------------------- #

def _progress_line(progress: dict) -> str:
    """What finished, what remains, who owns the next action, what triggers it."""
    def joined(key: str) -> str:
        items = [str(i).strip() for i in (progress.get(key) or []) if str(i).strip()]
        return "; ".join(items) or "nothing"
    parts = [f"Finished: {joined('finished')}.", f"Remaining: {joined('remaining')}."]
    if progress.get("next_owner"):
        parts.append(f"Next action owner: {progress['next_owner']}.")
    if progress.get("trigger"):
        parts.append(f"Triggered by: {progress['trigger']}.")
    return " ".join(parts)[:1200]


def settle_closeout(conn, task_id: str, run_id: int, closeout: dict, report: str) -> Optional[str]:
    """Apply a closeout receipt: keep the task's recorded progress, file the executor problem on its
    own, and choose the card's status from the REMAINING work.

    - ``wait_kind == external_event`` (the first SAP order has not arrived): ``scheduled`` with a
      bounded read-only recheck (:func:`reconcile_rechecks`).
    - ``approval`` / ``prerequisite`` (a person must act): ``blocked`` as ``needs_input``.

    Neither path touches ``consecutive_failures`` or the block-loop counter: an executor that hit a
    push approval or wrote no report did not fail the task. The status transition closes the run,
    so completed work and its evidence stay on the card; nothing re-queues the card, which is what
    prevents a second production write.
    """
    kb = _kb()
    progress = dict(closeout.get("progress") or {})
    wait_kind = str(progress.get("wait_kind") or "")
    problem = str(closeout.get("executor_problem") or "").strip()
    summary = _progress_line(progress)
    if problem:
        summary = f"{summary} Executor problem (not a task blocker): {problem}"[:1500]
    with kb.write_txn(conn):
        row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] != run_id:
            return None
        if _run_event(conn, task_id, "progress_recorded", run_id) is None:
            kb._append_event(conn, task_id, "progress_recorded", {**progress, "summary": summary}, run_id=run_id)
        if problem and _run_event(conn, task_id, "executor_closeout_problem", run_id) is None:
            kb._append_event(conn, task_id, "executor_closeout_problem", {
                "problem": problem[:500], "diagnostics": closeout.get("diagnostics"),
                "task_status_unaffected": True}, run_id=run_id)
    if wait_kind == WAIT_EXTERNAL:
        recheck = dict(progress.get("recheck") or {})
        prev = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled_recheck' "
            "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        # A run that a recheck released continues the same bounded wait; any other run starts one.
        checks_done = 0
        if prev is not None and _event_count_after(conn, task_id, "recheck_started", prev["id"]):
            checks_done = int(_json(prev["payload"]).get("checks_done") or 0) + 1
        interval = max(int(recheck.get("interval_seconds") or RECHECK_DEFAULT_INTERVAL_SECONDS), 60)
        max_checks = max(int(recheck.get("max_checks") or RECHECK_DEFAULT_MAX_CHECKS), 1)
        ok = kb.schedule_task(conn, task_id, reason=summary, expected_run_id=run_id)
        disposition = "scheduled"
        if ok:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, "scheduled_recheck", {
                    "next_at": int(time.time()) + interval, "interval_seconds": interval,
                    "max_checks": max_checks, "checks_done": checks_done,
                    "how": str(recheck.get("how") or "")[:400],
                    "read_only": True}, run_id=run_id)
    elif wait_kind == "approval" or isinstance(progress.get("blocker"), dict):
        # A real hold: the executor stopped at an approval dialog, or the report named a validated
        # typed prerequisite. Only these are business blocks.
        if isinstance(progress.get("blocker"), dict):
            summary = f"{prerequisite_line(progress['blocker'])} {summary}"[:1500]
        ok = kb.block_task(conn, task_id, reason=summary, kind="needs_input", expected_run_id=run_id)
        disposition = "blocked"
    else:
        # Remaining work with no named prerequisite is not a business block (t_00c0e992): settle it
        # as a bounded partial, so a repeat queues coordinator reconciliation instead of a Blocked
        # card nobody can act on. The recorded progress above keeps finished work from being redone.
        remaining = [str(r) for r in (progress.get("remaining") or [])] or ["remaining work not itemised"]
        if problem:
            remaining.append(f"executor problem: {problem}"[:300])
        return settle_partial(conn, task_id, run_id, summary=summary, unmet=remaining)
    if not ok:
        _settle_error(conn, task_id, run_id, f"closeout transition to {disposition} refused")
        return None
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "settled_from_receipt", {
            "disposition": disposition, "next_action": progress.get("next_owner") or "coordinator"},
            run_id=run_id)
    return disposition


def _event_count_after(conn, task_id: str, kind: str, after_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=? AND id>?",
        (task_id, kind, after_id)).fetchone()[0]


def _last_clean_progress_id(conn, task_id: str) -> int:
    """Newest ``progress_recorded`` whose run had no executor problem: the last clean closeout."""
    row = conn.execute(
        "SELECT p.id FROM task_events p WHERE p.task_id=? AND p.kind='progress_recorded' AND NOT EXISTS ("
        "SELECT 1 FROM task_events e WHERE e.task_id=p.task_id AND e.run_id IS p.run_id "
        "AND e.kind='executor_closeout_problem') ORDER BY p.id DESC LIMIT 1", (task_id,)).fetchone()
    return int(row[0]) if row else 0


def reconcile_rechecks(conn, now: Optional[int] = None) -> list[str]:
    """Release each due ``scheduled`` card for ONE bounded read-only recheck.

    The recheck run's context says finished work is recorded and must not be repeated (see
    ``_ctx_recorded_progress``). Two bounds, each raised once as a coordinator diagnostic while the
    card stays ``scheduled``: ``max_checks`` rechecks, or ``EXECUTOR_PROBLEM_LIMIT`` executor
    problems in a row. Never moves a card to ``triage`` or ``blocked``.
    """
    kb = _kb()
    now = int(now if now is not None else time.time())
    released: list[str] = []
    for row in conn.execute("SELECT id FROM tasks WHERE status='scheduled'").fetchall():
        task_id = row["id"]
        spec = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled_recheck' "
            "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        if spec is None:
            continue
        data = _json(spec["payload"])
        interval = int(data.get("interval_seconds") or RECHECK_DEFAULT_INTERVAL_SECONDS)
        last = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='recheck_started' AND id>? "
            "ORDER BY id DESC LIMIT 1", (task_id, spec["id"])).fetchone()
        due_at = int(_json(last["payload"]).get("at") or 0) + interval if last else int(data.get("next_at") or 0)
        if now < due_at:
            continue
        max_checks = int(data.get("max_checks") or RECHECK_DEFAULT_MAX_CHECKS)
        # Rechecks already used by this wait, and executor problems since the last clean closeout.
        started = int(data.get("checks_done") or 0)
        problems = _event_count_after(conn, task_id, "executor_closeout_problem",
                                      _last_clean_progress_id(conn, task_id))
        stop = None
        if problems >= EXECUTOR_PROBLEM_LIMIT:
            stop = ("recurring_executor_problem",
                    f"{problems} executor problems in a row; automatic rechecks stopped")
        elif started >= max_checks:
            stop = ("recheck_exhausted",
                    f"{started} read-only rechecks done; the awaited event has not occurred")
        if stop:
            seen = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND kind='block_loop_detected' AND id>? "
                "AND payload LIKE ?", (task_id, spec["id"], f"%{stop[0]}%")).fetchone()
            if seen is None:
                with kb.write_txn(conn):
                    kb._append_event(conn, task_id, "block_loop_detected", {
                        "kind": stop[0], "reason": stop[1], "status": "scheduled",
                        "retries_stopped": True, "recurrences": max(problems, started)})
            continue
        if kb.unblock_task(conn, task_id):
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, "recheck_started", {
                    "n": started + 1, "of": max_checks, "read_only": True, "at": now})
            released.append(task_id)
    return released


# --------------------------------------------------------------------------- #
# 3c. Coordinator reconciliation: retries exhausted, never a silent Blocked.
# --------------------------------------------------------------------------- #

def prerequisite_line(blocker: dict) -> str:
    return (f"Prerequisite ({blocker.get('kind', '?')}): {blocker.get('needs', '?')} "
            f"(owner: {blocker.get('owner', '?')}).")


# Event kind -> (hold slot, label opening it, or None when the event closes it).
_HOLD_EVENTS = {
    "recovery_hold": ("recovery", "recovery hold on run {run_id} (uncertain effect)"),
    "recovery_hold_resolved": ("recovery", None),
    "operator_pause": ("pause", "operator pause"),
    "operator_resume": ("pause", None),
}


def _open_holds(conn, task_id: str) -> list[str]:
    """Holds a reconciliation must not bypass: unresolved recovery holds and operator pauses."""
    marks = ", ".join("?" * len(_HOLD_EVENTS))
    rows = conn.execute(
        f"SELECT kind, run_id FROM task_events WHERE task_id=? AND kind IN ({marks}) ORDER BY id",
        (task_id, *_HOLD_EVENTS)).fetchall()
    open_holds: dict[str, str] = {}
    for row in rows:
        slot, label = _HOLD_EVENTS[row["kind"]]
        if label is None:
            open_holds.pop(slot, None)
        else:
            open_holds[slot] = label.format(run_id=row["run_id"])
    return list(open_holds.values())


def queue_reconciliation(conn, task_id: str, run_id: Optional[int], *, trigger: str, error: str,
                         assignee: Optional[str]) -> dict:
    """Record, inside the caller's write transaction, that automatic retries are exhausted and the
    owner/coordinator must reconcile. The payload preserves the evidence (recent attempts, their
    unmet items and summaries, open holds) and states one explicit next action. Nothing here
    re-queues or releases the card, so no hold is bypassed and no retry loop starts."""
    kb = _kb()
    attempts = [
        {"run_id": r["id"], "outcome": r["outcome"], "error": (r["error"] or "")[:300]}
        for r in conn.execute(
            "SELECT id, outcome, error FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 4", (task_id,)).fetchall()]
    if run_id is not None and all(a["run_id"] != run_id for a in attempts):
        attempts.insert(0, {"run_id": run_id, "outcome": "gave_up", "error": error[:300]})
    details = [_json(r["payload"]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='partial_result_detail' "
        "ORDER BY id DESC LIMIT 2", (task_id,)).fetchall()]
    unmet = sorted({str(u) for d in details for u in (d.get("unmet") or [])})[:10]
    holds = _open_holds(conn, task_id)
    if trigger == "partial_result":
        action = ("Compare the unmet items with the card's acceptance criteria: accept the latest "
                  "result to Review if they are optional follow-ups, or post the corrected "
                  "instruction and unblock once if they are required.")
    else:
        action = ("Read the attempts' errors, fix the executor cause (or reassign), then unblock "
                  "once; do not unblock blind.")
    if holds:
        action = "Resolve the open hold first (" + "; ".join(holds) + "). Then: " + action
    payload = {
        "trigger": trigger, "owner": assignee or "unassigned", "coordinator": "kanban coordinator",
        "next_action": action, "attempts": attempts, "unmet": unmet,
        "partial_summary": next((d.get("partial_summary") for d in details if d.get("partial_summary")), None),
        "holds": holds, "error": error[:500], "state": "open",
    }
    kb._append_event(conn, task_id, "reconciliation_queued", payload, run_id=run_id)
    return payload


def open_reconciliations(conn) -> list[dict]:
    """Blocked cards whose newest reconciliation is still open (no unblock or reassignment since)."""
    out = []
    rows = conn.execute(
        "SELECT t.id, e.id AS ev_id, e.payload, e.created_at FROM tasks t JOIN task_events e "
        "ON e.task_id=t.id AND e.kind='reconciliation_queued' WHERE t.status='blocked' AND e.id = ("
        "SELECT MAX(id) FROM task_events WHERE task_id=t.id AND kind='reconciliation_queued')").fetchall()
    for row in rows:
        later = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND id>? AND kind IN "
            "('unblocked', 'assigned')", (row["id"], row["ev_id"])).fetchone()
        if later is None:
            out.append({"task_id": row["id"], "event_id": row["ev_id"], "queued_at": row["created_at"],
                        **_json(row["payload"])})
    return out


def reconcile_reconciliations(conn, now: Optional[int] = None) -> list[str]:
    """Escalate, once each, reconciliations still open after ``RECONCILE_ESCALATE_SECONDS``. The
    queue lives in ``task_events``, so it survives a gateway restart; this sweep is idempotent."""
    kb = _kb()
    now = int(now if now is not None else time.time())
    escalated = []
    for item in open_reconciliations(conn):
        if now - int(item["queued_at"] or now) < RECONCILE_ESCALATE_SECONDS:
            continue
        with kb.write_txn(conn):
            seen = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND kind='reconciliation_escalated' AND id>?",
                (item["task_id"], item["event_id"])).fetchone()
            if seen is not None:
                continue
            kb._append_event(conn, item["task_id"], "reconciliation_escalated", {
                "open_seconds": now - int(item["queued_at"]), "owner": item.get("owner"),
                "next_action": item.get("next_action"), "holds": item.get("holds") or []})
        escalated.append(item["task_id"])
    return escalated


def reconcile_settlements(conn) -> list[str]:
    """Settle every running card whose CURRENT run has a durable result receipt, and complete any
    recorded partial-settle intent.

    Runs on every sweep, including paused-spawn mode, and at startup. Cards are isolated so one
    corrupt receipt cannot stop reconciliation for the board. It deliberately does NOT treat a
    running card without a receipt as stale: a missing receipt is not evidence the owner is gone,
    and expiry of such claims stays with the lease/owner-fence paths (release_stale_claims ->
    owner_hold_or_continue), which fail closed into a visible recovery hold.
    """
    settled: list[str] = reconcile_partial_intents(conn)
    reconcile_reconciliations(conn)
    rows = conn.execute(
        "SELECT DISTINCT t.id FROM tasks t JOIN task_events e "
        "ON e.task_id = t.id AND e.run_id = t.current_run_id AND e.kind = 'worker_result_receipt' "
        "WHERE t.status = 'running' AND t.current_run_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        task_id = row["id"]
        try:
            if settle_from_receipt(conn, task_id):
                settled.append(task_id)
        except Exception as exc:  # health: allow BLE001 -- recorded on the run via _settle_error; one bad receipt must not stop the sweep
            run_id = _kb()._current_run_id(conn, task_id)
            if run_id is not None:
                _settle_error(conn, task_id, run_id, f"{type(exc).__name__}: {exc}")
    return settled


def _complete_partial(conn, task_id: str, run_id: int, detail: dict) -> bool:
    """Second half of a partial settle: one fenced transaction that closes the run, releases the
    claim and records the outcome (``_record_task_failure``). Idempotent: once the run is closed
    the fence no longer matches."""
    from hermes_cli import kanban_db_dispatch as kbd
    unmet = [str(u) for u in (detail.get("unmet") or [])]
    kbd._record_task_failure(
        conn, task_id, error="Partial result (not accepted for Review): " + "; ".join(unmet)[:400],
        outcome="partial_result", release_claim=True, end_run=True,
        event_payload_extra=detail, expected_run_id=run_id)
    return True


def settle_partial(conn, task_id: str, run_id: int, *, summary: str, unmet: list[str]) -> str:
    """Close a partial attempt truthfully and preserve its corrective assignment.

    Two fenced transactions with a replayable intent between them (write_txn does not nest):
    1. record ``partial_settle_intent`` + ``partial_result_detail`` (unmet contract, next owner,
       summary) for this exact run;
    2. ``_complete_partial`` closes the run, releases the claim and records the outcome together.
    A crash between them leaves the run open WITH the intent; ``reconcile_partial_intents`` (every
    sweep, including paused-spawn mode) completes it, so the settle is never lost or half-applied.
    """
    kb = _kb()
    with kb.write_txn(conn):
        current = conn.execute("SELECT status, current_run_id, assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
        if current is None or current["status"] != "running" or current["current_run_id"] != run_id:
            return "stale"
        detail = {"unmet": unmet, "next_owner": current["assignee"],
                  "partial_summary": (summary or "")[:1000], "run_id": run_id}
        if _run_event(conn, task_id, "partial_settle_intent", run_id) is None:
            kb._append_event(conn, task_id, "partial_settle_intent", detail, run_id=run_id)
        if _run_event(conn, task_id, "partial_result_detail", run_id) is None:
            kb._append_event(conn, task_id, "partial_result_detail", detail, run_id=run_id)
    _complete_partial(conn, task_id, run_id, detail)
    return "partial"


def reconcile_partial_intents(conn) -> list[str]:
    """Complete every partial settle whose intent was recorded but whose run is still open."""
    done: list[str] = []
    rows = conn.execute(
        "SELECT t.id, t.current_run_id, e.payload FROM tasks t JOIN task_events e "
        "ON e.task_id = t.id AND e.run_id = t.current_run_id AND e.kind = 'partial_settle_intent' "
        "WHERE t.status = 'running'").fetchall()
    for row in rows:
        try:
            detail = _json(row["payload"])
            _complete_partial(conn, row["id"], int(row["current_run_id"]), detail)
            done.append(row["id"])
        except Exception as exc:  # health: allow BLE001 -- recorded on the run via _settle_error; one bad intent must not stop the sweep
            _settle_error(conn, row["id"], int(row["current_run_id"]), f"{type(exc).__name__}: {exc}")
    return done


# --------------------------------------------------------------------------- #
# 4. Declared artifact contracts at the shared Review/Done boundary.
# --------------------------------------------------------------------------- #

UNMET_PREFIX = "unmet artifact contract: "


def declare_artifact_contract(conn, task_id: str, items: list[dict]) -> int:
    """Declare required deliverables: ``{"name", "min_bytes", "must_contain", "json_keys"}``."""
    from hermes_cli.kanban_db_refs import add_task_ref
    count = 0
    for item in items:
        name = os.path.basename(str(item.get("name") or "").strip())
        if not name:
            raise ValueError("artifact contract item needs a file name")
        clean = {
            "name": name,
            "min_bytes": max(1, int(item.get("min_bytes") or 1)),
            "must_contain": [str(s) for s in item.get("must_contain") or []],
            "json_keys": [str(s) for s in item.get("json_keys") or []],
        }
        add_task_ref(conn, task_id, ARTIFACT_CONTRACT_REF, ref=json.dumps(clean, sort_keys=True))
        count += 1
    return count


def load_artifact_contract(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT ref FROM task_refs WHERE task_id = ? AND ref_type = ? ORDER BY id",
        (task_id, ARTIFACT_CONTRACT_REF)).fetchall()
    return [d for d in (_json(r["ref"]) for r in rows) if d.get("name")]


def check_artifact_contract(conn, task_id: str, artifact_paths: Iterable[str]) -> list[str]:
    """Return unmet criteria ([] = satisfied or no contract declared).

    Deterministic structure only: file identity (basename), existence, size,
    required substrings, required JSON keys. Semantic correctness stays with the
    human reviewer.
    """
    contract = load_artifact_contract(conn, task_id)
    if not contract:
        return []
    by_name: dict[str, list[Path]] = {}
    for raw in artifact_paths:
        p = Path(str(raw))
        by_name.setdefault(p.name, []).append(p)
    unmet: list[str] = []
    for item in contract:
        name = item["name"]
        candidates = [p for p in by_name.get(name, []) if p.is_file()]
        if not candidates:
            unmet.append(f"{name}: declared deliverable not delivered")
            continue
        problems: list[str] = []
        for path in candidates:
            problems = []
            if path.stat().st_size < item["min_bytes"]:
                problems.append(f"{name}: {path.stat().st_size} bytes < required {item['min_bytes']}")
            text = None
            if item["must_contain"] or item["json_keys"]:
                try:
                    text = path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    problems.append(f"{name}: unreadable")
            if text is not None:
                problems += [f"{name}: missing required text {s!r}" for s in item["must_contain"] if s not in text]
                if item["json_keys"]:
                    doc = _json(text)
                    problems += [f"{name}: missing JSON key {k!r}" for k in item["json_keys"] if k not in doc]
            if not problems:
                break
        unmet += problems
    return unmet


def refuse_unmet_contract(conn, task_id: str, unmet: list[str], *, boundary: str) -> str:
    """Record the refusal durably and return the reason string for the caller."""
    kb = _kb()
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "completion_refused_unmet_contract", {
            "boundary": boundary, "unmet": unmet[:20]})
    return UNMET_PREFIX + "; ".join(unmet)[:500]


# --------------------------------------------------------------------------- #
# 5. Trusted-human acceptance for Done on review-required boards.
# --------------------------------------------------------------------------- #

_ISSUER = object()


class HumanAcceptance:
    """Proof that a human authenticated for one acceptance.

    Only :func:`authenticate_passphrase` and :func:`dashboard_acceptance` can
    build one (the constructor requires this module's private issuer), no tool,
    env var, author label or kwarg a model controls produces it. The kernel
    still cannot defeat code that imports internals or writes SQLite directly:
    that boundary is OS-level and out of scope here.
    """

    def __init__(self, principal: str, source: str, *, _issuer: object = None):
        if _issuer is not _ISSUER:
            raise PermissionError("HumanAcceptance can only be issued by kanban_settlement")
        self.principal, self.source, self.issued_at = principal, source, int(time.time())


def make_verifier(passphrase: str) -> str:
    """``scrypt$salt$hash`` for ``kanban.human_acceptance.verifier`` (set by the human)."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def authenticate_passphrase(passphrase: str, verifier: str, principal: str = "operator") -> Optional[HumanAcceptance]:
    try:
        scheme, salt_hex, hash_hex = (verifier or "").split("$")
        if scheme != "scrypt":
            return None
        digest = hashlib.scrypt(passphrase.encode(), salt=bytes.fromhex(salt_hex),
                                n=2 ** 14, r=8, p=1, dklen=32)
        if hmac.compare_digest(digest.hex(), hash_hex):
            return HumanAcceptance(principal, "passphrase", _issuer=_ISSUER)
    except (ValueError, TypeError):
        pass
    return None


class PolicyError(RuntimeError):
    """The shared human-acceptance policy exists but cannot be trusted (callers fail closed)."""


def policy_path():
    """Canonical policy file: ``<kanban_home>/kanban/human_acceptance.json``.

    ``kanban_home()`` is shared across profiles by design (``HERMES_KANBAN_HOME`` else the default
    Hermes root), the same root that holds the boards and their databases, so every profile
    (default, CIC, CS, P5) reads the SAME policy. Caller-profile config is never consulted: an
    empty or different profile config cannot weaken it.
    """
    return _kb().kanban_home() / "kanban" / "human_acceptance.json"


def read_policy() -> dict:
    """``{"boards": [...], "verifier": "..."}``. Missing file = policy off ({}); an existing file
    that is unreadable, malformed or has the wrong shape raises :class:`PolicyError`."""
    path = policy_path()
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise PolicyError(f"acceptance policy unreadable: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PolicyError(f"acceptance policy is not valid JSON: {exc}") from exc
    boards, verifier = (data.get("boards", []), data.get("verifier", "")) if isinstance(data, dict) else (None, None)
    if not isinstance(boards, list) or not all(isinstance(b, str) and b for b in boards) or not isinstance(verifier, str):
        raise PolicyError("acceptance policy has the wrong shape (need {boards: [str], verifier: str})")
    return {"boards": boards, "verifier": verifier}


def write_policy(boards: list, verifier: str) -> dict:
    """Atomically replace the shared policy file (owner-only mode). Operator command only."""
    data = {"boards": sorted({str(b) for b in boards}), "verifier": str(verifier or "")}
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    return data


def _acceptance_config() -> dict:
    """The one policy source (seam kept for tests): the shared file, never profile config."""
    return read_policy()


def board_of_connection(conn) -> Optional[str]:
    """Board slug this connection is bound to, from its database FILE (not the ambient board).

    ``<root>/kanban.db`` is ``default``; ``<root>/kanban/boards/<slug>/kanban.db`` is ``<slug>``.
    Anything else (in-memory, custom path, error) is unknown: ``None``.
    """
    try:
        from pathlib import Path
        kb = _kb()
        main = next((r for r in conn.execute("PRAGMA database_list").fetchall() if r[1] == "main"), None)
        if main is None or not main[2]:
            return None
        file = Path(main[2]).resolve()
        if file == (kb.kanban_home() / "kanban.db").resolve():
            return kb.DEFAULT_BOARD
        if file.name == "kanban.db" and file.parent.parent == kb.boards_root().resolve():
            return file.parent.name
    except Exception:  # health: allow BLE001 -- unknown board identity is the fail-closed answer callers handle
        return None
    return None


def acceptance_required(board: Optional[str]) -> bool:
    """True when this board requires trusted-human Done.

    Fails closed: an unreadable policy requires acceptance, and so does an UNKNOWN board
    identity (``board is None``) whenever any board is protected. Never falls back to the
    ambient board.
    """
    try:
        boards = _acceptance_config().get("boards") or []
    except Exception:  # health: allow BLE001 -- unreadable policy fails closed (acceptance required)
        return True
    if "*" in boards:
        return True
    if not boards:
        return False
    return board is None or board in boards


def dashboard_acceptance(*, gated_session: bool = False, passphrase: Optional[str] = None) -> Optional[HumanAcceptance]:
    """Acceptance for a dashboard request, derived from server-side request context only.

    ``gated_session`` is set by the dashboard route solely when the OAuth gate verified a
    provider session for this request. A loopback session token and the same-user native
    principal are NOT human proof (any local process can fetch the token from the SPA HTML),
    so on those the request must carry the acceptance passphrase, checked against the
    configured verifier. Anything else returns None and the gate refuses. The old global
    ``dashboard_session_trusted`` option is ignored.
    """
    if gated_session is True:
        return HumanAcceptance("dashboard-oauth-session", "dashboard", _issuer=_ISSUER)
    try:
        verifier = str(_acceptance_config().get("verifier") or "")
    except Exception:  # health: allow BLE001 -- unreadable policy fails closed (no acceptance)
        return None
    if passphrase and verifier:
        return authenticate_passphrase(passphrase, verifier, principal="dashboard-passphrase")
    return None


def verify_acceptance(acceptance: Any) -> bool:
    return (isinstance(acceptance, HumanAcceptance)
            and int(time.time()) - acceptance.issued_at <= ACCEPTANCE_MAX_AGE_SECONDS)


def gate_done(conn, task_id: str, *, board: Optional[str], acceptance: Any) -> Optional[str]:
    """None = allowed. Otherwise the refusal reason (event already recorded).

    On a review-required board Done is only reachable from ``review`` with a
    verified human acceptance; this applies to every caller, ``force`` included.
    """
    if not acceptance_required(board):
        return None
    kb = _kb()
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    status = row["status"] if row else None
    if status != "review":
        reason = f"board {board!r} requires Review before Done (task is {status!r})"
    elif not verify_acceptance(acceptance):
        reason = "trusted human acceptance required for Done on this board"
    else:
        return None
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "completion_refused_human_acceptance", {
            "reason": reason, "board": board})
    return reason


# Review-text shaping shared by the kernel (request_review) and the direct-Herdr bridge.
def _review_summary(report: str) -> str:
    """Extract the final human-facing result, without terminal chrome or progress narration."""
    import re

    text = (report or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("›", "? for shortcuts", "⚠", "Worked for ", "Worked ")):
            continue
        if any(marker in line for marker in (
            "OpenAI Codex", "Claude Code", "GPT-", "permissions:", "To get started, describe a task",
            "session_id:", "direct-herdr-dispatch bridge", "herdr agent", "Read SKILL.md",
        )):
            continue
        if line.startswith(("• Ran ", "• Explored ", "• Searched ", "• Listed ", "• Read ", "• Edited ", "└ ", "+ Show details")):
            continue
        cleaned.append(line)
    useful = [line for line in cleaned if re.search(
        r"(?i)(PR #?\d+|pull/\d+|https?://|deliverable|implemented|completed|recommend|verification|review ready|^changes?:)", line)]
    if useful:
        summary = " ".join(useful[-5:])
    else:
        summary = " ".join(cleaned[-3:])
    if not summary:
        return "Worker finished, but no concise review summary was available."
    if re.search(r"(?i)(handoff|transfer|pending|not implemented|not started|still pending)", summary) and not re.search(
            r"(?i)(PR #?\d+|pull/\d+|implemented|completed|deliverable ready)", summary):
        return HANDOFF_ONLY_SUMMARY
    return summary[:1200]


def _verified_review_artifacts(report: str, workspace_dir: str) -> list[str]:
    """Return explicit substantive workspace deliverables, excluding setup and session files."""
    from pathlib import Path

    lowered_report = report.lower()
    handoff_only = any(marker in lowered_report for marker in ("handoff", "transfer", "pending", "not implemented", "not started")) and not any(
        marker in lowered_report for marker in ("pr #", "pull/", "implemented", "completed", "deliverable ready"))
    if not workspace_dir:
        return []
    root = Path(workspace_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    candidates: set[Path] = set()
    # Absolute paths, relative paths with a directory part, and bare file names (no slash, so no
    # traversal syntax); every candidate is still resolved strictly and must land inside ``root``.
    pattern = r'''(?:/[^\s`'"<>]+|(?:\.{0,2}/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|(?<![\w/.:@-])[A-Za-z0-9_][A-Za-z0-9_.-]*\.[A-Za-z0-9]{1,5})'''
    for match in re.findall(pattern, report):
        cleaned = match.rstrip(".,:;)]}")
        if not cleaned or cleaned.startswith(("http://", "https://")):
            continue
        candidate = Path(cleaned).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            lower_name = resolved.name.lower()
            if any(token in lower_name for token in ("handoff", "transfer", "session", "transcript", "readme", "instructions")):
                continue
            if handoff_only and lower_name not in {"result.md", "final.md", "deliverable.md", "final_report.md", "report.md"}:
                continue
            if resolved.suffix.lower() not in {".md", ".txt", ".pdf", ".docx", ".xlsx", ".pptx"}:
                continue
            if resolved.is_file() and 0 < resolved.stat().st_size <= 10 * 1024 * 1024:
                candidates.add(resolved)
        except (OSError, ValueError):
            continue
    return sorted(str(path) for path in candidates)
