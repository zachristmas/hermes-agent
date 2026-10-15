"""Close a Herdr pane and later reopen the SAME agent session into a fresh one.

Closing a Herdr pane destroys Herdr's own record of the agent that lived in it: verified live
(herdr 0.9.3) that ``agent get``/``agent list`` report ``agent_not_found`` for that name
immediately after ``pane close`` returns, with no server-side way to recover its
``agent_session`` afterward. Herdr's own pane-survives-restart behavior is a different
mechanism (an on-disk session-snapshot the Herdr app itself replays across a process restart of
STILL-OPEN panes) and exposes nothing usable for an explicitly-closed pane. So this module's own
contract is the honest one: an agent's resumable identity (``agent_session.value`` --
Claude/Codex's own session id, plus which agent kind it belongs to and where) must be captured
BEFORE the pane closes, or it is gone for good. ``close()`` captures and persists that record as
its first act, then closes the pane; ``reopen()`` reads a persisted record (or an explicit
session id the caller already has) and replays the per-kind resume invocation into a fresh pane.

Resume syntax per agent kind (confirmed against the installed CLIs' own ``--help``, and for
``claude`` additionally live-verified end to end: a pane was closed, a new pane was opened
elsewhere, and a prompt asking the resumed agent to recall a secret phrase it was given before
closing came back correct):

- ``claude``: ``claude --resume <session-id>`` -- the id is accepted as a trailing CLI arg.
- ``codex``: ``codex resume <session-id>`` -- note this is a SUBCOMMAND (the first positional
  arg), not a flag; it does not mirror Claude's ``--resume``.

Cross-host (Mac <-> Windows): same single-coordinating-install pattern as
``kanban_direct_herdr_worker.py``'s ``sweep_other_hosts_for_task``/``_continue_remote_matches``
(whichever host runs the command reaches every other saved Herdr machine via
``herdr --machine <id> ...``, never the reverse). ``machine_id`` is forwarded on every call this
module makes, so a pane can be closed on one saved Herdr machine and reopened on a DIFFERENT one
later (``reopen(..., machine_id=...)`` always overrides the record's own machine, never just the
one it happened to be closed on).

Re-verified directly (not assumed) before building this, twice, with two independent agent
sessions (not just the first run that motivated this module): starting an agent, capturing its
``agent_session.value``, closing its pane, then immediately checking BOTH ``agent get <name>``
and the full ``agent list`` -- the closed agent is entirely absent from both, every time, with no
observed delay/GC lag. Herdr keeps no live post-close record at all (unlike the dispatch bridge's
sweep, which only ever looks at agents that are still running -- it never needed to survive a
close, because it is finding existing in-progress work, not resuming something intentionally
closed). That is why this module needs its own persisted record
(``herdr_session_store.py``, a local JSON file under this install's ``HERMES_HOME``) where the
dispatch bridge needs none: there would be nothing left in Herdr's own live state to read it back
from. Consistent with the same pattern, though, this is single-install bookkeeping, not a sync
feature -- ``close()`` and ``reopen()`` are expected to be issued from the SAME coordinating
Hermes install (reaching either machine's Herdr server via ``--machine``), exactly like the
dispatch bridge's own sweep. Nothing here attempts to reconcile two independently-written local
records across two separate Hermes installs; if that turns out to be genuinely needed, it is a
distinct follow-up, not something to guess at here.

Deliberately NOT reused from ``kanban_direct_herdr_worker.py`` (the existing direct-Herdr-dispatch
bridge, which already wraps the ``herdr`` CLI in a very similar shape): that module is mid-flight
work on another branch this change must not touch or collide with. The small subprocess wrapper
below is this module's own, independent copy of just the parts it needs (command resolution, JSON
result unwrapping, structured error classification) -- a future cleanup pass can fold both into one
shared sibling once that other work has landed.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from typing import Any, Optional

from hermes_cli import herdr_session_store as store

_CLI_TIMEOUT_S = 30
_AGENT_START_TIMEOUT_S = 90
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: Per-agent-kind resume invocation, built as argv appended after ``herdr agent start ... --``.
#: Add a new kind here only once its real resume syntax has been confirmed against that agent's
#: own ``--help`` (and ideally exercised live) -- an unconfirmed guess here would silently start a
#: FRESH session instead of resuming, which is worse than refusing.
_RESUME_ARG_BUILDERS = {
    "claude": lambda session_id: ["--resume", session_id],
    "codex": lambda session_id: ["resume", session_id],
}


class PaneLifecycleError(RuntimeError):
    """A clean, reportable failure -- never lets a caller hang or see a raw traceback."""


class HerdrCliError(PaneLifecycleError):
    """A structured ``{"error": {"code": ..., "message": ...}}`` response from the herdr CLI."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def herdr_command(machine_id: Optional[str] = None) -> list[str]:
    """Resolve the ``herdr`` executable, same resolution order as the rest of the Herdr
    integration: ``$HERDR_BIN``, then PATH, then the standalone-release glob."""
    env = os.environ.get("HERDR_BIN")
    if env:
        base = shlex.split(env, posix=(os.name != "nt"))
    else:
        found = shutil.which("herdr")
        if not found:
            pattern = os.path.join(
                os.path.expanduser("~"), ".herdr", "packages", "standalone", "releases", "*", "herdr*"
            )
            hits = sorted(h for h in glob.glob(pattern) if os.path.basename(h).lower() in ("herdr", "herdr.exe"))
            found = hits[-1] if hits else None
        if not found:
            raise PaneLifecycleError("the herdr command isn't installed or isn't on PATH (set HERDR_BIN)")
        base = [found]
    if machine_id:
        return [*base, "--machine", machine_id]
    return base


def _parse_json(text: Optional[str]) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text.splitlines()[-1])
    except ValueError:
        return None


def run_herdr(args: list[str], *, timeout: float = _CLI_TIMEOUT_S, machine_id: Optional[str] = None) -> Any:
    """Run one ``herdr`` subcommand and return its parsed ``.result`` (unwrapped).

    Same exit-code contract as the rest of the Herdr integration: exit 2 is a syntax bug in THIS
    module (raised as a plain :class:`PaneLifecycleError`), exit 1 carries a structured JSON error
    (raised as :class:`HerdrCliError` so callers can branch on ``.code``), anything else (non-zero
    exit, timeout, missing binary) is a plain failure.
    """
    cmd = herdr_command(machine_id) + args
    label = " ".join(args[:2])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HerdrCliError("timeout", f"herdr {label} did not respond within {timeout}s") from exc
    except OSError as exc:
        raise PaneLifecycleError(f"could not run herdr: {exc}") from exc

    if proc.returncode == 2:
        raise PaneLifecycleError(
            f"herdr {label} rejected its own arguments (exit 2): {(proc.stderr or proc.stdout or '').strip()}"
        )
    if proc.returncode == 1:
        err = _parse_json(proc.stderr) or _parse_json(proc.stdout)
        if isinstance(err, dict) and isinstance(err.get("error"), dict):
            e = err["error"]
            raise HerdrCliError(str(e.get("code", "error")), str(e.get("message", "")))
        raise PaneLifecycleError(f"herdr {label} failed (exit 1): {(proc.stderr or proc.stdout or '').strip()}")
    if proc.returncode != 0:
        raise PaneLifecycleError(f"herdr {label} failed (exit {proc.returncode}): {(proc.stderr or proc.stdout or '').strip()}")

    out = (proc.stdout or "").strip()
    if not out:
        return {}
    parsed = _parse_json(out)
    if isinstance(parsed, dict):
        return parsed.get("result", parsed)
    return parsed if parsed is not None else {"_raw": out}


def _agent_info(target: str, *, machine_id: Optional[str] = None) -> Optional[dict]:
    try:
        result = run_herdr(["agent", "get", target], machine_id=machine_id)
    except HerdrCliError as exc:
        if exc.code == "agent_not_found":
            return None
        raise
    agent = result.get("agent") or result if isinstance(result, dict) else {}
    return agent if isinstance(agent, dict) else {}


def _list_workspaces(*, machine_id: Optional[str] = None) -> list[dict]:
    listing = run_herdr(["workspace", "list"], machine_id=machine_id)
    entries = listing.get("workspaces") if isinstance(listing, dict) else None
    return [ws for ws in (entries or ()) if isinstance(ws, dict)]


def _resolve_workspace_id(label_or_id: str, *, machine_id: Optional[str] = None) -> str:
    """A workspace **id** from either a label or an id -- ``tab create --workspace`` only
    accepts the id, not the label, even though both ``workspace list``/``create`` echo the label."""
    for ws in _list_workspaces(machine_id=machine_id):
        if ws.get("workspace_id") == label_or_id or ws.get("label") == label_or_id:
            workspace_id = ws.get("workspace_id")
            if workspace_id:
                return workspace_id
    raise PaneLifecycleError(f"no Herdr workspace found matching {label_or_id!r} on this machine")


def _unique_name(preferred: str, *, machine_id: Optional[str] = None) -> str:
    """``preferred`` if no live agent already holds it, else a short suffixed variant."""
    base = preferred if _NAME_RE.match(preferred) else re.sub(r"[^a-z0-9_-]", "-", preferred.lower())[:24]
    if not base or not base[0].isalpha():
        base = ("t" + base)[:32]
    if _agent_info(base, machine_id=machine_id) is None:
        return base
    suffix = uuid.uuid4().hex[:6]
    return (base[: 32 - len(suffix) - 1] + "-" + suffix)


def close(target: str, *, machine_id: Optional[str] = None, reason: str = "") -> dict:
    """Capture this agent's resumable identity and persist it, THEN close its pane.

    Returns the persisted record. Raises :class:`PaneLifecycleError` (never closes the pane) when
    Herdr itself has nothing resumable recorded for this agent -- e.g. a non-interactive/unknown
    agent kind with no ``agent_session`` -- rather than closing a pane this module could not
    later reopen.
    """
    info = _agent_info(target, machine_id=machine_id)
    if info is None:
        raise PaneLifecycleError(f"no live Herdr agent found for {target!r}")
    session = info.get("agent_session") or {}
    session_id = session.get("value")
    if session.get("kind") != "id" or not session_id:
        raise PaneLifecycleError(
            f"agent {target!r} has no resumable session id recorded by Herdr "
            f"(agent_session={session!r}); refusing to close a pane this module could not reopen"
        )
    pane_id = info.get("pane_id")
    if not pane_id:
        raise PaneLifecycleError(f"agent {target!r} reported no pane_id; cannot close it safely")
    record = {
        "name": info.get("name") or target,
        "kind": info.get("agent"),
        "session_id": session_id,
        "cwd": info.get("cwd"),
        "workspace_id": info.get("workspace_id"),
        "machine_id": machine_id,
        "terminal_title": info.get("terminal_title_stripped") or info.get("terminal_title"),
        "tokens": info.get("tokens") or {},
        "closed_pane_id": pane_id,
        "closed_at": time.time(),
        "reason": reason,
    }
    run_herdr(["pane", "close", pane_id], machine_id=machine_id)
    store.save_record(record)
    return record


def _resolve_record(target: str, *, kind: Optional[str], machine_id: Optional[str]) -> dict:
    """A persisted record by name, or a synthetic one when ``target`` is itself a session id
    (the honest fallback when nothing was recorded before close: the caller must already know the
    id and supply ``--kind``, since Herdr itself can no longer be asked)."""
    record = store.get_record(target)
    if record is not None:
        return record
    if _UUID_RE.match(target):
        if not kind:
            raise PaneLifecycleError(
                f"{target!r} looks like a raw session id with no recorded close entry; "
                f"--kind is required to reopen it (e.g. --kind claude)"
            )
        return {"name": None, "kind": kind, "session_id": target, "cwd": None,
                "workspace_id": None, "machine_id": machine_id, "tokens": {}}
    raise PaneLifecycleError(
        f"no recorded closed Herdr session found for {target!r}; run 'hermes herdr list' to see "
        f"what is recorded, or pass the raw session id with --kind"
    )


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def reopen(
    target: str, *, pane_id: Optional[str] = None, machine_id: Optional[str] = None,
    kind: Optional[str] = None, workspace: Optional[str] = None, cwd: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """Resume a previously-closed (or otherwise known) agent session into a fresh pane.

    ``machine_id`` overrides the record's own machine (reopening on a different host than the
    one the pane was originally closed on is exactly the cross-host case this exists for).
    ``pane_id`` reuses an already-available pane instead of creating one. Returns the new
    ``agent start`` result's ``agent`` dict, and updates the persisted record to point at the new
    pane so a later ``close``/``reopen`` cycle keeps working.
    """
    record = _resolve_record(target, kind=kind, machine_id=machine_id)
    effective_machine = machine_id if machine_id is not None else record.get("machine_id")
    effective_kind = kind or record.get("kind")
    if not effective_kind:
        raise PaneLifecycleError(f"no agent kind known for {target!r}; pass --kind")
    builder = _RESUME_ARG_BUILDERS.get(effective_kind)
    if builder is None:
        raise PaneLifecycleError(
            f"no known resume syntax for agent kind {effective_kind!r}; supported: "
            f"{sorted(_RESUME_ARG_BUILDERS)}"
        )
    session_id = record.get("session_id")
    if not session_id:
        raise PaneLifecycleError(f"recorded entry for {target!r} has no session id")
    effective_cwd = cwd or record.get("cwd") or os.getcwd()
    new_name = _unique_name(name or record.get("name") or "reopened", machine_id=effective_machine)

    new_pane_id = pane_id
    if new_pane_id is None:
        ws_label_or_id = workspace or record.get("workspace_id")
        if not ws_label_or_id:
            raise PaneLifecycleError(
                f"no workspace known for {target!r} and no --pane/--workspace given; "
                f"pass one of those to place the reopened agent"
            )
        workspace_id = _resolve_workspace_id(ws_label_or_id, machine_id=effective_machine)
        created = run_herdr(
            ["tab", "create", "--workspace", workspace_id, "--cwd", effective_cwd,
             "--label", new_name, "--no-focus"],
            machine_id=effective_machine,
        )
        root_pane = created.get("root_pane") or {} if isinstance(created, dict) else {}
        new_pane_id = root_pane.get("pane_id")
        if not new_pane_id:
            raise PaneLifecycleError(f"tab create did not return root_pane.pane_id: {created!r}")

    resume_args = builder(session_id)
    started = run_herdr(
        ["agent", "start", new_name, "--kind", effective_kind, "--pane", new_pane_id,
         "--timeout", "60000", "--", *resume_args],
        timeout=_AGENT_START_TIMEOUT_S, machine_id=effective_machine,
    )
    agent = started.get("agent") or started if isinstance(started, dict) else {}
    record = {
        **record, "name": new_name, "kind": effective_kind, "session_id": session_id,
        "cwd": effective_cwd, "workspace_id": agent.get("workspace_id") or record.get("workspace_id"),
        "machine_id": effective_machine, "closed_pane_id": None, "reopened_pane_id": new_pane_id,
        "reopened_at": time.time(),
    }
    store.save_record(record)
    return agent
