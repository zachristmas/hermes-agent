"""Direct-Herdr bridge opt-in + per-board agent profile (card t_cb0795f9), split out of
``kanban_db_dispatch.py`` to keep that facade under its line cap — behaviour, not a re-export shim.

``_default_spawn`` calls into this module to decide (a) whether a board uses the direct-Herdr
bridge worker at all (``kanban.direct_herdr_dispatch_boards``) and (b) which ``herdr agent start
--kind``/model/effort that board's bridge should use (``kanban.herdr_board_agents``). Neither
question has a hardcoded board->kind table: an absent or unreadable config always resolves to the
safe default (bridge off; agent profile claude/claude-sonnet-5-5/medium), never a guessed one.
"""
from __future__ import annotations

import socket
import sys
from typing import TYPE_CHECKING
from typing import Mapping
from typing import Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


def _local_hostname() -> str:
    try:
        return (socket.gethostname() or "").strip()
    except Exception:  # health: allow BLE001 -- hostname lookup is best-effort;
        # an empty string below never matches a real pin, so this fails closed.
        return ""


def _handoff_profile_config_path():
    """``config.yaml`` of the profile a dispatcher handed this process (``HERMES_PROFILE``, set
    only on kanban worker children), so a bridge worker reads its board policy from the
    dispatching profile; None means the ambient, profile-aware home."""
    import os

    # health: allow HX002 -- dispatcher->worker handoff, same convention as TASK/WORKSPACE/BOARD
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if not profile:
        return None
    from hermes_cli.profiles import get_profile_dir
    return get_profile_dir(profile) / "config.yaml"


def _load_dispatch_config():
    """Load effective settings from the dispatching profile, not the default profile."""
    from hermes_cli.config_effective import load_user_config_effective

    return load_user_config_effective(config_path=_handoff_profile_config_path(), fail_closed=True)


def _direct_herdr_dispatch_boards() -> frozenset:
    """Boards opted into the direct-Herdr bridge worker (``kanban.direct_herdr_dispatch_boards``,
    card t_cb0795f9).

    The fail direction is the OPPOSITE of ``_dispatch_profile_allowlist``: there, "can't tell"
    must mean "claim nothing" (fail-closed on a claim-widening question). Here, "can't tell" must
    mean "use the existing worker argv" — an unreadable config must never silently switch a board
    onto a path that drives external Herdr panes and coding agents. Absent, empty, or unreadable
    all resolve to the empty set.
    """
    from hermes_cli import kanban_db as _kb
    try:
        kanban = (_load_dispatch_config() or {}).get("kanban", {})
    except Exception as exc:  # health: allow BLE001 -- config read boundary; an unreadable
        # config must stay "off", not propagate and crash a dispatcher tick other boards need.
        _kb._log.warning(
            "kanban: could not read kanban.direct_herdr_dispatch_boards (%s: %s) — "
            "every board keeps the existing worker argv until the config is readable",
            type(exc).__name__, exc,
        )
        return frozenset()
    if not isinstance(kanban, Mapping):
        return frozenset()
    raw = kanban.get("direct_herdr_dispatch_boards")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return frozenset(
        slug for slug in (_kb._normalize_board_slug(n) or n.strip() or None for n in names) if slug
    )


def _board_uses_direct_herdr_dispatch(board: Optional[str], task: Optional[Task] = None) -> bool:
    """True when ``board`` (or the current board, if None) opted into the direct-Herdr bridge,
    AND ``task`` carries no ``remote_host`` pin to a different machine.

    The bridge always spawns a fresh local Herdr pane: it has no notion of an existing
    cross-host session. A card pinned to another host (a continuation of a specific writer/
    reviewer session there, e.g. card t_a87265e1) must fall through to the normal worker/
    orchestrator path, which already knows cross-host handoff, instead of getting a second,
    unrelated fresh agent started on top of it here.
    """
    from hermes_cli import kanban_db as _kb
    slug = _kb._normalize_board_slug(board) or _kb.get_current_board()
    if slug not in _direct_herdr_dispatch_boards():
        return False
    pin = (getattr(task, "remote_host", None) or "").strip()
    if pin and pin.lower() != _local_hostname().lower():
        return False
    return True


_DEFAULT_HERDR_AGENT_PROFILE = {"kind": "claude", "model": "claude-sonnet-5-5", "effort": "medium"}


def _herdr_agent_profile_for_board(board: Optional[str]) -> dict:
    """``{"kind", "model", "effort"}`` for the direct-Herdr bridge's ``herdr agent start``, from
    ``kanban.herdr_board_agents``. There is no hardcoded board->kind table: a board with no
    configured entry, or an unreadable config, falls back to ``_DEFAULT_HERDR_AGENT_PROFILE``
    (claude) — a safe default, never a guessed-codex one, since guessing wrong here would launch
    the wrong interactive subscription's agent.
    """
    from hermes_cli import kanban_db as _kb
    slug = _kb._normalize_board_slug(board) or _kb.get_current_board()
    try:
        kanban = (_load_dispatch_config() or {}).get("kanban", {})
    except Exception as exc:  # health: allow BLE001 -- same config-read boundary as
        # _direct_herdr_dispatch_boards; the safe fallback below is the point of the catch.
        _kb._log.warning(
            "kanban: could not read kanban.herdr_board_agents (%s: %s) — board %r falls back to "
            "the default agent profile (%s)",
            type(exc).__name__, exc, slug, _DEFAULT_HERDR_AGENT_PROFILE["kind"],
        )
        return dict(_DEFAULT_HERDR_AGENT_PROFILE)
    if not isinstance(kanban, Mapping):
        return dict(_DEFAULT_HERDR_AGENT_PROFILE)
    boards = kanban.get("herdr_board_agents")
    entry = boards.get(slug) if isinstance(boards, Mapping) else None
    if not isinstance(entry, Mapping):
        return dict(_DEFAULT_HERDR_AGENT_PROFILE)
    return {
        "kind": str(entry.get("kind") or _DEFAULT_HERDR_AGENT_PROFILE["kind"]),
        "model": str(entry.get("model") or _DEFAULT_HERDR_AGENT_PROFILE["model"]),
        "effort": str(entry.get("effort") or _DEFAULT_HERDR_AGENT_PROFILE["effort"]),
    }


def _review_dispatch_boards() -> frozenset:
    """Boards opted into automatic review-lane dispatch (``kanban.review_dispatch_boards``).

    Enabling automatic review is a claim-widening question (it decides whether a card already
    sitting in Review gets a fresh agent spawned on top of it) exactly like
    ``_direct_herdr_dispatch_boards``, so this fails closed the same way: absent, empty, or
    unreadable config all resolve to the empty set, never "every board reviews automatically".
    A board this function has never heard of stays human-only review, which is the safe
    default — the previous flat ``True`` default caused a real 24-spawn runaway loop on a
    human-only-review board (titan, 2026-10-15): a card already in Review kept getting a fresh
    agent spawned with the same generic implement-lane prompt, which correctly found nothing
    new to do and immediately re-requested review, forever.
    """
    from hermes_cli import kanban_db as _kb
    try:
        kanban = (_load_dispatch_config() or {}).get("kanban", {})
    except Exception as exc:  # health: allow BLE001 -- config read boundary; an unreadable
        # config must stay "off", matching _direct_herdr_dispatch_boards's own fail-closed rule.
        _kb._log.warning(
            "kanban: could not read kanban.review_dispatch_boards (%s: %s) — "
            "every board stays human-only review until the config is readable",
            type(exc).__name__, exc,
        )
        return frozenset()
    if not isinstance(kanban, Mapping):
        return frozenset()
    raw = kanban.get("review_dispatch_boards")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return frozenset(
        slug for slug in (_kb._normalize_board_slug(n) or n.strip() or None for n in names) if slug
    )


def _board_uses_review_dispatch(board: Optional[str]) -> bool:
    """True when ``board`` (or the current board, if None) opted into automatic review-lane
    dispatch (``kanban.review_dispatch_boards``). See :func:`_review_dispatch_boards` for why
    the default is off everywhere until a board explicitly opts in.
    """
    from hermes_cli import kanban_db as _kb
    slug = _kb._normalize_board_slug(board) or _kb.get_current_board()
    return slug in _review_dispatch_boards()


def _open_pr_handoff_boards() -> frozenset:
    """Direct-Herdr boards whose Review handoff must be a human ``/open-pr`` command
    (``kanban.direct_herdr_open_pr_handoff_boards``, e.g. ``[titan]``).

    Opt-in per board, never a default for every board: a board with no PR step keeps the bridge's
    plain behaviour. Absent, empty, or unreadable all resolve to the empty set.
    """
    from hermes_cli import kanban_db as _kb
    try:
        kanban = (_load_dispatch_config() or {}).get("kanban", {})
    except Exception as exc:  # health: allow BLE001 -- config read boundary; an unreadable
        # config keeps the bridge's plain behaviour rather than crashing a worker mid-run.
        _kb._log.warning(
            "kanban: could not read kanban.direct_herdr_open_pr_handoff_boards (%s: %s) — "
            "no board enforces the /open-pr handoff contract until the config is readable",
            type(exc).__name__, exc,
        )
        return frozenset()
    if not isinstance(kanban, Mapping):
        return frozenset()
    raw = kanban.get("direct_herdr_open_pr_handoff_boards")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return frozenset(
        slug for slug in (_kb._normalize_board_slug(n) or n.strip() or None for n in names) if slug
    )


def _board_requires_open_pr_handoff(board: Optional[str]) -> bool:
    """True when ``board`` (or the current board, if None) enforces the /open-pr handoff contract."""
    from hermes_cli import kanban_db as _kb
    slug = _kb._normalize_board_slug(board) or _kb.get_current_board()
    return slug in _open_pr_handoff_boards()


def _direct_herdr_worker_argv() -> list[str]:
    """Build the direct-Herdr bridge worker command (per-board opt-in, card t_cb0795f9).

    This is the board-gated alternative to ``_worker_argv``: instead of the usual worker command,
    it runs a small synchronous bridge (``hermes_cli.kanban_direct_herdr_worker``) that drives a
    Herdr pane + coding agent itself per the herdr-dispatch skill's playbook, then marks the card
    and exits. Every env var ``_default_spawn`` already sets (HERMES_KANBAN_TASK/WORKSPACE/
    BOARD/BRANCH/RUN_ID/CLAIM_LOCK, HERMES_HOME/HERMES_PROFILE, ...) still reaches this child
    unchanged — only this argv differs from ``_worker_argv``'s. ``_propagate_module_import_root``
    pins the package root on PYTHONPATH for any ``-m hermes_cli.<module>`` child, this one
    included.
    """
    from pathlib import Path

    from tools.process_registry import RestartSafeScopeUnavailable

    if not (Path(__file__).resolve().parent / "kanban_direct_herdr_worker.py").is_file():
        # The checkout this dispatcher spawns from no longer carries the bridge (t_6465d3ec run
        # 111: the live tree was switched to a branch without it and the child died on "No module
        # named hermes_cli.kanban_direct_herdr_worker"). A host condition, so the dispatcher's
        # infrastructure path defers the spawn without spending the card's retry budget.
        raise RestartSafeScopeUnavailable(
            "hermes_cli/kanban_direct_herdr_worker.py is missing from the Hermes checkout this "
            "dispatcher spawns from; restore that checkout's branch before dispatching")
    return [sys.executable, "-m", "hermes_cli.kanban_direct_herdr_worker"]


def run_comment_reply_phase(conn, board: Optional[str]) -> list[str]:
    """Start the current assignee's reply run for each direct-Herdr card at rest that has
    unanswered human comments (``kanban_comment_replies``). Runs every spawning tick, after the
    reclaim phase and before the lanes, so a lease taken here also guards this tick's lanes."""
    from hermes_cli import kanban_comment_replies as replies
    from hermes_cli import kanban_db_dispatch as kbd

    def spawn(task, workspace, board_, env):
        return kbd._default_spawn(task, workspace, board=board_, extra_env=env)

    return replies.reconcile_comment_replies(
        conn, board=board, spawn_fn=spawn, eligible=lambda task: _board_uses_direct_herdr_dispatch(board, task))
