"""Persisted record of Herdr agent sessions this install has closed, keyed by the agent's last
live name. This is the state Hermes itself must keep: once a pane is closed, Herdr's own
``agent get``/``agent list`` have nothing left for that agent (verified live, herdr 0.9.3), so a
later ``hermes herdr reopen`` can only work from what was captured before the close happened.

One JSON file under ``get_hermes_home()`` (profile-scoped, like ``spawn-ledger.json``), not a
state.db table: records are few, read/written one at a time from a CLI invocation, and never
queried relationally -- a plain dict keeps this module tiny and dependency-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

FILENAME = "herdr_closed_sessions.json"


def _path() -> Path:
    return get_hermes_home() / FILENAME


def _load() -> dict[str, dict]:
    path = _path()
    if not path.exists():
        return {}
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = data.get("records") if isinstance(data, dict) else None
    return records if isinstance(records, dict) else {}


def save_record(record: dict[str, Any]) -> None:
    """Persist (or overwrite) one record, keyed by its ``name``."""
    name = record.get("name")
    if not name:
        raise ValueError("record has no 'name' to key it by")
    records = _load()
    records[name] = record
    atomic_json_write(_path(), {"records": records})


def get_record(name: str) -> Optional[dict]:
    return _load().get(name)


def list_records() -> dict[str, dict]:
    return _load()


def remove_record(name: str) -> bool:
    records = _load()
    if name not in records:
        return False
    del records[name]
    atomic_json_write(_path(), {"records": records})
    return True
