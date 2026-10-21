"""Review transitions: ``request_review`` (implementer -> Review, with the artifact contract,
deliverable verification and result-receipt reconciliation), ``request_changes`` (reviewer ->
implementer) and ``reopen_review_task`` (Review -> the current assignee), plus their
workspace/provenance helpers.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class _ReviewRefused(Exception):
    """request_review refused after its transaction opened; raising rolls everything back."""


def _task_workspace_for_review(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The task's workspace path from the real schema (``tasks.workspace_kind/workspace_path``; runs carry
    no path). A persisted path wins; an unset scratch task resolves to ``<board workspaces root>/<id>``
    for the CONNECTION's board. Read-only: never creates a directory or worktree. None when unknown."""
    row = conn.execute("SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    if row["workspace_path"]:
        return str(row["workspace_path"])
    if (row["workspace_kind"] or "scratch") == "scratch":
        from hermes_cli import kanban_settlement as _settlement
        slug = _settlement.board_of_connection(conn)
        if slug:
            return str(_kb.workspaces_root(board=slug) / task_id)
    return None


_REVIEW_ARTIFACT_EXCLUDED = ("handoff", "transfer", "session", "transcript", "instructions")


def _verified_declared_review_artifacts(
    declared: list[str], workspace: Optional[str], receipted: list[str],
) -> list[str]:
    """Declared deliverables that really exist: non-empty files that are not handoff/session
    material and either sit inside the task workspace or are listed in the bridge's receipt."""
    root = None
    if workspace:
        try:
            root = Path(workspace).expanduser().resolve()
        except OSError:
            root = None
    trusted = set()
    for item in receipted:
        try:
            trusted.add(Path(item).expanduser().resolve(strict=True))
        except OSError:
            continue
    out: list[str] = []
    for item in declared:
        try:
            path = Path(item).expanduser().resolve(strict=True)
        except OSError:
            continue
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        if any(token in path.name.lower() for token in _REVIEW_ARTIFACT_EXCLUDED):
            continue
        inside = False
        if root is not None:
            try:
                path.relative_to(root)
                inside = True
            except ValueError:
                inside = False
        if inside or path in trusted:
            out.append(str(path) if path in trusted and not inside else item)
    return out


def request_review(
    conn: sqlite3.Connection, task_id: str, *, summary: Optional[str] = None,
    metadata: Optional[dict] = None, reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None, force: bool = False, with_reason: bool = False,
):
    """``running``/``ready`` -> ``review``; never touches block recurrence accounting.

    Implementer and reviewer are recorded on the event so requested changes
    route back to the right profile; ``reviewer`` reassigns the task, and on
    re-review defaults to the latest ``changes_requested`` provenance. A live
    claim is only cleared with proof of ownership (``expected_run_id``) or
    ``force=True``. Returns ``bool``, or ``(ok, reason)`` with ``with_reason``.

    ``metadata["artifacts"]`` names the handoff's deliverable
    files; a review handoff is the last implementer transition, and the
    *reviewer's* completion is what cleans the managed scratch workspace up, so
    the files are staged into the task's durable attachments dir here and the
    staged paths ride the ``review_requested`` payload for the notifier to
    upload. A declared artifact that cannot be preserved raises
    :class:`ArtifactPreservationError`, rolling the whole transition back: the
    task stays ``running`` and retryable, with no attachments and no event.
    """

    def _ret(ok: bool, reason: Optional[str] = None):
        return (ok, reason) if with_reason else ok

    summary = _kb.redact_review_value(summary)
    metadata = _kb.redact_review_value(metadata)
    # Declared (metadata["artifacts"]) and prose-referenced files
    # must be durable BEFORE anything can clean the scratch workspace up: for a
    # review-bound card the reviewer's completion is the cleanup trigger.
    metadata = _kb._merge_completion_prose_artifacts(conn, task_id, metadata, summary=summary, result=None)
    # Declared deliverables must be delivered before Review; shared by every caller.
    from hermes_cli import kanban_settlement as _settlement
    _unmet = _settlement.check_artifact_contract(conn, task_id, _kb._cleaned_artifact_paths(metadata))
    if _unmet:
        return _ret(False, _settlement.refuse_unmet_contract(conn, task_id, _unmet, boundary="review"))
    now = int(time.time())
    # Staged copies live outside the txn: a rollback after staging must not
    # leave orphans that make the retry stage ``name_1.ext`` beside them.
    staged_copies: list[Path] = []
    try:
        with _kb.write_txn(conn):
            if not _kb._parents_satisfied(conn, task_id):
                return _ret(False, "parent dependencies are not satisfied")
            trow = conn.execute(
                "SELECT assignee, status, claim_lock, current_run_id, worker_pid, "
                "worker_started_at FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if trow is None:
                return _ret(False, "task not found")
            # Refuse to clear a live worker's claim without proof of ownership
            # (expected_run_id) or an explicit human override (force=True);
            # the same fence as complete_task (_claim_is_live).
            if expected_run_id is None and not force and _kb._claim_is_live(trow):
                return _ret(
                    False, "task is running under a live claim; pass expected_run_id "
                    "(worker ownership) or force=True (explicit operator "
                    "override) instead of clearing the live run's claim",
                )
            if reviewer is None:
                reviewer = _prior_reviewer(conn, task_id)
                if reviewer is False:
                    return _ret(
                        False, "re-review has no durable reviewer provenance (the "
                        "latest changes_requested event is missing or "
                        "malformed); pass reviewer= explicitly",
                    )
            reviewer = _kb._canonical_assignee(reviewer)
            # The actor is the run that did the work. ``assignee`` is the actor
            # only while a worker holds the card; on a never-claimed card it is
            # whoever the operator assigned -- possibly the reviewer itself,
            # which is what ``kanban create --assignee <reviewer>`` followed by
            # ``request-review`` produces. Recording the reviewer as its own
            # implementer is worse than recording nothing: request_changes()
            # routes on this field, and it already refuses a handoff that
            # carries no implementer provenance.
            implementer = None
            if trow["current_run_id"] is not None:
                arow = conn.execute(
                    "SELECT profile FROM task_runs WHERE id = ?",
                    (trow["current_run_id"],),
                ).fetchone()
                implementer = arow["profile"] if arow else None
            if implementer is None and trow["assignee"] != reviewer:
                implementer = trow["assignee"]
            assignee_sql = ", assignee = ?" if reviewer is not None else ""
            run_guard = "" if expected_run_id is None else " AND current_run_id = ?"
            params: tuple[Any, ...] = (
                *(() if reviewer is None else (reviewer,)), task_id,
                *(() if expected_run_id is None else (int(expected_run_id),)),
            )
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'review',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL
                """ + assignee_sql + """
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """ + run_guard,
                params,
            )
            if cur.rowcount != 1:
                return _ret(
                    False, "task is not in running/ready (or expected_run_id did not match the current run)",
                )
            artifacts = _kb._cleaned_artifact_paths(metadata)
            workspace = _task_workspace_for_review(conn, task_id)
            if artifacts:
                artifacts, metadata, short_summary = _verified_review_deliverables(
                    conn, task_id, summary, metadata, artifacts, workspace, expected_run_id,
                )
            if isinstance(metadata, dict):
                staged_copies = _kb._stage_completion_artifacts(
                    conn, task_id, metadata, now, uploaded_by="kanban_request_review",
                )
            if artifacts:
                summary = _deliverable_summary(short_summary, [Path(str(a)).name for a in artifacts[:3]])
            summary = _clip_review_summary(summary)
            run_id = _kb._end_or_synthesize_run(
                conn, task_id, outcome="review_requested", status="review",
                summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
                profile=implementer,
            )
            summary, metadata = _apply_result_receipt(conn, task_id, run_id, summary, metadata, expected_run_id)
            summary = _clip_review_summary(summary)
            if metadata and metadata.get("artifacts"):
                summary = _deliverable_summary(summary or "", [str(a) for a in metadata["artifacts"][:3]])
            payload: dict = {
                "summary": _kb._first_line(summary, 400) or None,
                "implementer": implementer,
                "reviewer": reviewer,
            }
            staged = _kb._cleaned_artifact_paths(metadata)
            if staged:
                payload["artifacts"] = staged
            _kb._append_event(conn, task_id, "review_requested", payload, run_id=run_id)
    except _ReviewRefused as refusal:
        # Raised inside the transaction so the status flip and staging roll back with it.
        if staged_copies:
            _kb._discard_staged_copies(staged_copies, staged_copies[0].parent)
        return _ret(False, str(refusal))
    except Exception:
        if staged_copies:
            _kb._discard_staged_copies(staged_copies, staged_copies[0].parent)
        raise
    return _ret(True)


def _verified_review_deliverables(
    conn: sqlite3.Connection, task_id: str, summary: Optional[str], metadata: Any,
    artifacts: list[str], workspace: Optional[str], expected_run_id: Optional[int],
) -> tuple[list[str], dict, str]:
    """``(verified artifacts, metadata carrying them, readable short summary)`` for a review
    handoff that declares deliverables; raises :class:`_ReviewRefused` when none is real.

    Verify BEFORE staging so the durable staged copies (not the scratch paths the reviewer's
    completion deletes) are what ride the payload. A declared file counts when it is a
    substantive non-handoff file inside the task workspace, or was already verified by the
    native bridge and recorded in this run's receipt.
    """
    from hermes_cli import kanban_settlement as _settlement
    from hermes_cli.kanban_settlement import _review_summary, _verified_review_artifacts

    # Persist a readable run summary, not the worker's complete terminal transcript.
    summary_text = (summary or "").strip()
    short_summary = _review_summary(summary_text)
    if short_summary.startswith("Worker handoff only"):
        raise _ReviewRefused("Review refused: worker handoff has no completed deliverable")
    receipt = _settlement._run_event(conn, task_id, "worker_result_receipt", expected_run_id)
    receipted = []
    if receipt is not None:
        receipted = [str(a) for a in (json.loads(receipt["payload"] or "{}").get("artifacts") or [])]
    verified = _verified_declared_review_artifacts(
        artifacts, workspace, receipted,
    ) or ([] if not workspace else _verified_review_artifacts(summary_text, workspace))
    if not verified:
        raise _ReviewRefused("Review refused: no verified workspace deliverable")
    return verified, {**(metadata if isinstance(metadata, dict) else {}), "artifacts": verified}, short_summary


_DELIVERABLE_MENTION = re.compile(r"(?i)(PR #?\d+|pull/\d+|https?://|deliverable|document|report)")


def _deliverable_summary(summary: str, names: list[str]) -> str:
    """Lead with the deliverables unless the summary already points at one; capped at 1200."""
    if not _DELIVERABLE_MENTION.search(summary[:400]):
        summary = "Deliverable ready for review: " + ", ".join(names) + ". " + summary
    return summary[:1200]


def _clip_review_summary(summary: Optional[str]) -> Optional[str]:
    if summary is not None and len(summary) > 1200:
        return summary[:1197].rstrip() + "..."
    return summary


def _apply_result_receipt(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int], summary: Optional[str],
    metadata: Any, expected_run_id: Optional[int],
) -> tuple[Optional[str], Any]:
    """Fold this run's ``worker_result_receipt`` (bridge report + verified artifacts) into the
    review summary/metadata; the receipt's report also becomes the closed run's summary."""
    from hermes_cli import kanban_settlement as _settlement

    receipt = _settlement._run_event(conn, task_id, "worker_result_receipt", expected_run_id)
    if receipt is None:
        return summary, metadata
    receipt_payload = json.loads(receipt["payload"] or "{}")
    receipt_summary = receipt_payload.get("report") or summary
    if receipt_summary and receipt_summary != summary:
        if run_id is not None:
            conn.execute("UPDATE task_runs SET summary = ? WHERE id = ?", (receipt_summary, run_id))
        summary = receipt_summary
    return summary, {**(metadata or {}), "artifacts": receipt_payload.get("artifacts") or []}


def _prior_reviewer(conn: sqlite3.Connection, task_id: str):
    """Reviewer recorded by the latest ``changes_requested`` run's event.
    ``None`` = first review (no such run); ``False`` = a run exists but its
    provenance is missing/malformed."""
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    changes_event = _kb._latest_event(conn, task_id, "changes_requested", changes_run["id"])
    reviewer = _kb._json_dict(_kb._row_get(changes_event, "payload")).get("reviewer")
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False


def request_changes(
    conn: sqlite3.Connection, task_id: str, *, reason: str, expected_run_id: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """Close an active reviewer run (claimed from ``review``) and hand the task
    back to the implementer from the latest ``review_requested`` event, parent
    gating reapplied. Returns ``(ok, implementer | reason)``."""
    reason = str(_kb.redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"

    with _kb.write_txn(conn):
        task_row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if task_row is None:
            return False, "task not found"
        current_run_id = task_row["current_run_id"]
        if task_row["status"] != "running" or current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"

        claimed_event = _kb._latest_event(conn, task_id, "claimed", current_run_id)
        claimed_payload = _kb._json_dict(_kb._row_get(claimed_event, "payload"))
        if claimed_payload.get("source_status") != "review":
            return False, "active run was not claimed from review"

        requested_event = _kb._latest_event(conn, task_id, "review_requested")
        if requested_event is None:
            return False, "no prior review_requested event"
        implementer = _kb._nonblank_str(_kb._json_dict(requested_event["payload"]).get("implementer"))
        if implementer is None:
            return False, "review handoff has no valid implementer provenance"
        reviewer = _kb._canonical_assignee(_kb._nonblank_str(task_row["assignee"]))

        new_status = _kb._landing_status_after_parents(conn, task_id)
        # consecutive_failures deliberately PRESERVED: a review transition is
        # not evidence the pathology cleared; only complete_task resets it.
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = COALESCE(?, assignee),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL
             WHERE id = ? AND status = 'running' AND current_run_id = ?
            """,
            (new_status, implementer, task_id, int(current_run_id)),
        )
        if cur.rowcount != 1:
            return False, "task changed during review handoff"
        run_id = _kb._end_run(
            conn, task_id, outcome="changes_requested", status=new_status, summary=reason,
        )
        _kb._append_event(
            conn,
            task_id,
            "changes_requested",
            {
                "reason": reason,
                "implementer": implementer,
                "reviewer": reviewer,
                "status": new_status,
            },
            run_id=run_id,
        )
    return True, implementer


def reopen_review_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``review`` -> ``ready``/``todo`` so the implementer re-runs on the new
    comments; restores the implementer from the ``review_requested`` event.
    Preserves ``consecutive_failures`` and the block loop counter (review is
    not a block; only :func:`complete_task` clears them)."""
    now = int(time.time())
    with _kb.write_txn(conn):
        _kb._reclaim_dangling_run(
            conn, task_id, statuses=("review",), now=now,
            note="invariant recovery on review reopen",
        )
        new_status = _kb._landing_status_after_parents(conn, task_id)
        review_event = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
            "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        # A reassignment after the handoff is the newer routing decision: restoring the
        # implementer recorded at review time would route the card back to a stale handler.
        reassigned = review_event is not None and conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'assigned' AND id > ?",
            (task_id, review_event["id"])).fetchone() is not None
        implementer = None if reassigned else _kb._nonblank_str(_kb._json_dict(_kb._row_get(review_event, "payload")).get("implementer"))
        params: tuple[Any, ...] = (new_status, *((implementer,) if implementer else ()), task_id)
        cur = conn.execute(
            # consecutive_failures deliberately PRESERVED: review reopen is not
            # a success signal; only complete_task resets the breaker (#35072).
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            + (", assignee = ?" if implementer else "")
            + " WHERE id = ? AND status = 'review'",
            params,
        )
        if cur.rowcount != 1:
            return False
        payload: dict[str, Any] = {"status": new_status}
        if implementer:
            payload["implementer"] = implementer
        _kb._append_event(
            conn, task_id, "review_reopened", payload if payload != {"status": "ready"} else None,
        )
        return True

def _descendant_statuses(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str, bool]]:
    """``(id, status, owned)`` of every transitive child of *task_id*, id order; *owned* means a
    claim, open run or worker pid is set (a running OR review child can hold one). Read-only."""
    rows = conn.execute(
        """
        WITH RECURSIVE descendants(id) AS (
            SELECT child_id FROM task_links WHERE parent_id = ?
            UNION
            SELECT l.child_id FROM task_links l JOIN descendants d ON d.id = l.parent_id
        )
        SELECT t.id, t.status, (t.claim_lock IS NOT NULL OR t.current_run_id IS NOT NULL
                                OR t.worker_pid IS NOT NULL) AS owned FROM descendants d JOIN tasks t ON t.id = d.id ORDER BY t.id
        """,
        (task_id,),
    ).fetchall()
    return [(r["id"], r["status"], bool(r["owned"])) for r in rows]


def _done_reopen_refusal(conn: sqlite3.Connection, task_id: str, reason: Optional[str]) -> Optional[str]:
    """Why :func:`reopen_done_task` must refuse right now, or ``None``. The dry-run and the
    in-transaction recheck both call this, so they cannot drift."""
    from hermes_cli import kanban_settlement as _settlement

    if os.environ.get("HERMES_KANBAN_TASK"):
        return "reopen-done is operator-only; a running worker cannot reopen closed tasks"
    row = conn.execute(
        "SELECT status, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        return f"task {task_id} not found"
    if row["status"] != "done":
        return f"task {task_id} is {row['status']!r}; reopen-done only applies to 'done'"
    if not reason:
        return "reopen-done requires a nonblank reason (recorded in the audit event)"
    board = _settlement.board_of_connection(conn)
    if _settlement.acceptance_required(board):
        return (
            f"board {board!r} protects trusted human acceptance; a Done card there cannot be "
            f"reopened from the CLI (use the signed-in dashboard)"
        )
    if row["claim_lock"] is not None or row["current_run_id"] is not None or row["worker_pid"] is not None:
        return f"task {task_id} still has a claim or open run; reclaim it first"
    holds = _settlement._open_holds(conn, task_id)
    if holds:
        return f"task {task_id} has open holds that reopening cannot bypass: {'; '.join(holds)}"
    live = [cid for cid, status, owned in _descendant_statuses(conn, task_id) if owned or status == "running"]
    if live:
        return (
            f"descendants are running or hold a claim, run or worker ({', '.join(live)}); "
            f"reopening would have to kill their workers. Reclaim them first"
        )
    return None


def reopen_done_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str], dry_run: bool = False,
) -> tuple[bool, Optional[str], dict[str, Any]]:
    """Operator recovery ``done`` -> ``ready`` (or ``todo`` while a parent is unfinished) so the
    normal evidence-gated :func:`request_review` can take the card to Review. Keeps the task
    row, ``result``, runs, comments, attachments and history; writes ``done_recovered``.
    Reopening a satisfied parent demotes its descendants through the same
    ``invalidate_descendants_for_parent_reopen`` the dashboard uses, inside one transaction
    (any failure rolls everything back). It never grants Done, and refuses boards that protect
    trusted human acceptance, live claims, open holds and running descendants.
    Returns ``(ok, error, info)``; ``info`` has ``to_status`` and ``invalidate`` (descendant ids)."""
    reason = str(_kb.redact_review_value(reason or "")).strip() or None
    refusal = _done_reopen_refusal(conn, task_id, reason)
    if refusal:
        return False, refusal, {}
    if dry_run:
        return True, None, _reopen_done_info(conn, task_id)
    with _kb.write_txn(conn):
        refusal = _done_reopen_refusal(conn, task_id, reason)
        if refusal:
            return False, refusal, {}
        info = _reopen_done_info(conn, task_id)
        prior = conn.execute("SELECT completed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()["completed_at"]
        cur = conn.execute(
            "UPDATE tasks SET status = ?, completed_at = NULL WHERE id = ? AND status = 'done' "
            "AND claim_lock IS NULL AND current_run_id IS NULL AND worker_pid IS NULL",
            (info["to_status"], task_id),
        )
        if cur.rowcount != 1:
            return False, f"task {task_id} changed during reopen", {}
        result = _kb.invalidate_descendants_for_parent_reopen(conn, task_id, author=actor)
        info["invalidate"] = [d["id"] for d in result["invalidated"]]
        _kb._append_event(conn, task_id, "done_recovered", {
            "actor": actor, "reason": reason, "from_status": "done", "to_status": info["to_status"],
            "prior_completed_at": prior, "invalidated": info["invalidate"],
        })
        _kb._append_event(conn, task_id, "status", {
            "status": info["to_status"], "reason": "done_recovered", "previous_status": "done"})
    return True, None, info


def _reopen_done_info(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    return {
        "to_status": _kb._landing_status_after_parents(conn, task_id),
        "invalidate": [cid for cid, st, _owned in _descendant_statuses(conn, task_id)
                       if st in {"ready", "review", "done"}],
    }


from hermes_cli import kanban_db as _kb
