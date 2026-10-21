"""Human comment -> assignee reply queue.

A comment a person writes on a card from the dashboard or desktop app is a question or instruction
to the card's CURRENT assignee, and that assignee answers on the same card. The pieces:

- **Admission** is durable and trusted: ``kanban_db.add_comment`` records the writing surface as
  ``source`` in the ``commented`` event of the same transaction. Only the human UI surface
  (:data:`HUMAN_COMMENT_SOURCES`) is admitted; the CLI (agents drive it through their terminal
  tool), the agent toolset and the replies themselves never are, so an agent or system comment can
  never start a reply cycle and replies cannot recurse.
- **Consumption** is a watermark: ``comment_replied`` events carry ``through_id``, the newest
  human comment the reply covered. Pending = admitted human comments above the watermark.
- **Routing** follows the card as it stands: a ``running`` card's worker (or the next run of a
  ``ready`` card) answers through its final report, so a reply queues behind busy work; a card at
  rest (Review, Done, blocked, ...) gets a lease-fenced reply run from its current assignee that
  answers WITHOUT moving the card. Conversational questions leave Review/Done alone; an explicitly
  requested action re-enters the normal run lifecycle (review reopen, unblock) so claims, contract
  gates and human acceptance still apply.
- **Bounded**: a reply that fails :data:`REPLY_MAX_ATTEMPTS` times posts one truthful system note
  and advances the watermark instead of retrying forever.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Callable, Optional

HUMAN_COMMENT_SOURCES = frozenset({"dashboard"})
REPLY_SOURCE = "kanban-reply"
SYSTEM_SOURCE = "kanban-system"
WORKER_COMMENT_SOURCES = frozenset({"agent", REPLY_SOURCE, SYSTEM_SOURCE})
REPLY_LEASE_SECONDS = 45 * 60
REPLY_MAX_ATTEMPTS = 2
# Cards at rest: nobody is working them, so a reply run is the only way the assignee answers.
# ``ready`` is left to the run the dispatcher is about to start; ``running`` queues.
REPLY_STATUSES = ("review", "done", "blocked", "todo", "triage", "scheduled")
_ACTION_STATUSES = {"review": "reopen_review", "blocked": "unblock"}


def _kb():
    from hermes_cli import kanban_db
    return kanban_db


def _payload(raw: Any) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _events(conn, task_id: str, kind: str) -> list[tuple[int, dict]]:
    rows = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind)).fetchall()
    return [(int(r["id"]), _payload(r["payload"])) for r in rows]


def human_comment_ids(conn, task_id: str) -> list[int]:
    """Comment ids whose ``commented`` event names a trusted human surface."""
    return [
        int(p["comment_id"]) for _, p in _events(conn, task_id, "commented")
        if p.get("source") in HUMAN_COMMENT_SOURCES and isinstance(p.get("comment_id"), int)
    ]


def worker_comment_ids(conn, task_id: str) -> list[int]:
    """Comment ids an agent or the system wrote (never an instruction to the next run)."""
    return [
        int(p["comment_id"]) for _, p in _events(conn, task_id, "commented")
        if p.get("source") in WORKER_COMMENT_SOURCES and isinstance(p.get("comment_id"), int)
    ]


def reply_watermark(conn, task_id: str) -> int:
    return max((int(p.get("through_id") or 0) for _, p in _events(conn, task_id, "comment_replied")), default=0)


def pending_human_comments(conn, task_id: str) -> list:
    """Admitted human comments the assignee has not answered yet, oldest first."""
    mark = reply_watermark(conn, task_id)
    wanted = {cid for cid in human_comment_ids(conn, task_id) if cid > mark}
    if not wanted:
        return []
    return [c for c in _kb().list_comments(conn, task_id) if c.id in wanted]


def _open_lease(conn, task_id: str, now: int) -> Optional[dict]:
    """The newest reply lease not yet closed by a reply/failure, with ``expired`` set."""
    claims = _events(conn, task_id, "comment_reply_claimed")
    if not claims:
        return None
    _, lease = claims[-1]
    lock = lease.get("lock")
    closed = any(p.get("lock") == lock for kind in ("comment_replied", "comment_reply_failed")
                 for _, p in _events(conn, task_id, kind))
    if closed:
        return None
    spawned = [p for _, p in _events(conn, task_id, "comment_reply_spawned") if p.get("lock") == lock]
    dead = False
    if spawned and spawned[-1].get("pid"):
        from hermes_cli.kanban_db_dispatch import _worker_alive
        dead = not _worker_alive(int(spawned[-1]["pid"]), spawned[-1].get("started_at"))
    return {**lease, "expired": dead or int(lease.get("expires") or 0) <= now}


def reply_in_flight(conn, task_id: str, *, now: Optional[int] = None) -> bool:
    lease = _open_lease(conn, task_id, int(now if now is not None else time.time()))
    return bool(lease) and not lease["expired"]


def claim_reply(conn, task_id: str, *, now: Optional[int] = None) -> Optional[dict]:
    """Take the reply lease for a card at rest with pending human comments, or ``None``."""
    kb = _kb()
    now = int(now if now is not None else time.time())
    with kb.write_txn(conn):
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] not in REPLY_STATUSES or not row["assignee"]:
            return None
        if _open_lease(conn, task_id, now):
            return None
        pending = pending_human_comments(conn, task_id)
        if not pending:
            return None
        lease = {"lock": uuid.uuid4().hex, "through_id": pending[-1].id, "assignee": row["assignee"],
                 "status": row["status"], "expires": now + REPLY_LEASE_SECONDS}
        kb._append_event(conn, task_id, "comment_reply_claimed", lease)
    return lease


def note_reply_spawned(conn, task_id: str, lock: str, pid: int) -> None:
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    kb = _kb()
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "comment_reply_spawned",
                         {"lock": lock, "pid": int(pid), "started_at": _process_fingerprint(int(pid))})


def record_reply(
    conn, task_id: str, *, through_id: int, author: str, body: str,
    lock: Optional[str] = None, run_id: Optional[int] = None,
) -> Optional[int]:
    """Post ``body`` as ``author``'s reply and advance the watermark to ``through_id``, atomically.

    Fenced by exactly one owner: the reply lease ``lock`` (a reply run) or the card's current
    ``run_id`` (a work run answering in its final report). A stale owner posts nothing.
    """
    kb = _kb()
    body = (body or "").strip()
    if not body:
        return None
    with kb.write_txn(conn):
        if lock is not None:
            lease = _open_lease(conn, task_id, int(time.time()))
            if not lease or lease.get("lock") != lock:
                return None
        elif run_id is None or kb._current_run_id(conn, task_id) != int(run_id):
            return None
        if reply_watermark(conn, task_id) >= int(through_id):
            return None
        comment_id = kb.add_comment(conn, task_id, author, body, source=REPLY_SOURCE)
        kb._append_event(conn, task_id, "comment_replied", {
            "through_id": int(through_id), "comment_id": comment_id, "author": author,
            **({"lock": lock} if lock else {"run_id": int(run_id)})})
    return comment_id


def fail_reply(conn, task_id: str, *, lock: str, reason: str) -> str:
    """Close a failed reply lease. After :data:`REPLY_MAX_ATTEMPTS` failures for the same pending
    comment, post one truthful system note and advance the watermark: ``"gave_up"``; else
    ``"retry"`` (the next tick takes a fresh lease). ``"stale"`` when the lease is not open."""
    kb = _kb()
    with kb.write_txn(conn):
        lease = _open_lease(conn, task_id, int(time.time()))
        if not lease or lease.get("lock") != lock:
            return "stale"
        through_id = int(lease["through_id"])
        kb._append_event(conn, task_id, "comment_reply_failed",
                         {"lock": lock, "through_id": through_id, "reason": reason[:500]})
        attempts = sum(1 for _, p in _events(conn, task_id, "comment_reply_failed")
                       if int(p.get("through_id") or 0) == through_id)
        if attempts < REPLY_MAX_ATTEMPTS:
            return "retry"
        note = (f"@{lease.get('assignee')} could not answer the comment(s) above after {attempts} "
                f"attempts. Last error: {reason[:300]}. Comment again or reopen the card to retry.")
        comment_id = kb.add_comment(conn, task_id, "kanban", note, source=SYSTEM_SOURCE)
        kb._append_event(conn, task_id, "comment_replied", {
            "through_id": through_id, "comment_id": comment_id, "gave_up": True, "lock": lock})
    return "gave_up"


def route_requested_action(conn, task_id: str, *, author: str, action: str) -> str:
    """Hand an explicitly requested action to the normal run lifecycle. Review reopens to the
    current assignee, a blocked card unblocks; every other resting status (Done included) stays
    put with a note, because only a person may reopen it."""
    kb = _kb()
    status = kb._task_status(conn, task_id)
    route = _ACTION_STATUSES.get(status or "")
    if route == "reopen_review" and kb.reopen_review_task(conn, task_id):
        kb.add_comment(conn, task_id, author, f"Reopened to act on the request: {action}", source=REPLY_SOURCE)
        return "reopened"
    if route == "unblock" and kb.unblock_task(conn, task_id):
        kb.add_comment(conn, task_id, author, f"Unblocked to act on the request: {action}", source=REPLY_SOURCE)
        return "unblocked"
    kb.add_comment(conn, task_id, author, (
        f"The request needs the card reopened (it is {status}); nothing was started: {action}"),
        source=REPLY_SOURCE)
    return "needs_reopen"


def tasks_with_pending_replies(conn) -> list[str]:
    """Cards at rest carrying an admitted human comment above their watermark. The LIKE is only a
    prefilter (payloads are ``json.dumps`` text); :func:`human_comment_ids` decides."""
    marks = " OR ".join("e.payload LIKE ?" for _ in HUMAN_COMMENT_SOURCES)
    rows = conn.execute(
        "SELECT DISTINCT e.task_id FROM task_events e JOIN tasks t ON t.id = e.task_id "
        f"WHERE e.kind = 'commented' AND ({marks}) "
        f"AND t.status IN ({','.join('?' for _ in REPLY_STATUSES)})",
        (*(f'%"source": "{s}"%' for s in sorted(HUMAN_COMMENT_SOURCES)), *REPLY_STATUSES)).fetchall()
    return [r["task_id"] for r in rows if pending_human_comments(conn, r["task_id"])]


def _reply_workspace(conn, task, board: Optional[str]) -> Optional[str]:
    """The card's existing workspace; a scratch card that never ran gets its scratch dir (no side
    effects beyond a directory). A worktree card without one is left alone: creating a worktree
    just to answer a question is not a reply's business."""
    if task.workspace_path and os.path.isdir(task.workspace_path):
        return task.workspace_path
    if task.workspace_kind != "scratch":
        return None
    from hermes_cli import kanban_db_workspace as kbw
    workspace = str(kbw.resolve_workspace(task, board=board))
    kbw.set_workspace_path(conn, task.id, workspace)
    return workspace


def reconcile_comment_replies(
    conn, *, board: Optional[str],
    spawn_fn: Callable[..., Optional[int]],
    eligible: Callable[[Any], bool],
    now: Optional[int] = None,
) -> list[str]:
    """One dispatcher pass: close dead/expired reply leases, then start a reply run for each card
    at rest with pending human comments. ``eligible(task)`` says whether this board/host can run a
    reply for that card; ``spawn_fn(task, workspace, board, env)`` returns the worker PID."""
    kb = _kb()
    now = int(now if now is not None else time.time())
    started: list[str] = []
    for task_id in tasks_with_pending_replies(conn):
        lease = _open_lease(conn, task_id, now)
        if lease and lease["expired"]:
            fail_reply(conn, task_id, lock=lease["lock"], reason="the reply worker exited or timed out without replying")
        task = kb.get_task(conn, task_id)
        if task is None or task.status not in REPLY_STATUSES or not task.assignee or not eligible(task):
            continue
        workspace = _reply_workspace(conn, task, board)
        if workspace is None:
            continue
        lease = claim_reply(conn, task_id, now=now)
        if lease is None:
            continue
        env = {"HERMES_KANBAN_REPLY_LOCK": lease["lock"], "HERMES_KANBAN_REPLY_THROUGH": str(lease["through_id"])}
        try:
            pid = spawn_fn(task, workspace, board, env)
        except Exception as exc:  # health: allow BLE001 -- recorded on the lease; the sweep continues
            fail_reply(conn, task_id, lock=lease["lock"], reason=f"spawn failed: {exc}")
            continue
        if pid:
            note_reply_spawned(conn, task_id, lease["lock"], int(pid))
        started.append(task_id)
    return started


# --------------------------------------------------------------------------- #
# Requested deliverables named in instructions.
# --------------------------------------------------------------------------- #

_PR_MENTION = re.compile(r"(?i)\b(?:pull request|PR)\b")
_PR_REQUEST = re.compile(
    r"(?i)\bdeliverable\b[^.\n]{0,40}\b(?:pull request|PR)\b"
    r"|\b(?:open|create|raise|submit|make|need|want|waiting for|send)\s+(?:me\s+)?(?:a|an|the)\s+(?:pull request|PR)\b"
    r"|\b(?:pull request|PR)\s+(?:link|url)\b")
_PR_NEGATED = re.compile(r"(?i)\b(?:no|not|don'?t|never|without)\b[^.\n]{0,24}\b(?:pull request|PR)\b")
PR_URL = re.compile(r"https?://\S+/pull/\d+|https?://\S+/pullrequest/\d+")


def requested_deliverables(comments: list) -> list[str]:
    """Deliverables later instructions added to the card (today: a pull request), as prompt lines.
    The newest comment that talks about a PR decides: a request adds it, a negation ("do not open
    a PR") withdraws it, a plain mention ("confirm the PR") is skipped."""
    for c in reversed(comments):
        body = c.body or ""
        if not _PR_MENTION.search(body):
            continue
        if _PR_NEGATED.search(body):
            return []
        if _PR_REQUEST.search(body):
            return [f"A pull request (requested in comment #{c.id}); report its URL as evidence."]
    return []
