"""``hermes herdr`` -- close a Herdr agent's pane and later reopen the SAME session elsewhere.

``hermes herdr close <target>`` records what Herdr itself can no longer tell you once the pane
is gone (the agent's resumable session id, kind, cwd and workspace), then closes it.
``hermes herdr reopen <target-or-session-id>`` resumes that same session into a fresh pane,
optionally on a different machine (``--machine``) or into an already-available pane (``--pane``).
``hermes herdr list`` shows what is recorded. See ``hermes_cli/herdr_pane_lifecycle.py`` for the
mechanics and why this needs Hermes-side state at all.
"""

from __future__ import annotations

import argparse
import json
import sys


def _emit(args, payload: dict, line: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload))
    else:
        print(line)
    return 0


def _fail(exc: Exception) -> int:
    print(f"hermes herdr: {exc}", file=sys.stderr)
    return 1


def _cmd_close(args) -> int:
    from hermes_cli.herdr_pane_lifecycle import PaneLifecycleError, close

    try:
        record = close(args.target, machine_id=args.machine, reason=args.reason or "")
    except PaneLifecycleError as exc:
        return _fail(exc)
    return _emit(
        args, record,
        f"Closed {record['name']!r} ({record['kind']}); recorded session {record['session_id']} "
        f"for later reopen.",
    )


def _cmd_reopen(args) -> int:
    from hermes_cli.herdr_pane_lifecycle import PaneLifecycleError, reopen

    try:
        agent = reopen(
            args.target, pane_id=args.pane, machine_id=args.machine, kind=args.kind,
            workspace=args.workspace, cwd=args.cwd, name=args.name,
        )
    except PaneLifecycleError as exc:
        return _fail(exc)
    return _emit(
        args, agent,
        f"Reopened as {agent.get('name')!r} in pane {agent.get('pane_id')} "
        f"(session {args.target}).",
    )


def _cmd_list(args) -> int:
    from hermes_cli.herdr_session_store import list_records

    records = list_records()
    if getattr(args, "json", False):
        print(json.dumps(records))
        return 0
    if not records:
        print("No closed Herdr sessions recorded.")
        return 0
    for name, record in sorted(records.items()):
        where = record.get("machine_id") or "local"
        closed = "closed" if record.get("closed_pane_id") is None else "open"
        print(
            f"{name}\t{record.get('kind')}\t{record.get('session_id')}\t{where}\t{closed}\t"
            f"{record.get('cwd')}"
        )
    return 0


_ACTIONS = {"close": _cmd_close, "reopen": _cmd_reopen, "list": _cmd_list, None: _cmd_list}


def cmd_herdr(args) -> int:
    action = getattr(args, "herdr_action", None)
    handler = _ACTIONS.get(action)
    if handler is None:
        print("Unknown herdr action. See: hermes herdr --help", file=sys.stderr)
        return 2
    return handler(args)


def build_herdr_pane_parser(subparsers) -> None:
    """Attach the ``herdr`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "herdr", help="Close a Herdr agent's pane and later reopen the same session elsewhere",
        description="Close a Herdr-managed agent's pane while keeping enough of its identity "
            "(agent kind + its own resumable session id) to reopen the SAME conversation into a "
            "fresh pane later, including on a different machine. Closing a pane destroys Herdr's "
            "own record of that agent, so this is Hermes-side state, not something Herdr can be "
            "asked for after the fact -- only sessions closed through 'hermes herdr close' (or "
            "resumed by an already-known session id + --kind) can be reopened this way.",
        epilog=(
            "Examples:\n"
            "  hermes herdr close reviewer\n"
            "  hermes herdr reopen reviewer\n"
            "  hermes herdr reopen reviewer --machine spark --workspace titan\n"
            "  hermes herdr reopen 373452c2-7449-4782-83b9-4edb63f11537 --kind claude --pane w1:p4\n"
            "  hermes herdr list\n"
            "\n"
            "Exit codes: 0 ok, 1 Herdr/record error, 2 usage error."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    herdr_sub = parser.add_subparsers(dest="herdr_action")

    close_p = herdr_sub.add_parser("close", help="Record a live agent's session, then close its pane")
    close_p.add_argument("target", help="Live Herdr agent name or pane id")
    close_p.add_argument("--machine", default=None, help="Saved Herdr machine id/label (default: local)")
    close_p.add_argument("--reason", default="", help="Optional note stored with the record")
    close_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    reopen_p = herdr_sub.add_parser(
        "reopen", help="Resume a recorded (or directly-known) session into a fresh pane")
    reopen_p.add_argument(
        "target", help="A name previously closed with 'hermes herdr close', or a raw session id")
    reopen_p.add_argument("--pane", default=None, help="Reuse this existing pane instead of creating one")
    reopen_p.add_argument("--machine", default=None, help="Saved Herdr machine id/label to reopen on")
    reopen_p.add_argument("--kind", default=None, help="Agent kind (required with a raw session id)")
    reopen_p.add_argument("--workspace", default=None, help="Workspace label/id to create a pane in")
    reopen_p.add_argument("--cwd", default=None, help="Working directory for the new pane")
    reopen_p.add_argument("--name", default=None, help="Name for the reopened agent")
    reopen_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    list_p = herdr_sub.add_parser("list", help="Show recorded closed Herdr sessions")
    list_p.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    parser.set_defaults(func=cmd_herdr, json=False)
