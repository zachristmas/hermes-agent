"""Shared ``/herdr`` slash-command body for the CLI REPL (``cli_commands_mixin.py``) and the
messaging gateway (``gateway/slash_commands.py``) -- one parse+dispatch+format function so neither
frontend duplicates argument parsing or result formatting; each just forwards its raw argument
string and prints/returns the text back. See ``hermes_cli/herdr_pane_lifecycle.py`` for the
mechanics (why Hermes has to keep its own record at all, what "reopen" actually replays).
"""

from __future__ import annotations

import shlex


def _parse_flag(tokens: list[str], flag: str) -> tuple[list[str], str | None]:
    """Pull ``flag value`` out of *tokens*, returning the remaining tokens and the value (or
    None). Small and local rather than argparse: this only ever sees a handful of optional
    ``--flag value`` pairs from a chat message, not a real CLI invocation."""
    if flag not in tokens:
        return tokens, None
    i = tokens.index(flag)
    value = tokens[i + 1] if i + 1 < len(tokens) else None
    rest = tokens[:i] + tokens[i + 2:]
    return rest, value


def _format_list() -> str:
    from hermes_cli.herdr_session_store import list_records

    records = list_records()
    if not records:
        return "No closed Herdr sessions recorded."
    lines = ["Recorded Herdr sessions:"]
    for name, record in sorted(records.items()):
        where = record.get("machine_id") or "local"
        state = "closed" if record.get("closed_pane_id") is None else "open"
        lines.append(f"- {name} ({record.get('kind')}, {where}, {state}): {record.get('cwd')}")
    return "\n".join(lines)


def handle_cli_command(self, command: str) -> None:
    """``CLICommandsMixin._handle_herdr_command`` body, assigned onto the class in
    ``cli_commands_mixin.py`` rather than defined there (that file's line count is already over
    the health-check cap -- this keeps the new behaviour out of it entirely)."""
    print(f"  {dispatch_herdr_slash(command.partition(' ')[2])}")


def dispatch_herdr_slash(raw_args: str) -> str:
    """Parse ``/herdr <close|reopen|list> ...`` and return the human-readable result text.

    Never raises: every failure (bad usage, a Herdr/record error) comes back as plain text, since
    both callers (CLI ``print()``, gateway chat reply) just surface whatever this returns.
    """
    from hermes_cli.herdr_pane_lifecycle import PaneLifecycleError, close, reopen

    tokens = shlex.split(raw_args or "")
    if not tokens or tokens[0] == "list":
        return _format_list()
    action, rest = tokens[0], tokens[1:]
    if action not in ("close", "reopen"):
        return (
            "Usage: /herdr close <target> | /herdr reopen <target> [--machine ID] "
            "[--pane ID] [--kind KIND] | /herdr list"
        )
    rest, machine = _parse_flag(rest, "--machine")
    rest, pane = _parse_flag(rest, "--pane")
    rest, kind = _parse_flag(rest, "--kind")
    rest, workspace = _parse_flag(rest, "--workspace")
    if not rest:
        return f"Usage: /herdr {action} <target> [--machine ID] ..."
    target = rest[0]
    try:
        if action == "close":
            record = close(target, machine_id=machine)
            return (
                f"Closed {record['name']!r} ({record['kind']}); recorded session "
                f"{record['session_id']} for later reopen."
            )
        agent = reopen(target, pane_id=pane, machine_id=machine, kind=kind, workspace=workspace)
        return f"Reopened as {agent.get('name')!r} in pane {agent.get('pane_id')} (session {target})."
    except PaneLifecycleError as exc:
        return f"/herdr {action}: {exc}"
