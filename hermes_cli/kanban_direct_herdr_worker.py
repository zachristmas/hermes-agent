"""Direct Herdr dispatch bridge — titan-only opt-in (card t_cb0795f9).

For boards listed in ``kanban.direct_herdr_dispatch_boards``, ``_default_spawn`` in
``kanban_db_dispatch.py`` spawns THIS module instead of a full interactive
``hermes -p <profile> chat -q`` worker. Rather than running its own Hermes agent turn, this
small synchronous process plays the ``skills/devops/herdr-dispatch`` SKILL.md playbook itself:
ensure the board's Herdr workspace exists, create a pane, tag it with the card (the same tokens
the ``kanban_herdr_link`` tool and the ``kanban-herdr`` dashboard plugin use), start a Claude
Code coding agent in it through the interactive ``herdr agent start --kind claude`` path (never
the Anthropic API — this is still billed through the interactive Claude subscription, exactly as
today), prompt it, wait for it, read its report, and clean up. It then marks the kanban card
itself (``complete_task`` / ``block_task``) and exits with a real success/failure code.

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
    here so this bridge has no import dependency on that separately-versioned plugin repo."""
    env = os.environ.get("HERDR_BIN")
    if env:
        return shlex.split(env, posix=(os.name != "nt"))
    found = shutil.which("herdr")
    if not found:
        pattern = os.path.join(
            os.path.expanduser("~"), ".herdr", "packages", "standalone", "releases", "*", "herdr*"
        )
        hits = sorted(h for h in glob.glob(pattern) if os.path.basename(h).lower() in ("herdr", "herdr.exe"))
        found = hits[-1] if hits else None
    if not found:
        raise BridgeError("the herdr command isn't installed or isn't on PATH (set HERDR_BIN)")
    return [found]


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
    if tokens.get("kanban_task") != task_id:
        raise BridgeError(f"pane {pane_id} tag did not verify after linking: {tokens!r}")


def agent_status(name: str) -> str:
    result = _run_herdr(["agent", "get", name])
    agent = result.get("agent") or result if isinstance(result, dict) else {}
    # Verified live (herdr 0.9.3): the field is `agent_status`, not `status`.
    return str(agent.get("agent_status") or "").lower()


def start_agent(name: str, pane_id: str, model: str, effort: str) -> None:
    """``herdr agent start <name> --kind claude --pane <pane> -- --model <model> --effort
    <effort>`` — the interactive Claude Code path, never the Anthropic API.

    ``agent_not_ready`` is not treated as fatal on its own: Herdr returns it when it could not
    confirm input-readiness in time, but the agent may still be present and about to become
    ready — ``agent get`` decides, not the start call's own exit status.
    """
    try:
        _run_herdr([
            "agent", "start", name, "--kind", "claude", "--pane", pane_id, "--timeout", "60000",
            "--", "--model", model, "--effort", effort,
        ], timeout=_AGENT_START_TIMEOUT_S)
    except HerdrCliError as exc:
        if exc.code != "agent_not_ready":
            raise
        if not agent_status(name):
            raise BridgeError(f"agent {name} start reported agent_not_ready and no agent is present") from exc
        # Present, just not confirmed ready yet -- the prompt/wait steps observe its real state.


def prompt_agent(name: str, text: str) -> None:
    """``herdr agent prompt <name> "<text>" --wait --timeout <ms>``.

    ``agent_blocked`` means the agent is already at an approval/question dialog -- surfaced as
    :class:`AgentBlockedError`, never auto-answered. ``agent_prompt_stalled`` / ``timeout`` do not
    prove the prompt wasn't delivered, so both re-check state instead of assuming failure or
    resending (a resend after real delivery would double-prompt the agent).
    """
    try:
        # Verified live on a busy machine (15+ concurrent real agent panes): a cold-started
        # interactive Claude Code session can take noticeably longer than 10s just to produce its
        # first observed activity, well short of being stuck. 30s gives real resource contention
        # room without masking an actually-wedged agent for long.
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


def close_pane(pane_id: str) -> None:
    """Best-effort cleanup: a pane that is already gone is not a bridge failure."""
    try:
        _run_herdr(["pane", "close", pane_id])
    except BridgeError:
        pass


def run(task_id: str, board: str, workspace_dir: str, model: str, effort: str,
        herdr_workspace: Optional[str] = None) -> tuple[bool, str]:
    """Drive one card through the herdr-dispatch skill's workspace -> pane -> link -> agent
    lifecycle.

    Returns ``(success, report_text)`` and never raises: every ``herdr``/agent failure becomes
    ``(False, reason)`` so ``main()`` can mark the card and exit cleanly instead of hanging or
    crashing with a traceback. ``herdr_workspace`` overrides the board-derived workspace
    (``HERMES_KANBAN_HERDR_WORKSPACE`` in ``main()`` — a label or an id, resolved either way);
    otherwise the workspace is the one labelled after the board slug.
    """
    name = pane_name(board, task_id)
    pane_id: Optional[str] = None
    try:
        if herdr_workspace:
            workspace_id, stale_root_pane_id = resolve_workspace_id(herdr_workspace), None
        else:
            workspace_id, stale_root_pane_id = ensure_board_workspace(board)
        pane_id = create_pane(name, workspace_dir, herdr_workspace=workspace_id)
        if stale_root_pane_id:
            # The real pane now exists, so the just-created workspace's throwaway root pane is
            # safe to close without deleting the workspace out from under it (see
            # ensure_board_workspace's docstring for why this can't happen any earlier).
            close_pane(stale_root_pane_id)
        link_pane(pane_id, task_id, board)
        start_agent(name, pane_id, model, effort)
        try:
            prompt_agent(name, f"work kanban task {task_id}")
        except AgentBlockedError as exc:
            return False, (
                f"agent {name} is blocked on an approval/question dialog and was not "
                f"auto-answered: {exc}"
            )
        status = wait_agent(name, _AGENT_WAIT_TIMEOUT_S * 1000)
        if status == "blocked":
            return False, f"agent {name} is blocked on an approval/question dialog: {read_agent(name)}"
        if status not in _SETTLED_STATUSES:
            return False, f"agent {name} did not reach idle/done before the wait timeout (status={status!r})"
        return True, read_agent(name)
    except BridgeError as exc:
        return False, str(exc)
    finally:
        if pane_id is not None:
            close_pane(pane_id)


def _mark_card(task_id: str, board: Optional[str], run_id: Optional[int], success: bool, report: str) -> None:
    """Record the bridge's result on the card itself — the same completion/block path a
    dispatcher-spawned chat worker's own ``kanban_complete``/``kanban_block`` tool call would
    take, with the same run-ownership proof (``expected_run_id``)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        if success:
            kb.complete_task(
                conn, task_id,
                result=report.strip() or "Direct Herdr dispatch completed with no report text.",
                expected_run_id=run_id,
            )
        else:
            kb.block_task(
                conn, task_id,
                reason=report.strip() or "Direct Herdr dispatch failed with no further detail.",
                kind="transient", expected_run_id=run_id,
            )
    finally:
        conn.close()


def main() -> int:
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    board = os.environ.get("HERMES_KANBAN_BOARD")
    workspace_dir = os.environ.get("HERMES_KANBAN_WORKSPACE")
    model = os.environ.get("HERMES_KANBAN_HERDR_MODEL", _DEFAULT_MODEL)
    effort = os.environ.get("HERMES_KANBAN_HERDR_EFFORT", _DEFAULT_EFFORT)
    herdr_workspace = os.environ.get("HERMES_KANBAN_HERDR_WORKSPACE") or None
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    try:
        run_id = int(raw_run_id) if raw_run_id else None
    except ValueError:
        run_id = None

    if not task_id or not board or not workspace_dir:
        print(
            "kanban_direct_herdr_worker: missing HERMES_KANBAN_TASK/BOARD/WORKSPACE; "
            "this module must be spawned by the kanban dispatcher, not run standalone.",
            file=sys.stderr,
        )
        return 1

    try:
        success, report = run(task_id, board, workspace_dir, model, effort, herdr_workspace=herdr_workspace)
    except Exception as exc:  # a worker must exit cleanly, never hang or crash out
        success, report = False, f"direct-herdr bridge crashed: {exc}"

    print(report)
    try:
        _mark_card(task_id, board, run_id, success, report)
    except Exception as exc:  # the pane/agent work already happened; a failure to
        # record it must still exit non-zero (so the dispatcher's crash/failure accounting sees
        # it), not raise past this process's own error handling.
        print(f"kanban_direct_herdr_worker: could not record the card result: {exc}", file=sys.stderr)
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
