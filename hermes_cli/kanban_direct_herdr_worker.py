"""Direct Herdr dispatch bridge (card t_cb0795f9), per-board opt-in.

For boards listed in ``kanban.direct_herdr_dispatch_boards``, ``_default_spawn`` in
``kanban_db_dispatch.py`` spawns THIS module instead of a full interactive
``hermes -p <profile> chat -q`` worker. Rather than running its own Hermes agent turn, this
small synchronous process plays the ``skills/devops/herdr-dispatch`` SKILL.md playbook itself:
ensure the board's Herdr workspace exists, create a pane, tag it with the card (the same tokens
the ``kanban_herdr_link`` tool and the ``kanban-herdr`` dashboard plugin use), start a coding
agent in it through the interactive ``herdr agent start`` path (never a raw provider API -- this
is still billed through whichever interactive subscription the board's own agent kind uses,
exactly as today) -- ``--kind`` comes from ``HERMES_KANBAN_HERDR_KIND`` (resolved by the
dispatcher from ``kanban.herdr_board_agents``; this module has no board->kind table of its own,
defaulting to ``claude`` only when the dispatcher didn't set it) -- prompt it, wait for it, read
its report, and clean up. It then marks the kanban card
itself (``request_review`` on success, never ``complete_task`` — a settled Herdr status is not
proof of correct work, so a human still reviews it — or ``block_task`` on failure) and exits with
a real success/failure code.

Nothing about the dispatcher's existing worker lifecycle changes: this process's own PID is
what ``subprocess.Popen`` in ``_default_spawn`` returns, so ``_worker_alive``,
``reap_worker_zombies``, ``detect_crashed_workers``, ``enforce_max_runtime`` and the claim-TTL
heartbeat machinery watch it exactly as they watch the legacy chat worker. All of the task's
dispatcher-set environment (``HERMES_KANBAN_TASK/WORKSPACE/BOARD/BRANCH/RUN_ID/CLAIM_LOCK``,
``HERMES_HOME``, ``HERMES_PROFILE``) is already present when this module starts; it adds no new
spawn surface of its own.

Result/error shapes below are the real ``herdr`` CLI's, read from its own bundled ``--skill``
reference against the installed binary (``herdr.exe`` 0.9.3) and relayed into this build:

- ``workspace create`` -> ``.result.workspace`` / ``.result.tab`` / ``.result.root_pane``.
- ``tab create`` -> ``.result.tab`` / ``.result.root_pane`` (pane id at ``root_pane.pane_id``).
- ``pane split`` -> ``.result.pane`` (pane id at ``pane.pane_id``) — unused here (one pane/card).
- ``agent start`` succeeds only once Herdr confirms the agent is ready for input; on failure it
  returns structured error ``agent_not_ready`` — the agent may still be present, so that specific
  code is not fatal here, it falls through to an ``agent get`` check.
- ``agent prompt ... --wait --timeout <ms>`` can return ``agent_blocked`` (already at an
  approval/question dialog — never auto-answered, always surfaced), ``agent_prompt_stalled`` (no
  activity within 5s of submission), or ``timeout`` (the caller's own timeout elapsed first) —
  neither of the latter two proves non-delivery, so both re-check state via ``agent get`` instead
  of assuming failure or resending.
- ``agent wait`` with no ``--until`` settles on whichever of idle/done/blocked the agent reaches
  first.
- ``agent get`` / ``agent read --source recent-unwrapped --lines 120`` are the read-only
  inspection calls used throughout.
- A herdr CLI *server* error is JSON on stderr with process exit 1; a CLI *syntax* error (this
  bridge passing herdr a flag it rejects) exits 2 and is treated as a bridge bug, not a retryable
  business failure.

Workspace naming: Herdr workspaces are per-project/board already (the live install's one real
workspace is labelled ``cic``, not a generic ``default``), so this bridge targets a workspace
named after the board slug, creating it via ``workspace create`` if ``workspace list`` doesn't
already show one with that label. ``HERMES_KANBAN_HERDR_WORKSPACE`` overrides this when set.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Optional

from hermes_cli import kanban_direct_herdr_handoff as handoff

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DEFAULT_MODEL = "claude-sonnet-5-5"
_HANDOFF_REJECTED = "handoff contract rejected"
_DEFAULT_EFFORT = "medium"
# Used only when the dispatcher didn't set HERMES_KANBAN_HERDR_KIND at all (e.g. a direct manual
# invocation of this module outside kanban_db_dispatch.py). The dispatcher itself always resolves
# a board's real kind from kanban.herdr_board_agents before spawning; this module has no
# board->kind table of its own.
_DEFAULT_KIND = "claude"

# Per-step timeouts (seconds). The dispatcher's own claim TTL / heartbeat is the outer backstop;
# these are the inner one, so a wedged `herdr` call still exits this process instead of hanging
# past the claim TTL and getting force-reclaimed out from under a possibly-still-working pane.
_CLI_TIMEOUT_S = 30
_AGENT_START_TIMEOUT_S = 90
_AGENT_PROMPT_TIMEOUT_S = 45
_AGENT_WAIT_TIMEOUT_S = 600
# A freshly created pane is not a usable shell until its shell has drawn a prompt (herdr 0.9.3:
# ``pane get`` shows ``revision`` 0 and no ``terminal_title`` until then). ``agent start`` into it
# earlier fails ``agent_pane_busy: ... is not an available shell``.
_PANE_SHELL_WAIT_S = 20.0
_PANE_SHELL_POLL_S = 0.25
_START_BUSY_RETRIES = 4
_START_BUSY_BACKOFF_S = 2.0

_SETTLED_STATUSES = ("idle", "done", "finished")
_LAST_SETTLED_AGENT: Optional[str] = None


class BridgeError(RuntimeError):
    """A clean, reportable failure. Never lets the bridge hang or raise uncaught out of ``main``."""


class HerdrCliError(BridgeError):
    """A structured ``{"error": {"code": ..., "message": ...}}`` response from the herdr CLI
    (process exit 1). Callers branch on ``.code`` for the handful of codes that are not fatal on
    their own (``agent_not_ready``, ``agent_blocked``, ``agent_prompt_stalled``, ``timeout``)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class AgentBlockedError(BridgeError):
    """The agent is already sitting at an approval/question dialog. Never auto-answered — this
    always surfaces as a blocker on the card, never a silent retry or a fabricated answer."""


def herdr_command(machine_id: Optional[str] = None) -> list[str]:
    """Resolve the ``herdr`` executable: ``$HERDR_BIN``, then PATH, then the standalone-release
    glob — the same resolution order ``plugins/kanban-herdr/herdr_tool.py`` uses, reimplemented
    here so this bridge has no import dependency on that separately-versioned plugin repo.

    Local calls (``machine_id`` is ``None``) pin ``--session`` (default ``default``, override via
    ``HERMES_KANBAN_HERDR_SESSION``), per the herdr-dispatch skill's own rule: without it, commands
    fall back to whatever session context the calling process happened to inherit rather than the
    recorded local session. A ``machine_id`` call instead targets a saved Herdr machine
    (``herdr --machine <id> ...``, verified live against the real saved-machine list) — that
    machine's own configured session applies server-side, so ``--session`` is never added here;
    ``--machine`` and ``--session`` are alternate top-level modes, not meant to combine.
    """
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
            raise BridgeError("the herdr command isn't installed or isn't on PATH (set HERDR_BIN)")
        base = [found]
    if machine_id:
        return [*base, "--machine", machine_id]
    session = os.environ.get("HERMES_KANBAN_HERDR_SESSION") or "default"  # health: allow HX002 -- dispatcher->worker handoff, same convention as TASK/WORKSPACE/BOARD
    return [*base, "--session", session]


def _parse_json(text: Optional[str]) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text.splitlines()[-1])
    except ValueError:
        return None


def _run_herdr(args: list[str], *, timeout: float = _CLI_TIMEOUT_S, machine_id: Optional[str] = None) -> Any:
    """Run one ``herdr`` subcommand and return its parsed ``.result`` (unwrapped).

    Raises :class:`HerdrCliError` for a structured server error (exit 1 with a JSON error body —
    callers can branch on ``.code``), plain :class:`BridgeError` for a CLI syntax error (exit 2 —
    a bridge-side bug, not a business failure), any other non-zero exit, a timeout, or a
    missing/unreadable binary. Never hangs past ``timeout`` and never lets a subprocess failure
    escape as an uncaught exception. ``machine_id`` runs the same subcommand against a saved
    Herdr machine (``herdr --machine <id> ...``) instead of the local session.
    """
    cmd = herdr_command(machine_id) + args
    label = " ".join(args[:2])
    try:
        # Verified live (herdr 0.9.3 on Windows): `agent read`'s output can carry terminal box-
        # drawing/Unicode content that the default `text=True` console-codepage decode (cp1252)
        # cannot handle — it crashed a subprocess reader thread and silently truncated the
        # captured report to empty. herdr itself emits UTF-8; decode explicitly as that.
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HerdrCliError("timeout", f"herdr {label} did not respond within {timeout}s") from exc
    except OSError as exc:
        raise BridgeError(f"could not run herdr: {exc}") from exc

    if proc.returncode == 2:
        raise BridgeError(
            f"herdr {label} rejected its own arguments (exit 2, a bridge bug): "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
    if proc.returncode == 1:
        err = _parse_json(proc.stderr) or _parse_json(proc.stdout)
        if isinstance(err, dict) and isinstance(err.get("error"), dict):
            e = err["error"]
            raise HerdrCliError(str(e.get("code", "error")), str(e.get("message", "")))
        raise BridgeError(f"herdr {label} failed (exit 1): {(proc.stderr or proc.stdout or '').strip()}")
    if proc.returncode != 0:
        raise BridgeError(f"herdr {label} failed (exit {proc.returncode}): {(proc.stderr or proc.stdout or '').strip()}")

    out = (proc.stdout or "").strip()
    if not out:
        return {}
    parsed = _parse_json(out)
    if isinstance(parsed, dict):
        if "result" in parsed and parsed.get("error"):
            raise BridgeError(f"herdr {label} returned an error with exit 0: {parsed['error']}")
        return parsed.get("result", parsed)
    return parsed if parsed is not None else {"_raw": out}


def pane_name(board: str, task_id: str) -> str:
    """``<board>-<task_id>``, per the herdr-dispatch skill's naming rule (``[a-z][a-z0-9_-]{0,31}``,
    unique among live agents), sanitized as a defensive fallback."""
    name = f"{board}-{task_id}"
    if _NAME_RE.match(name):
        return name
    sanitized = re.sub(r"[^a-z0-9_-]", "-", name.lower())[:32]
    if not sanitized or not sanitized[0].isalpha():
        sanitized = ("t" + sanitized)[:32]
    return sanitized


def _list_workspaces() -> list:
    listing = _run_herdr(["workspace", "list"])
    entries = listing if isinstance(listing, list) else (listing.get("workspaces") if isinstance(listing, dict) else None)
    return [ws for ws in (entries or []) if isinstance(ws, dict)]


def resolve_workspace_id(label_or_id: str) -> str:
    """Resolve a workspace **id** (what ``tab create --workspace`` actually requires — verified
    live against herdr 0.9.3: passing a *label* there fails ``workspace_not_found`` even when
    ``workspace list``/``workspace create`` happily accept and echo that same label back) from
    either a label or an id someone already gave as an override. Raises if neither matches."""
    for ws in _list_workspaces():
        if ws.get("workspace_id") == label_or_id or ws.get("label") == label_or_id:
            workspace_id = ws.get("workspace_id")
            if workspace_id:
                return workspace_id
    raise BridgeError(f"no Herdr workspace found matching {label_or_id!r} (checked id and label)")


def ensure_board_workspace(board: str) -> tuple[str, Optional[str]]:
    """The Herdr workspace **id** for the workspace labelled after the board (e.g. ``titan``),
    creating it via ``workspace create`` if ``workspace list`` doesn't already show one with that
    label. Returns ``(workspace_id, stale_root_pane_id)`` — the id, not the label (see
    :func:`resolve_workspace_id`), plus the id of a just-created workspace's own throwaway root
    pane to close once a real pane exists in it (``None`` when reusing an existing workspace).

    Herdr workspaces are per-project already (the live install's one real workspace is labelled
    ``cic``), so this never falls back to a generic name.

    ``workspace create`` mints its own shell pane (``result.root_pane``), distinct from the one
    ``create_pane``'s later ``tab create`` makes for the actual work. Verified live (herdr 0.9.3)
    that closing it immediately, before that real pane exists, deletes the whole just-created
    workspace outright (an empty workspace is garbage-collected on the spot) — so the caller must
    create the real pane FIRST and only close this one afterward, never here.
    """
    for ws in _list_workspaces():
        if ws.get("label") == board:
            workspace_id = ws.get("workspace_id")
            if workspace_id:
                return workspace_id, None
    created = _run_herdr(["workspace", "create", "--label", board])
    workspace = created.get("workspace") or {} if isinstance(created, dict) else {}
    workspace_id = workspace.get("workspace_id")
    if not workspace_id:
        raise BridgeError(f"workspace create did not return result.workspace.workspace_id: {created!r}")
    root_pane = created.get("root_pane") or {} if isinstance(created, dict) else {}
    return workspace_id, root_pane.get("pane_id")


def create_pane(name: str, workspace_dir: str, *, herdr_workspace: str) -> str:
    result = _run_herdr([
        "tab", "create", "--workspace", herdr_workspace, "--cwd", workspace_dir,
        "--label", name, "--no-focus",
    ])
    root_pane = result.get("root_pane") or {} if isinstance(result, dict) else {}
    pane_id = root_pane.get("pane_id")
    if not pane_id:
        raise BridgeError(f"tab create did not return root_pane.pane_id: {result!r}")
    return pane_id


def link_pane(pane_id: str, task_id: str, board: str) -> None:
    """Tag the pane exactly as ``kanban_herdr_link(action="link", ...)`` / the skill's
    ``pane report-metadata`` fallback would, then read it back and verify (same contract as the
    tool: a tag that doesn't read back the same is a bridge failure, not a silent partial link)."""
    _run_herdr([
        "pane", "report-metadata", pane_id, "--source", "kanban-link",
        "--token", f"kanban_task={task_id}", "--token", f"kanban_board={board}",
    ])
    after = _run_herdr(["pane", "get", pane_id])
    pane = after.get("pane") or after if isinstance(after, dict) else {}
    tokens = pane.get("tokens") or {}
    if tokens.get("kanban_task") != task_id or tokens.get("kanban_board") != board:
        raise BridgeError(f"pane {pane_id} tag did not verify after linking: {tokens!r}")


def wait_pane_shell_ready(pane_id: str, timeout_s: float = _PANE_SHELL_WAIT_S) -> bool:
    """Best-effort wait until a just-created pane's shell has drawn its prompt, so ``agent start``
    (which requires an interactive shell prompt) doesn't hit ``agent_pane_busy``. Read-only.

    Ready means ``pane get`` reports a ``terminal_title`` or ``revision`` > 0. A response with
    neither field at all is treated as ready (nothing to wait on). Returns ``False`` on timeout;
    the caller still proceeds, since :func:`start_agent` retries ``agent_pane_busy`` itself.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        result = _run_herdr(["pane", "get", pane_id])
        pane = result.get("pane") or result if isinstance(result, dict) else {}
        if pane.get("terminal_title") or (pane.get("revision") or 0) > 0:
            return True
        if "revision" not in pane and "terminal_title" not in pane:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PANE_SHELL_POLL_S)


def _pane_tokens(pane_id: str) -> dict:
    """Read-only lookup of a pane's current tags -- unlike :func:`link_pane`, never writes
    anything, so it can be used to check a pane's existing ownership before touching it."""
    result = _run_herdr(["pane", "get", pane_id])
    pane = result.get("pane") or result if isinstance(result, dict) else {}
    return pane.get("tokens") or {}


def _agent_info(name: str, *, machine_id: Optional[str] = None) -> Optional[dict]:
    """The raw ``agent get <name>`` response's ``agent`` dict, or ``None`` if the agent truly
    doesn't exist (``agent_not_found``). Lower-level than :func:`agent_status`: a caller that needs
    to tell "doesn't exist" apart from "exists but reports an empty/unrecognized status" -- which
    collapsing straight to a status string can't do -- uses this instead.
    """
    try:
        result = _run_herdr(["agent", "get", name], machine_id=machine_id)
    except HerdrCliError as exc:
        if exc.code == "agent_not_found":
            return None
        raise
    agent = result.get("agent") or result if isinstance(result, dict) else {}
    return agent if isinstance(agent, dict) else {}


def agent_exists(name: str) -> bool:
    """Whether Herdr knows an agent by this name at all, regardless of its status field's value."""
    return _agent_info(name) is not None


def agent_status(name: str, *, machine_id: Optional[str] = None) -> str:
    """The agent's live status, or ``""`` if no agent by that name exists (or one exists but
    doesn't report a status -- see :func:`agent_exists` when that distinction matters).

    Verified live (herdr 0.9.3): ``agent get`` on a name with no agent returns the structured
    error ``agent_not_found``, not an empty/absent result.
    """
    agent = _agent_info(name, machine_id=machine_id)
    # Verified live (herdr 0.9.3): the field is `agent_status`, not `status`.
    return str((agent or {}).get("agent_status") or "").lower()


def _list_agents(*, machine_id: Optional[str] = None) -> list[dict]:
    """The raw ``agent list`` entries (each already carrying its own ``tokens`` dict, if tagged),
    local or (``machine_id`` set) on a saved Herdr machine."""
    result = _run_herdr(["agent", "list"], machine_id=machine_id)
    agents = result.get("agents") if isinstance(result, dict) else None
    return [a for a in (agents or ()) if isinstance(a, dict)]


def find_existing_agent(name: str) -> Optional[dict]:
    """The live local ``agent list`` entry named ``name``, or ``None``.

    Goes through ``agent list`` rather than ``agent get <name>`` + :func:`agent_status`: a crashed
    attempt's existing agent is one entry among however many others are live, and ``agent list``'s
    entry shape (including ``pane_id`` alongside ``agent_status``) was captured directly from a
    real run and is already trusted.
    """
    for agent in _list_agents():
        if agent.get("name") == name:
            return agent
    return None


def list_saved_herdr_machines() -> list[dict]:
    """Every enabled saved Herdr machine (``herdr machine list --json``): ``[{"id", "label",
    "target"}, ...]``. Unlike every other call in this file, ``machine list --json`` emits
    pretty-printed (multi-line) JSON, not the socket API's single-line form :func:`_parse_json`
    expects, so this parses the whole captured stdout directly instead of going through
    :func:`_run_herdr`. Raises :class:`BridgeError` on any failure -- a caller sweeping for
    existing sessions elsewhere must tell "no other machines" apart from "could not even ask",
    since those mean opposite things for whether it's safe to proceed.
    """
    cmd = herdr_command() + ["machine", "list", "--json"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_CLI_TIMEOUT_S, stdin=subprocess.DEVNULL, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise BridgeError(f"could not list saved Herdr machines: {exc}") from exc
    if proc.returncode != 0:
        raise BridgeError(
            f"herdr machine list --json failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
    try:
        machines = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        raise BridgeError(f"herdr machine list --json returned unparsable output: {exc}") from exc
    if not isinstance(machines, list):
        raise BridgeError(f"herdr machine list --json returned a non-list: {machines!r}")
    return [
        m for m in machines
        if isinstance(m, dict) and m.get("id") and m.get("enabled", True)
    ]


def sweep_other_hosts_for_task(task_id: str) -> tuple[list[dict], list[str], list[str]]:
    """Every OTHER saved Herdr machine's panes already tagged ``kanban_task=<task_id>`` (this
    card's own existing Herdr state elsewhere), which machines could not be checked, and which
    machines WERE successfully checked (for an honest "checked X, found nothing" report).

    Local panes are intentionally excluded: :func:`run` already has its own, more carefully
    TOCTOU-guarded local-reuse path (matched by :func:`pane_name`, not by tag). This only covers
    what that path cannot see -- a session on a different machine entirely, reached through one of
    Herdr's own saved remote targets.

    Returns ``(matches, unreachable_labels, checked_labels)``. A caller must treat ANY unreachable
    machine as "cannot rule out a session existing there" -- never as "no match found" -- since a
    card's work being spread across more than one host is normal (not every host, and not always
    the one this dispatcher happens to run on). Not being ABLE to enumerate saved machines at all
    (an old ``herdr`` without ``machine list --json``, or none configured) is different: that's the
    ordinary single-machine case, not a sign something is hidden, so it resolves to "zero machines
    to check" rather than blocking every dispatch on this install.
    """
    try:
        machines = list_saved_herdr_machines()
    except BridgeError:
        return [], [], []
    matches: list[dict] = []
    unreachable: list[str] = []
    checked: list[str] = []
    for m in machines:
        machine_id, label = m["id"], str(m.get("label") or m["id"])
        try:
            agents = _list_agents(machine_id=machine_id)
        except BridgeError:
            unreachable.append(label)
            continue
        checked.append(label)
        for agent in agents:
            tokens = agent.get("tokens") or {}
            if tokens.get("kanban_task") == task_id:
                matches.append({**agent, "_machine_id": machine_id, "_machine_label": label})
    return matches, unreachable, checked


def _settle_and_close(name: str, pane_id: Optional[str], *, machine_id: Optional[str] = None) -> tuple[bool, str, bool]:
    """Wait out the agent, then close its pane only if that's actually safe.

    Returns ``(success, report, pane_preserved)``. Re-checks the agent's live status right before
    closing (not just the status that got the caller here) per the herdr-dispatch skill's cleanup
    rule: a blocked or still-working agent's pane is never closed, so a human can still reach it.
    Only an agent confirmed genuinely settled (idle/done/finished) by BOTH checks is closed.
    """
    global _LAST_SETTLED_AGENT
    _LAST_SETTLED_AGENT = name
    status = wait_agent(name, _AGENT_WAIT_TIMEOUT_S * 1000, machine_id=machine_id)
    if status == "blocked":
        return False, f"agent {name} is blocked on an approval/question dialog: {read_agent(name, machine_id=machine_id)}", True
    if status not in _SETTLED_STATUSES:
        return False, f"agent {name} did not reach idle/done before the wait timeout (status={status!r})", True
    # Re-verify immediately before closing: `wait_agent` can itself time out and return a stale
    # settled-looking read, or the agent can flip to blocked/working/some other status between the
    # wait returning and this check. Require the live status to still be positively settled rather
    # than excluding a few known-unsafe ones -- an empty, "unknown", or any future status this file
    # doesn't recognize must not fall through to "safe to close" by default.
    live_status = agent_status(name, machine_id=machine_id)
    if live_status not in _SETTLED_STATUSES:
        return False, (
            f"agent {name} reported {status!r} on wait but is now {live_status!r}, not confirmed "
            f"settled; not closing its pane: {read_agent(name, machine_id=machine_id)}"
        ), True
    # Read the report BEFORE closing: closing a pane tears down the agent living in it (verified
    # live -- a `read` issued after `close_pane` gets `agent_not_found`, not the transcript).
    report = read_agent(name, machine_id=machine_id)
    # Do not treat a settled session as delivered work. Validate artifact claims against the
    # task workspace before requesting human review. The review gate must reject setup-only
    # transcripts and handoff scripts, leaving the card retryable instead of laundering a
    # worker's premature Review transition into the authoritative handoff.
    # `read_agent` is itself a real call that can take time -- recheck once more immediately
    # before closing so an agent that resumed DURING the read doesn't still get its pane closed
    # out from under it. The work already succeeded (the report above is good); a resume here only
    # changes whether it's safe to close, not whether this run counts as a success.
    final_status = agent_status(name, machine_id=machine_id)
    if final_status not in _SETTLED_STATUSES:
        return True, report, True
    closed = True
    if pane_id is not None:
        closed = close_pane(pane_id)
    return True, report, not closed


def _herdr_agent_args(kind: str, model: str, effort: str) -> list[str]:
    """Return harness-specific supported flags and the requested permission bypass.

    Claude model/effort defaults never leak into another harness: Codex keeps its own model config
    when handed a Claude default, and Windows Codex stays in the owned terminal (``--no-daemon``)
    rather than attaching to the shared daemon.
    """
    if kind == "codex":
        args = ["--model", model] if model and not model.lower().startswith("claude") else []
        if effort:
            args += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
        args.append("--yolo")
        if os.name == "nt":
            args.append("--no-daemon")
        return args
    if kind == "claude":
        return ["--model", model, "--effort", effort, "--dangerously-skip-permissions"]
    return []


def _herdr_agent_profile_for_worker(
    board: str, kind: str, model: str, effort: str, *,
    has_kind_override: bool = False, has_model_override: bool = False, has_effort_override: bool = False,
) -> tuple[str, str, str]:
    """Read effective board defaults while retaining explicit per-card overrides."""
    try:
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_cli.kanban_db_dispatch_herdr import _handoff_profile_config_path

        config = load_user_config_effective(config_path=_handoff_profile_config_path(), fail_closed=True)
        entry = (config or {}).get("kanban", {}).get("herdr_board_agents", {}).get(board, {})
        if isinstance(entry, dict):
            return (
                str(kind if has_kind_override else entry.get("kind") or kind),
                str(model if has_model_override else entry.get("model") or model),
                str(effort if has_effort_override else entry.get("effort") or effort),
            )
    except Exception as exc:  # health: allow BLE001 -- config read boundary; falls back to the dispatcher's values
        print(f"Could not resolve {board!r} Herdr settings from dispatch profile; using dispatcher values: {exc}", file=sys.stderr)
    return kind, model, effort


def start_agent(name: str, pane_id: str, model: str, effort: str, kind: str = _DEFAULT_KIND) -> None:
    """Start the configured interactive harness in the task-owned pane with supported flags.

    Codex receives its model, the configured effort and ``--yolo``. Claude receives its
    model, configured effort and ``--dangerously-skip-permissions``.
    """
    agent_args = _herdr_agent_args(kind, model, effort)
    try:
        for attempt in range(_START_BUSY_RETRIES + 1):
            try:
                _run_herdr([
                    "agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "60000",
                    *(["--", *agent_args] if agent_args else []),
                ], timeout=_AGENT_START_TIMEOUT_S)
                return
            except HerdrCliError as exc:
                # The pane's shell may still be initializing; nothing has started, so a bounded
                # retry is safe. Any other code (or the last attempt) propagates unchanged.
                if exc.code != "agent_pane_busy" or attempt >= _START_BUSY_RETRIES:
                    raise
                time.sleep(_START_BUSY_BACKOFF_S)
    except HerdrCliError as exc:
        if exc.code != "agent_not_ready":
            raise
        info = _agent_info(name)
        if info is None:
            raise BridgeError(f"agent {name} start reported agent_not_ready and no agent is present") from exc
        other_pane = info.get("pane_id")
        if other_pane not in (None, pane_id):
            raise BridgeError(
                f"agent {name} start reported agent_not_ready, and an agent by that name exists "
                f"but in pane {other_pane!r}, not the pane just created ({pane_id!r}); refusing to "
                f"treat it as this card's own"
            ) from exc
        # Present in this pane (or pane_id unreported), just not confirmed ready yet -- the
        # prompt/wait steps observe its real state.


def prompt_agent(name: str, text: str, *, machine_id: Optional[str] = None) -> None:
    """``herdr agent prompt <name> "<text>" --wait --timeout <ms>``.

    ``agent_blocked`` means the agent is already at an approval/question dialog -- surfaced as
    :class:`AgentBlockedError`, never auto-answered. ``agent_prompt_stalled`` / ``timeout`` do not
    prove the prompt wasn't delivered, so both re-check state instead of assuming failure or
    resending (a resend after real delivery would double-prompt the agent).
    """
    try:
        # Verified live on a busy machine (15+ concurrent real Claude Code agent panes): a
        # cold-started interactive session can take noticeably longer than 10s just to produce its
        # first observed activity, well short of being stuck. 30s gives real resource contention
        # room without masking an actually-wedged agent for long. Not separately verified for
        # Codex agents, but the same timeout applies to both -- the risk is the same direction
        # (too-short masks a slow-but-fine cold start), not a new one.
        _run_herdr(
            ["agent", "prompt", name, text, "--wait", "--timeout", "30000"],
            timeout=_AGENT_PROMPT_TIMEOUT_S, machine_id=machine_id,
        )
    except HerdrCliError as exc:
        if exc.code == "agent_blocked":
            raise AgentBlockedError(f"agent {name} is already blocked on a dialog: {exc}") from exc
        if exc.code in ("agent_prompt_stalled", "timeout"):
            status = agent_status(name, machine_id=machine_id)
            if status in ("blocked", "busy", "working", "running"):
                return  # delivered -- this call's job (handing the prompt off) is done
            raise BridgeError(f"agent {name} prompt could not be confirmed delivered ({exc.code})") from exc
        raise


def wait_agent(name: str, timeout_ms: int, *, machine_id: Optional[str] = None) -> str:
    """``herdr agent wait <name> --timeout <ms>`` with no ``--until``: settles on whichever of
    idle/done/blocked the agent reaches first. Returns the settled status."""
    result = _run_herdr(
        ["agent", "wait", name, "--timeout", str(timeout_ms)],
        timeout=(timeout_ms / 1000.0) + 30, machine_id=machine_id,
    )
    agent = result.get("agent") or result if isinstance(result, dict) else {}
    # Verified live (herdr 0.9.3): the field is `agent_status`, not `status`.
    return str(agent.get("agent_status") or "").lower()


def read_agent(name: str, *, machine_id: Optional[str] = None) -> str:
    """``herdr agent read <name> --source recent-unwrapped --lines 120`` -- the read-only
    transcript inspection call, soft-wrapped lines joined."""
    result = _run_herdr(
        ["agent", "read", name, "--source", "recent-unwrapped", "--lines", "120"], machine_id=machine_id,
    )
    if isinstance(result, dict):
        return result.get("text") or result.get("output") or result.get("_raw") or ""
    return str(result) if result else ""


def close_pane(pane_id: str) -> bool:
    """Best-effort cleanup: a pane that is already gone is not a bridge failure, but a real close
    failure is reported to the caller (as ``False``) rather than silently assumed to have
    succeeded -- callers that care about leaked panes act on the return value."""
    try:
        _run_herdr(["pane", "close", pane_id])
        return True
    except BridgeError:
        return False


def _compose_prompt(task_id: str, board: Optional[str], run_id: Optional[int] = None) -> str:
    """``"work kanban task <id>"`` enriched with the card's own title/description when the DB is
    reachable, so the agent's first move isn't a blind re-fetch of the card it was already handed.
    Best-effort: any read failure falls back to the bare instruction rather than blocking dispatch
    on a problem the agent itself can also just fix by reading the card.

    On a board that enforces the ``/open-pr`` handoff contract, the instructions for it are
    appended (``run_id`` names the run the handoff block must be tied to).
    """
    base = f"work kanban task {task_id}"
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        conn = kbc.connect(board=board)
        try:
            task = kb.get_task(conn, task_id)
        finally:
            conn.close()
    except Exception:  # health: allow BLE001 -- best-effort enrichment only; any DB/connect
        # failure here (missing board, locked file, schema drift) must fall back to the bare
        # instruction, never block dispatch over a problem the agent can fix by reading the card.
        task = None
    lines = [base]
    if task is not None:
        lines += ["", f"Title: {task.title}"]
        if task.body:
            lines += ["", task.body.strip()]
    prompt = "\n".join(lines)
    if run_id is not None and _requires_open_pr_handoff(board):
        prompt += "\n" + handoff.contract_instructions(task_id, run_id)
    return prompt


def _requires_open_pr_handoff(board: Optional[str]) -> bool:
    from hermes_cli.kanban_db_dispatch_herdr import _board_requires_open_pr_handoff

    return _board_requires_open_pr_handoff(board)


def _annotate_preserved_pane(success: bool, report: str, pane_preserved: bool) -> str:
    """Surface ``_settle_and_close``'s ``pane_preserved`` signal in the report text itself --
    computing it and then dropping it on the floor would make the TOCTOU/close-failure handling in
    ``_settle_and_close`` pointless, since nothing downstream (the card summary a human reads)
    would ever know the pane needs a look."""
    if success and pane_preserved:
        return (
            f"{report}\n\n[direct-herdr-dispatch bridge] work succeeded, but its Herdr pane/agent "
            f"could not be confirmed closed (it may have resumed activity during cleanup, or the "
            f"close call itself failed) -- needs manual follow-up to verify/close it."
        )
    return report


def run(task_id: str, board: str, workspace_dir: str, model: str, effort: str,
        herdr_workspace: Optional[str] = None, kind: str = _DEFAULT_KIND,
        run_id: Optional[int] = None) -> tuple[bool, str]:
    """Drive one card through the herdr-dispatch skill's workspace -> pane -> link -> agent
    lifecycle.

    Returns ``(success, report_text)`` and never raises: every ``herdr``/agent failure becomes
    ``(False, reason)`` so ``main()`` can mark the card and exit cleanly instead of hanging or
    crashing with a traceback. ``herdr_workspace`` overrides the board-derived workspace
    (``HERMES_KANBAN_HERDR_WORKSPACE`` in ``main()`` — a label or an id, resolved either way);
    otherwise the workspace is the one labelled after the board slug.

    If an agent named for this card is already live (a dispatcher retry after this bridge's own
    process crashed mid-run), that agent is reused instead of spawning a second one onto the same
    pane slot. Its pane's CURRENT tag is read (read-only, before anything is written) and must
    already be unset or match this card -- a pane that matches by name but is already tagged for a
    different card isn't safely identifiable as this card's own, and is refused rather than
    adopted. This has to happen before :func:`link_pane`, not after: that call's own
    ``report-metadata`` step unconditionally overwrites the tag, so a write-then-verify would just
    confirm the write it itself made, never the pane's prior ownership. A genuinely mid-flight
    agent (working/busy/blocked) goes straight to the wait/close path below; anything else (idle,
    or any other status) gets the task prompt (re-)sent first: an `idle` status here can't be
    trusted to mean "already handled this" -- the previous attempt may have crashed between
    starting this agent and ever prompting it, and resending to an agent that already finished the
    work is a safe no-op (it just re-confirms), while silently waiting on a never-prompted agent
    would report a false success built from an unrelated transcript.

    Known residual risk, accepted rather than chased further: the ownership read above and the
    close-safety recheck in ``_settle_and_close`` are both check-then-act, not atomic. Something
    else retagging this exact pane between the read and ``link_pane``'s write, or an agent resuming
    in the instant between the final status recheck and the close call, are both real TOCTOU
    windows this bridge cannot fully close without an atomic conditional-update primitive from
    Herdr that isn't known to exist. Narrowing these further would mean guessing at unverified CLI
    behavior, which this file deliberately avoids; both are narrow enough in practice (local
    subprocess calls, not network round trips) that the live dry runs validating this bridge never
    triggered either one.
    """
    global _LAST_SETTLED_AGENT
    _LAST_SETTLED_AGENT = None
    name = pane_name(board, task_id)
    try:
        existing = find_existing_agent(name)
        if existing is not None:
            pane_id = existing.get("pane_id")
            if not pane_id:
                return False, (
                    f"agent {name} already exists but reported no pane_id; cannot verify "
                    f"ownership before reuse"
                )
            tokens = _pane_tokens(pane_id)
            owner_task = tokens.get("kanban_task")
            owner_board = tokens.get("kanban_board")
            if owner_task not in (None, task_id) or owner_board not in (None, board):
                return False, (
                    f"agent {name}'s pane {pane_id} is already tagged for a different card "
                    f"(kanban_task={owner_task!r}, kanban_board={owner_board!r}); refusing to "
                    f"adopt it"
                )
            link_pane(pane_id, task_id, board)
            status = agent_status(name)
            if status not in ("working", "busy", "blocked", "running"):
                try:
                    prompt_agent(name, _compose_prompt(task_id, board, run_id))
                except AgentBlockedError as exc:
                    return False, (
                        f"agent {name} is blocked on an approval/question dialog and was not "
                        f"auto-answered: {exc}"
                    )
            success, report, pane_preserved = _settle_and_close(name, pane_id)
            return success, _annotate_preserved_pane(success, report, pane_preserved)

        if herdr_workspace:
            workspace_id, stale_root_pane_id = resolve_workspace_id(herdr_workspace), None
        else:
            workspace_id, stale_root_pane_id = ensure_board_workspace(board)
        pane_id = create_pane(name, workspace_dir, herdr_workspace=workspace_id)
        try:
            if stale_root_pane_id:
                # The real pane now exists, so the just-created workspace's throwaway root pane is
                # safe to close without deleting the workspace out from under it (see
                # ensure_board_workspace's docstring for why this can't happen any earlier).
                close_pane(stale_root_pane_id)
            link_pane(pane_id, task_id, board)
            wait_pane_shell_ready(pane_id)
        except BridgeError:
            # link_pane only ever touches pane metadata -- no agent exists yet at this point, so a
            # pane that fails to tag is safe to drop outright.
            close_pane(pane_id)
            raise
        try:
            start_agent(name, pane_id, model, effort, kind=kind)
        except BridgeError as exc:
            # Unlike link_pane, start_agent's own failure (including a bare CLI timeout) doesn't
            # prove the agent was never created -- the server may have created and started it
            # despite a lost or slow response. Check before discarding a pane that could already
            # hold a live executor. Existence alone isn't enough: a same-named agent reported in a
            # DIFFERENT pane than the one just created here isn't this card's agent either (this
            # bridge tracks state by pane_id, not just name) -- that pane is still safe to drop.
            info = _agent_info(name)
            if info is not None and info.get("pane_id") in (None, pane_id):
                return False, (
                    f"agent {name} start failed ambiguously ({exc}) but the agent exists; its "
                    f"pane is preserved for investigation rather than discarded"
                )
            close_pane(pane_id)
            raise
        try:
            prompt_agent(name, _compose_prompt(task_id, board, run_id))
        except AgentBlockedError as exc:
            return False, (
                f"agent {name} is blocked on an approval/question dialog and was not "
                f"auto-answered: {exc}"
            )
        report = read_agent(name)
        success, report, pane_preserved = _settle_and_close(name, pane_id)
        return success, _annotate_preserved_pane(success, report, pane_preserved)
    except BridgeError as exc:
        return False, str(exc)


def _continue_remote_matches(matches: list[dict], task_id: str, board: Optional[str]) -> tuple[bool, str]:
    """Pick up a card whose Herdr state already exists on another machine, instead of spawning an
    unrelated fresh agent on top of it (the GSD incident, card t_a87265e1: the real writer/reviewer
    sessions were sitting right here all along, tagged and inspectable, when a fresh local agent
    got started over them).

    This is a handshake, not a refusal: every match gets acknowledged by name/host/pane in the
    returned report, and a still-active one is waited on (so real in-flight work gets the chance to
    finish) rather than abandoned. Only a genuinely blocked match (sitting at an approval dialog)
    or one that never settles within the wait budget keeps this from counting as a clean handoff --
    and even then, the report says exactly which match and why, never just "refused".

    Driving a match further than a wait+read (e.g. actively steering a remote agent toward
    completion) is deliberately out of scope here: this bridge now acknowledges and surfaces
    cross-host state it previously had zero awareness of, which is the actual gap the incident
    exposed. Teaching it to run a full remote work session is a separate, larger step.
    """
    lines = [
        f"[direct-herdr-dispatch bridge] found {len(matches)} existing Herdr session(s) for this "
        f"card on another machine; continuing from there instead of starting a new one:",
    ]
    any_unsettled = False
    for match in matches:
        name = match.get("name") or "(unnamed)"
        host = match.get("_machine_label", "unknown host")
        machine_id = match.get("_machine_id")
        pane_id = match.get("pane_id")
        status = str(match.get("agent_status") or "").lower()
        if status in ("working", "busy", "running"):
            status = wait_agent(name, _AGENT_WAIT_TIMEOUT_S * 1000, machine_id=machine_id)
        if status == "blocked":
            any_unsettled = True
            lines.append(
                f"- {name} on {host} (pane {pane_id}): blocked on an approval/question dialog, not "
                f"auto-answered: {read_agent(name, machine_id=machine_id)}"
            )
            continue
        if status not in _SETTLED_STATUSES:
            any_unsettled = True
            lines.append(
                f"- {name} on {host} (pane {pane_id}): did not reach idle/done within the wait "
                f"budget (status={status!r})"
            )
            continue
        lines.append(
            f"- {name} on {host} (pane {pane_id}, status={status!r}):\n"
            f"{read_agent(name, machine_id=machine_id).strip()}"
        )
    return not any_unsettled, "\n\n".join(lines)


def dispatch(
    task_id: str, board: str, workspace_dir: str, model: str, effort: str,
    herdr_workspace: Optional[str] = None, kind: str = _DEFAULT_KIND, run_id: Optional[int] = None,
) -> tuple[bool, str, str, Optional[str], Optional[str]]:
    """Entry point for :func:`main`: checks for this card's existing Herdr state on OTHER
    machines first, then falls through to :func:`run` (which already covers the local case).

    Returns ``(success, report, block_kind)`` -- ``block_kind`` only matters when ``success`` is
    ``False`` and is ``"transient"`` (today's existing behaviour: auto-retryable) except for the
    one case that genuinely can't be resolved mechanically: a saved machine this sweep could not
    reach at all. That case alone gets ``"needs_input"`` -- not because a session was found, but
    because the sweep cannot say whether one exists, and guessing either way risks exactly the
    double-dispatch this whole check exists to prevent.
    """
    matches, unreachable, checked = sweep_other_hosts_for_task(task_id)
    if unreachable:
        return False, (
            f"[direct-herdr-dispatch bridge] could not confirm whether this card already has a "
            f"Herdr session on: {', '.join(unreachable)}. A card's work can be spread across more "
            f"than one machine, so this is not safe to guess either way -- nothing was started "
            f"here. Check connectivity to that machine, then retry."
        ), "needs_input", None, None
    if matches:
        success, report = _continue_remote_matches(matches, task_id, board)
        return success, report, "transient", str(matches[0].get("name") or "") or None, matches[0].get("_machine_id") if success else None
    success, report = run(
        task_id, board, workspace_dir, model, effort, herdr_workspace=herdr_workspace, kind=kind,
        run_id=run_id,
    )
    # Appended, not prepended: request_review's event payload keeps only the summary's FIRST
    # LINE as its headline (`_first_line`), so this housekeeping note must never push the actual
    # work's own report out of that line.
    where = ', '.join(checked) if checked else "none configured"
    suffix = (
        f"\n\n[direct-herdr-dispatch bridge] checked for an existing Herdr session elsewhere "
        f"(saved machines checked: {where}); none found; proceeded here."
    )
    return success, report + suffix, "transient", _LAST_SETTLED_AGENT, None


def _archive_transcript(task_id: str, board: Optional[str], run_id: int, transcript: str) -> Optional[str]:
    """Keep the raw terminal capture as a task attachment (audit only, never the Review summary).
    Returns the stored filename, or ``None`` when there was nothing to keep or it could not be
    stored: the card transition must not depend on its own audit copy."""
    if not transcript.strip():
        return None
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    try:
        conn = kbc.connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn, task_id, f"herdr-transcript-run{run_id}.txt", transcript.encode("utf-8"),
                content_type="text/plain", uploaded_by="direct-herdr-bridge", board=board,
            )
            att = kb.get_attachment(conn, att_id)
        finally:
            conn.close()
    except Exception as exc:  # health: allow BLE001 -- audit copy is best-effort by design
        print(f"kanban_direct_herdr_worker: could not archive the raw transcript: {exc}", file=sys.stderr)
        return None
    return att.filename if att else None


def _last_comment_body(task_id: str, board: Optional[str]) -> Optional[str]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        comments = kb.list_comments(conn, task_id)
    finally:
        conn.close()
    return max(comments, key=lambda c: (c.created_at or 0, c.id)).body if comments else None


def _apply_handoff_contract(
    task_id: str, board: str, run_id: int, success: bool, report: str, block_kind: str,
) -> tuple[bool, str, str]:
    """On a board enforcing the ``/open-pr`` handoff contract, turn the settled agent's own report
    into a Review receipt only when it carries a deliberate handoff (see
    ``kanban_direct_herdr_handoff``); the raw terminal capture is archived as an attachment, never
    used as the summary. A rejected handoff becomes a ``needs_input`` failure receipt (sticky, not
    auto-retried) with the transcript tail as evidence. Other boards and bridge failures pass
    through unchanged.
    """
    if not success or not _requires_open_pr_handoff(board):
        return success, report, block_kind
    result = handoff.resolve_handoff(
        report, _compose_prompt(task_id, board, run_id), task_id, run_id,
        _last_comment_body(task_id, board),
    )
    archived = _archive_transcript(task_id, board, run_id, report)
    audit = f"\n\nRaw terminal capture kept as attachment {archived}." if archived else ""
    if not result.ok:
        return False, result.reason + audit, "needs_input"
    summary = result.summary
    if "could not be confirmed closed" in report:
        summary += "\n\nThe Herdr pane could not be confirmed closed; check it."
    if result.source == "card_comment":
        summary += "\n\nHandoff taken from the card's last comment."
    return True, summary + audit, block_kind


def _mark_card(
    task_id: str, board: Optional[str], run_id: Optional[int], success: bool, report: str,
    block_kind: str = "transient", workspace_dir: Optional[str] = None,
    settlement_owner: bool = False, agent_name: Optional[str] = None,
    machine_id: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Verify the exact settled Herdr executor and persist its run-scoped receipt."""
    from hermes_cli import kanban_settlement as settlement

    if not settlement_owner:
        raise BridgeError("dispatcher settlement ownership is required")
    if not agent_name:
        raise BridgeError("settled native agent identity is missing")
    status = agent_status(agent_name, machine_id=machine_id)
    if status not in _SETTLED_STATUSES:
        raise BridgeError(f"native Herdr agent is not settled: {status!r}")
    report = read_agent(agent_name, machine_id=machine_id).strip()
    if success and _requires_open_pr_handoff(board):
        # On an /open-pr board the verified handoff is the deliverable, not a workspace file.
        success, summary, block_kind = _apply_handoff_contract(task_id, board, run_id, success, report, block_kind)
        saved, why = _record_receipt(task_id, board, run_id, success, summary, (), block_kind, False)
        if saved and not success:
            return True, f"{_HANDOFF_REJECTED}; durable needs_input receipt recorded"
        return saved, why
    review_summary = settlement._review_summary(report)
    artifacts = settlement._verified_review_artifacts(report, workspace_dir or "") if success else ()
    # A success report with no verified deliverable stays a success receipt that carries no artifacts:
    # settlement (require_artifacts=True) turns it into a truthful partial that keeps the owner, instead
    # of a transient block that would drop the card out of the queue.
    if success and artifacts:
        artifact_names = ", ".join(artifacts[:4])
        review_summary = f"Deliverable ready for review: {artifact_names}. {review_summary}".strip()
    return _record_receipt(task_id, board, run_id, success, review_summary, artifacts, block_kind, True)


def _record_receipt(
    task_id: str, board: Optional[str], run_id: Optional[int], success: bool, report: str,
    artifacts, block_kind: str, require_artifacts: bool,
) -> tuple[bool, Optional[str]]:
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_settlement as settlement

    conn = kbc.connect(board=board)
    try:
        saved = settlement.record_result_receipt(
            conn, task_id, run_id, success=success, report=report,
            artifacts=artifacts, block_kind=block_kind, require_artifacts=require_artifacts,
        )
        if not saved:
            return False, "result receipt was already present or the run fence no longer matched"
        return True, "durable result receipt recorded; dispatcher owns lifecycle settlement"
    finally:
        conn.close()


def main() -> int:
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    board = os.environ.get("HERMES_KANBAN_BOARD")
    workspace_dir = os.environ.get("HERMES_KANBAN_WORKSPACE")
    herdr_workspace = os.environ.get("HERMES_KANBAN_HERDR_WORKSPACE") or None  # health: allow HX002 -- dispatcher->worker handoff, same convention as TASK/WORKSPACE/BOARD
    if not task_id or not board or not workspace_dir:
        print("kanban_direct_herdr_worker: missing task/board/workspace; dispatcher spawn required.", file=sys.stderr)
        return 1

    model = os.environ.get("HERMES_KANBAN_HERDR_MODEL", _DEFAULT_MODEL)  # health: allow HX002 -- dispatcher->worker handoff, same convention as TASK/WORKSPACE/BOARD
    effort = os.environ.get("HERMES_KANBAN_HERDR_EFFORT", _DEFAULT_EFFORT)  # health: allow HX002 -- same handoff convention
    kind = os.environ.get("HERMES_KANBAN_HERDR_KIND", _DEFAULT_KIND)  # health: allow HX002 -- same handoff convention, resolved from kanban.herdr_board_agents by the dispatcher
    kind, model, effort = _herdr_agent_profile_for_worker(
        board, kind, model, effort,
        has_kind_override="HERMES_KANBAN_HERDR_KIND" in os.environ,  # health: allow HX002 -- same handoff convention
        has_model_override="HERMES_KANBAN_HERDR_MODEL" in os.environ,  # health: allow HX002 -- same handoff convention
        has_effort_override="HERMES_KANBAN_HERDR_EFFORT" in os.environ,  # health: allow HX002 -- same handoff convention
    )
    # A missing or malformed run id would drop the run-ownership fence that keeps a stale or
    # duplicate run from overwriting a card a newer run already owns: a hard failure, never a fallback.
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    if raw_run_id is None:
        print("kanban_direct_herdr_worker: missing HERMES_KANBAN_RUN_ID; refusing unsafe run.", file=sys.stderr)
        return 1
    try:
        run_id = int(raw_run_id)
    except ValueError:
        print(f"kanban_direct_herdr_worker: malformed HERMES_KANBAN_RUN_ID={raw_run_id!r}.", file=sys.stderr)
        return 1

    success, block_kind = False, "transient"
    agent_name: Optional[str] = None
    machine_id: Optional[str] = None
    try:
        success, report, block_kind, agent_name, machine_id = dispatch(
            task_id, board, workspace_dir, model, effort, herdr_workspace=herdr_workspace, kind=kind,
            run_id=run_id,
        )
    except Exception as exc:  # health: allow BLE001 -- this is the process's top-level boundary;
        # a worker must exit cleanly with a reported reason, never crash out with a traceback.
        report = f"direct-herdr bridge failed: {type(exc).__name__}: {exc}"
    print(report)  # the raw capture always reaches the worker log, whatever the card summary becomes
    try:
        card_ok, reason = _mark_card(
            task_id, board, run_id, success, report, block_kind, workspace_dir=workspace_dir,
            settlement_owner=True, agent_name=agent_name, machine_id=machine_id,
        )
    except Exception as exc:  # health: allow BLE001 -- the pane/agent work already happened; a
        # failure to record it must still exit non-zero so the dispatcher's failure accounting sees it.
        print(f"kanban_direct_herdr_worker: could not record result receipt: {exc}", file=sys.stderr)
        return 1
    if not card_ok:
        print(f"kanban_direct_herdr_worker: result receipt rejected: {reason}", file=sys.stderr)
        return 1
    if reason and reason.startswith(_HANDOFF_REJECTED):
        return 1
    return 0 if success else 1

if __name__ == "__main__":
    # Same PM-install bootstrap precedent as kanban_worker_client.py: a bare `sys.executable -m`
    # child's interpreter carries no dependencies until hermes_bootstrap selects the committed
    # environment, so it must be the very first import.
    try:
        import hermes_bootstrap
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_bootstrap":
            raise
    sys.exit(main())
