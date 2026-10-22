"""Board-owned executor linkage for a direct-Herdr run: the startup invariant before any prompt.

On connect card t_b956ba38 the bridge's agent was live and tagged, yet the card itself named no
executor, so nobody could tell from the card which machine/pane/session was doing the work, and a
manual ref had to be added afterwards. Every run (fresh pane, adopted/resumed agent, reply run) now
does, in order, before the agent is given its assignment:

1. the bridge creates or adopts the pane and tags it (``kanban_task`` / ``kanban_board`` /
   ``kanban_run``) -- ``kanban_direct_herdr_worker.link_pane``;
2. :func:`resolve_executor` re-reads the LIVE agent and verifies its exact tags, its pane, and its
   native harness session id (only reported once the harness has started, which is why this runs
   after ``agent start`` and before the prompt rather than before launch), and confirms the pane is
   listed with those tags by ``pane list`` -- the same ``pane.list`` query the dashboard's Herdr
   drawer discovers panes with;
3. :func:`persist_executor` records the identity on the canonical card (an ``executor_linked``
   event plus one deduplicated ``herdr`` ref), fenced by the run (or reply lease) that owns the card.

Any step failing stops the run before the prompt: the agent is left idle and unprompted in its tagged
pane, and the next run adopts it and repeats these steps idempotently, so a restart never starts a
second executor and never leaves an unlinked one doing project work.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional

_IDENTITY_WAIT_S = 20.0
_IDENTITY_POLL_S = 0.5
REF_TYPE = "herdr"
# Harnesses whose current builds cannot report a native session id. They still
# link by name, pane, and live kanban tags; continuation resumes the pane, not a
# native session. Remove a label here once its harness reports sessions.
_SESSIONLESS_AGENT_LABELS = {"rpi"}


class LinkError(RuntimeError):
    """The executor could not be verified or recorded; the run must not prompt it."""


@dataclass
class Executor:
    host: str
    session: str
    workspace_id: str
    tab_id: str
    pane_id: str
    agent: str
    terminal_id: Optional[str]
    native_session: Optional[str]
    fence: Optional[str]

    @property
    def ref(self) -> str:
        return f"{self.host}/{self.session}/{self.workspace_id}/{self.tab_id}/{self.pane_id}"

    @property
    def note(self) -> str:
        native = self.native_session or "not reported"
        return f"agent {self.agent}; native session {native}; run {self.fence or '-'}"


def herdr_session() -> str:
    return os.environ.get("HERMES_KANBAN_HERDR_SESSION") or "default"  # health: allow HX002 -- dispatcher->worker handoff, same convention as TASK/WORKSPACE/BOARD


def _native_session(info: dict) -> Optional[str]:
    session = info.get("agent_session")
    value = session.get("value") if isinstance(session, dict) else None
    return str(value) if value else None


def resolve_executor(
    name: str, pane_id: str, task_id: str, board: str, fence: Optional[str], *,
    agent_info: Callable[[str], Optional[dict]], list_panes: Callable[[], list[dict]],
    wait_s: float = _IDENTITY_WAIT_S,
) -> Executor:
    """The verified live identity of this run's agent, or :class:`LinkError`. Waits (bounded) for
    the harness to report its native session id."""
    deadline = time.monotonic() + wait_s
    while True:
        info = agent_info(name)
        if info is None:
            raise LinkError(f"agent {name} is not present")
        sessionless = str(info.get("agent") or "").lower() in _SESSIONLESS_AGENT_LABELS
        if _native_session(info) or sessionless or time.monotonic() >= deadline:
            break
        time.sleep(_IDENTITY_POLL_S)
    if info.get("pane_id") not in (None, pane_id):
        raise LinkError(f"agent {name} is in pane {info.get('pane_id')!r}, not {pane_id!r}")
    want = {"kanban_task": task_id, "kanban_board": board, **({"kanban_run": fence} if fence else {})}
    tokens = info.get("tokens") or {}
    if any(tokens.get(k) != v for k, v in want.items()):
        raise LinkError(f"agent {name}'s live tags {tokens!r} do not match {want!r}")
    listed = next((p for p in list_panes() if p.get("pane_id") == pane_id), None)
    if listed is None:
        raise LinkError(f"pane {pane_id} is not listed by `pane list`, so the dashboard cannot discover it")
    listed_tokens = listed.get("tokens") or {}
    if any(listed_tokens.get(k) != v for k, v in want.items()):
        raise LinkError(f"pane {pane_id} is listed with tags {listed_tokens!r}, not {want!r}")
    native = _native_session(info)
    if not native and str(info.get("agent") or "").lower() not in _SESSIONLESS_AGENT_LABELS:
        raise LinkError(f"agent {name} has not reported its native session id after {wait_s:.0f}s")
    return Executor(
        host=socket.gethostname().split(".")[0], session=herdr_session(),
        workspace_id=str(info.get("workspace_id") or listed.get("workspace_id") or ""),
        tab_id=str(info.get("tab_id") or listed.get("tab_id") or ""), pane_id=pane_id, agent=name,
        terminal_id=info.get("terminal_id") or listed.get("terminal_id"), native_session=native, fence=fence,
    )


def persist_executor(
    task_id: str, board: Optional[str], executor: Executor, *,
    run_id: Optional[int] = None, lock: Optional[str] = None,
) -> None:
    """Record ``executor`` on the card, fenced by the owning run or reply lease; raises
    :class:`LinkError` when that owner no longer holds the card. Idempotent: the same pane adds one
    ref however many runs adopt it; every run still gets its own ``executor_linked`` event."""
    from hermes_cli import kanban_comment_replies as replies
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_refs as refs

    conn = kbc.connect(board=board)
    try:
        with kb.write_txn(conn):
            if lock is not None:
                lease = replies._open_lease(conn, task_id, int(time.time()))
                if not lease or lease.get("lock") != lock:
                    raise LinkError("the reply lease no longer owns this card")
            elif run_id is None or kb._current_run_id(conn, task_id) != int(run_id):
                raise LinkError(f"run {run_id} no longer owns this card")
            known = conn.execute(
                "SELECT 1 FROM task_refs WHERE task_id = ? AND ref_type = ? AND ref = ?",
                (task_id, REF_TYPE, executor.ref)).fetchone()
            if known is None:
                refs.add_task_ref(conn, task_id, REF_TYPE, ref=executor.ref, host=executor.host,
                                  note=executor.note)
            kb._append_event(conn, task_id, "executor_linked", {
                **asdict(executor), "ref": executor.ref,
                **({"lock": lock} if lock is not None else {"run_id": int(run_id)})},
                run_id=int(run_id) if run_id is not None else None)
    finally:
        conn.close()
