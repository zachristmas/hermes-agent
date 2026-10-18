"""Per-task host pin (``remote_host``) and agent-kind override (``agent_kind_override``), split
out of ``kanban_db.py`` to keep that facade under its line cap — behaviour, not a re-export shim.

``remote_host`` is read by the direct-Herdr bridge's dispatch gate
(``kanban_db_dispatch_herdr._board_uses_direct_herdr_dispatch``) to skip its fast local-spawn path
for a task that belongs to a specific other machine, e.g. a card continuing a pre-existing
cross-host Herdr session (the GSD incident, card t_a87265e1).

``agent_kind_override`` lets a specific card pin its own Herdr agent kind (``codex``, ``pi``,
``opencode``, ...), independent of ``kanban.herdr_board_agents``' per-board default -- the board
config is only ever a default, never the only way to choose a harness for one card (the baa-ton
per-work-type profile pattern: the orchestrator or a human owns this choice per task, not just per
board).
"""
from __future__ import annotations

import os
import socket
import sqlite3
from typing import Optional


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def _host_prefix() -> str:
    """``"<host>:"`` prefix shared by every claim lock issued from this host."""
    return f"{_claimer_id().split(':', 1)[0]}:"


def set_remote_host(conn: sqlite3.Connection, task_id: str, host: Optional[str]) -> bool:
    """Pin (or clear, on empty/None) the hostname this task's work must run on."""
    from hermes_cli import kanban_db as _kb

    host = (host or "").strip() or None
    return _kb._set_task_override(
        conn, task_id,
        "UPDATE tasks SET remote_host = ? WHERE id = ?", (host,),
        "remote_host_set", {"remote_host": host},
        ("remote_host",), archived_msg="cannot set remote host",
    )


def set_agent_kind_override(conn: sqlite3.Connection, task_id: str, kind: Optional[str]) -> bool:
    """Pin (or clear, on empty/None) this task's own Herdr agent kind, overriding the board's
    ``kanban.herdr_board_agents`` default. No fixed kind list is enforced here -- any installed
    ``herdr agent start --kind`` value is valid, and guessing at a closed set would reject a real
    one the next Herdr release adds."""
    from hermes_cli import kanban_db as _kb

    kind = (kind or "").strip() or None
    return _kb._set_task_override(
        conn, task_id,
        "UPDATE tasks SET agent_kind_override = ? WHERE id = ?", (kind,),
        "agent_kind_override_set", {"agent_kind_override": kind},
        ("agent_kind_override",), archived_msg="cannot set agent kind override",
    )
