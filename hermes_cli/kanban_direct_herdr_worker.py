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
from typing import Any, Optional

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DEFAULT_MODEL = "claude-sonnet-5-5"
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

_SETTLED_STATUSES = ("idle", "done", "finished")


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


def herdr_command() -> list[str]:
    """Resolve the ``herdr`` executable: ``$HERDR_BIN``, then PATH, then the standalone-release
    glob — the same resolution order ``plugins/kanban-herdr/herdr_tool.py`` uses, reimplemented
    here so this bridge has no import dependency on that separately-versioned plugin repo.

    Pins ``--session`` (default ``default``, override via ``HERMES_KANBAN_HERDR_SESSION``) on
    every call, per the herdr-dispatch skill's own rule for local work: without it, commands fall
    back to whatever session context the calling process happened to inherit rather than the
    recorded local session.
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


def _run_herdr(args: list[str], *, timeout: float = _CLI_TIMEOUT_S) -> Any:
    """Run one ``herdr`` subcommand and return its parsed ``.result`` (unwrapped).

    Raises :class:`HerdrCliError` for a structured server error (exit 1 with a JSON error body —
    callers can branch on ``.code``), plain :class:`BridgeError` for a CLI syntax error (exit 2 —
    a bridge-side bug, not a business failure), any other non-zero exit, a timeout, or a
    missing/unreadable binary. Never hangs past ``timeout`` and never lets a subprocess failure
    escape as an uncaught exception.
    """
    cmd = herdr_command() + args
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


def _pane_tokens(pane_id: str) -> dict:
    """Read-only lookup of a pane's current tags -- unlike :func:`link_pane`, never writes
    anything, so it can be used to check a pane's existing ownership before touching it."""
    result = _run_herdr(["pane", "get", pane_id])
    pane = result.get("pane") or result if isinstance(result, dict) else {}
    return pane.get("tokens") or {}


def _agent_info(name: str) -> Optional[dict]:
    """The raw ``agent get <name>`` response's ``agent`` dict, or ``None`` if the agent truly
    doesn't exist (``agent_not_found``). Lower-level than :func:`agent_status`: a caller that needs
    to tell "doesn't exist" apart from "exists but reports an empty/unrecognized status" -- which
    collapsing straight to a status string can't do -- uses this instead.
    """
    try:
        result = _run_herdr(["agent", "get", name])
    except HerdrCliError as exc:
        if exc.code == "agent_not_found":
            return None
        raise
    agent = result.get("agent") or result if isinstance(result, dict) else {}
    return agent if isinstance(agent, dict) else {}


def agent_exists(name: str) -> bool:
    """Whether Herdr knows an agent by this name at all, regardless of its status field's value."""
    return _agent_info(name) is not None


def agent_status(name: str) -> str:
    """The agent's live status, or ``""`` if no agent by that name exists (or one exists but
    doesn't report a status -- see :func:`agent_exists` when that distinction matters).

    Verified live (herdr 0.9.3): ``agent get`` on a name with no agent returns the structured
    error ``agent_not_found``, not an empty/absent result.
    """
    agent = _agent_info(name)
    # Verified live (herdr 0.9.3): the field is `agent_status`, not `status`.
    return str((agent or {}).get("agent_status") or "").lower()


def find_existing_agent(name: str) -> Optional[dict]:
    """The live ``agent list`` entry named ``name``, or ``None``.

    Goes through ``agent list`` rather than ``agent get <name>`` + :func:`agent_status`: a crashed
    attempt's existing agent is one entry among however many others are live, and ``agent list``'s
    entry shape (including ``pane_id`` alongside ``agent_status``) was captured directly from a
    real run and is already trusted.
    """
    result = _run_herdr(["agent", "list"])
    agents = result.get("agents") if isinstance(result, dict) else None
    for agent in agents or ():
        if isinstance(agent, dict) and agent.get("name") == name:
            return agent
    return None


def _settle_and_close(name: str, pane_id: Optional[str]) -> tuple[bool, str, bool]:
    """Wait out the agent, then close its pane only if that's actually safe.

    Returns ``(success, report, pane_preserved)``. Re-checks the agent's live status right before
    closing (not just the status that got the caller here) per the herdr-dispatch skill's cleanup
    rule: a blocked or still-working agent's pane is never closed, so a human can still reach it.
    Only an agent confirmed genuinely settled (idle/done/finished) by BOTH checks is closed.
    """
    status = wait_agent(name, _AGENT_WAIT_TIMEOUT_S * 1000)
    if status == "blocked":
        return False, f"agent {name} is blocked on an approval/question dialog: {read_agent(name)}", True
    if status not in _SETTLED_STATUSES:
        return False, f"agent {name} did not reach idle/done before the wait timeout (status={status!r})", True
    # Re-verify immediately before closing: `wait_agent` can itself time out and return a stale
    # settled-looking read, or the agent can flip to blocked/working/some other status between the
    # wait returning and this check. Require the live status to still be positively settled rather
    # than excluding a few known-unsafe ones -- an empty, "unknown", or any future status this file
    # doesn't recognize must not fall through to "safe to close" by default.
    live_status = agent_status(name)
    if live_status not in _SETTLED_STATUSES:
        return False, (
            f"agent {name} reported {status!r} on wait but is now {live_status!r}, not confirmed "
            f"settled; not closing its pane: {read_agent(name)}"
        ), True
    # Read the report BEFORE closing: closing a pane tears down the agent living in it (verified
    # live -- a `read` issued after `close_pane` gets `agent_not_found`, not the transcript).
    report = read_agent(name)
    # `read_agent` is itself a real call that can take time -- recheck once more immediately
    # before closing so an agent that resumed DURING the read doesn't still get its pane closed
    # out from under it. The work already succeeded (the report above is good); a resume here only
    # changes whether it's safe to close, not whether this run counts as a success.
    final_status = agent_status(name)
    if final_status not in _SETTLED_STATUSES:
        return True, report, True
    closed = True
    if pane_id is not None:
        closed = close_pane(pane_id)
    return True, report, not closed


def start_agent(name: str, pane_id: str, model: str, effort: str, kind: str = _DEFAULT_KIND) -> None:
    """``herdr agent start <name> --kind <kind> --pane <pane> -- --model <model> --effort
    <effort>`` — the interactive coding-agent path, never a raw provider API. ``kind`` is whatever
    the dispatcher resolved from ``kanban.herdr_board_agents`` for this board (``run()``'s own
    caller); it is a parameter rather than hardcoded so this one function serves every installed
    ``herdr agent`` kind, not just Claude/Codex.

    ``agent_not_ready`` is not treated as fatal on its own: Herdr returns it when it could not
    confirm input-readiness in time, but the agent may still be present and about to become
    ready — ``agent get`` decides, not the start call's own exit status. Existence alone isn't
    enough, though: a same-named agent reported in a *different* pane than the one just created
    here is not this card's agent (this bridge's caller tracks state by ``pane_id``, not just by
    name, so prompting/closing would then act on the wrong pane while leaving the real one
    orphaned) and is refused rather than silently adopted.
    """
    try:
        _run_herdr([
            "agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "60000",
            "--", "--model", model, "--effort", effort,
        ], timeout=_AGENT_START_TIMEOUT_S)
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


def prompt_agent(name: str, text: str) -> None:
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
        _run_herdr(["agent", "prompt", name, text, "--wait", "--timeout", "30000"], timeout=_AGENT_PROMPT_TIMEOUT_S)
    except HerdrCliError as exc:
        if exc.code == "agent_blocked":
            raise AgentBlockedError(f"agent {name} is already blocked on a dialog: {exc}") from exc
        if exc.code in ("agent_prompt_stalled", "timeout"):
            status = agent_status(name)
            if status in ("blocked", "busy", "working", "running"):
                return  # delivered -- this call's job (handing the prompt off) is done
            raise BridgeError(f"agent {name} prompt could not be confirmed delivered ({exc.code})") from exc
        raise


def wait_agent(name: str, timeout_ms: int) -> str:
    """``herdr agent wait <name> --timeout <ms>`` with no ``--until``: settles on whichever of
    idle/done/blocked the agent reaches first. Returns the settled status."""
    result = _run_herdr(["agent", "wait", name, "--timeout", str(timeout_ms)], timeout=(timeout_ms / 1000.0) + 30)
    agent = result.get("agent") or result if isinstance(result, dict) else {}
    # Verified live (herdr 0.9.3): the field is `agent_status`, not `status`.
    return str(agent.get("agent_status") or "").lower()


def read_agent(name: str) -> str:
    """``herdr agent read <name> --source recent-unwrapped --lines 120`` -- the read-only
    transcript inspection call, soft-wrapped lines joined."""
    result = _run_herdr(["agent", "read", name, "--source", "recent-unwrapped", "--lines", "120"])
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


def _compose_prompt(task_id: str, board: Optional[str]) -> str:
    """``"work kanban task <id>"`` enriched with the card's own title/description when the DB is
    reachable, so the agent's first move isn't a blind re-fetch of the card it was already handed.
    Best-effort: any read failure falls back to the bare instruction rather than blocking dispatch
    on a problem the agent itself can also just fix by reading the card.
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
        return base
    if task is None:
        return base
    lines = [base, "", f"Title: {task.title}"]
    if task.body:
        lines.append("")
        lines.append(task.body.strip())
    return "\n".join(lines)


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
        herdr_workspace: Optional[str] = None, kind: str = _DEFAULT_KIND) -> tuple[bool, str]:
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
                    prompt_agent(name, _compose_prompt(task_id, board))
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
            prompt_agent(name, _compose_prompt(task_id, board))
        except AgentBlockedError as exc:
            return False, (
                f"agent {name} is blocked on an approval/question dialog and was not "
                f"auto-answered: {exc}"
            )
        success, report, pane_preserved = _settle_and_close(name, pane_id)
        return success, _annotate_preserved_pane(success, report, pane_preserved)
    except BridgeError as exc:
        return False, str(exc)


def _mark_card(
    task_id: str, board: Optional[str], run_id: Optional[int], success: bool, report: str,
) -> tuple[bool, Optional[str]]:
    """Record the bridge's result on the card itself, with the same run-ownership proof
    (``expected_run_id``) a dispatcher-spawned chat worker's own ``kanban_complete``/
    ``kanban_block`` tool call would carry.

    A settled Herdr agent status is a lifecycle signal, not proof the work was actually done
    correctly (the herdr-dispatch skill is explicit: finish agent work in Review, never Done), so
    success routes to ``request_review`` for a human to judge, not straight to ``done``.

    Returns ``(ok, reason)``: a card transition can be refused (stale ``expected_run_id``, a
    status the transition doesn't apply from, ...) without raising, and a caller that only checked
    for an exception would wrongly treat a no-op as a recorded result.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        if success:
            ok, reason = kb.request_review(
                conn, task_id,
                summary=report.strip() or "Direct Herdr dispatch completed with no report text.",
                expected_run_id=run_id, with_reason=True,
            )
        else:
            ok = kb.block_task(
                conn, task_id,
                reason=report.strip() or "Direct Herdr dispatch failed with no further detail.",
                kind="transient", expected_run_id=run_id,
            )
            reason = None
        return ok, reason
    finally:
        conn.close()


def main() -> int:
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    board = os.environ.get("HERMES_KANBAN_BOARD")
    workspace_dir = os.environ.get("HERMES_KANBAN_WORKSPACE")
    herdr_workspace = os.environ.get("HERMES_KANBAN_HERDR_WORKSPACE") or None  # health: allow HX002 -- dispatcher->worker handoff, same convention as TASK/WORKSPACE/BOARD
    if not task_id or not board or not workspace_dir:
        print(
            "kanban_direct_herdr_worker: missing HERMES_KANBAN_TASK/BOARD/WORKSPACE; "
            "this module must be spawned by the kanban dispatcher, not run standalone.",
            file=sys.stderr,
        )
        return 1

    model = os.environ.get("HERMES_KANBAN_HERDR_MODEL", _DEFAULT_MODEL)  # health: allow HX002 -- dispatcher->worker handoff, same convention as TASK/WORKSPACE/BOARD
    effort = os.environ.get("HERMES_KANBAN_HERDR_EFFORT", _DEFAULT_EFFORT)  # health: allow HX002 -- same handoff convention
    kind = os.environ.get("HERMES_KANBAN_HERDR_KIND", _DEFAULT_KIND)  # health: allow HX002 -- same handoff convention, resolved from kanban.herdr_board_agents by the dispatcher

    # The dispatcher always sets this (kanban_db_dispatch.py's spawn path), same as the legacy
    # kanban_worker_client.py worker; a missing or malformed value silently turned into None here
    # would drop the run-ownership fence that keeps a stale/duplicate run from overwriting a card a
    # newer run already owns, so it is a hard failure, not a quiet fallback.
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    if raw_run_id is None:
        print(
            "kanban_direct_herdr_worker: missing HERMES_KANBAN_RUN_ID; refusing to run without a "
            "verifiable run id.",
            file=sys.stderr,
        )
        return 1
    try:
        run_id = int(raw_run_id)
    except ValueError:
        print(
            f"kanban_direct_herdr_worker: malformed HERMES_KANBAN_RUN_ID={raw_run_id!r}; refusing "
            "to run without a verifiable run id.",
            file=sys.stderr,
        )
        return 1

    try:
        success, report = run(task_id, board, workspace_dir, model, effort, herdr_workspace=herdr_workspace, kind=kind)
    except Exception as exc:  # health: allow BLE001 -- this is the process's top-level boundary;
        # a worker must exit cleanly with a reported reason, never hang or crash out with a
        # traceback the dispatcher's crash accounting can't read as a business failure.
        success, report = False, f"direct-herdr bridge crashed: {exc}"

    print(report)
    try:
        card_ok, card_reason = _mark_card(task_id, board, run_id, success, report)
    except Exception as exc:  # health: allow BLE001 -- the pane/agent work already happened; a
        # failure to record it must still exit non-zero (so the dispatcher's crash/failure
        # accounting sees it), not raise past this process's own top-level error handling.
        print(f"kanban_direct_herdr_worker: could not record the card result: {exc}", file=sys.stderr)
        return 1
    if not card_ok:
        suffix = f" ({card_reason})" if card_reason else ""
        print(
            f"kanban_direct_herdr_worker: card transition was rejected{suffix}; "
            f"run_id={run_id!r} may no longer own this card's claim.",
            file=sys.stderr,
        )
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
