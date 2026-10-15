"""Generic multi-valued task references (``task_refs``), split out of ``kanban_db.py`` to keep
that facade under its line cap -- behaviour, not a re-export shim.

A kanban task can span more than one repo/branch (a GSD card already spans backoffice + order
service + payment service in one card) and more than one reference kind (a branch, an ADO work
item, a Connect ticket, a PR URL). A single scalar column (``set-branch``) was rejected as too
narrow for that shape. ``task_refs`` instead holds any number of typed rows per task: ``ref_type``
is free-form (``branch``, ``ado-workitem``, ``connect-ticket``, ``pr-url``, ...), never a closed
enum, so a new reference kind never needs a schema or code change. ``find_task_refs`` is the
reverse lookup (given a repo/branch, which task owns it) that lets an ad-hoc interactive session
(``/open-pr``, ``/wrap``, ``/guided-review``) discover its kanban card instead of asking every time.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

# Moved here from kanban_db.py (import-back via its top-of-file import) to offset this file's own
# growth against kanban_db.py's line cap, same convention as kanban_db_host_pin.py. Generic enough
# that comments/attachments/events keep using them unqualified after the import-back.


def _require_task(conn: sqlite3.Connection, task_id: str) -> None:
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ValueError(f"unknown task {task_id}")


def _task_rows(conn: sqlite3.Connection, table: str, task_id: str, order: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {table} WHERE task_id = ? ORDER BY {order}", (task_id,)
    ).fetchall()


def add_task_ref(
    conn: sqlite3.Connection, task_id: str, ref_type: str, *,
    repo: Optional[str] = None, ref: Optional[str] = None,
    host: Optional[str] = None, note: Optional[str] = None,
) -> int:
    """Attach one typed reference to a task; returns the new ref's id."""
    from hermes_cli import kanban_db as _kb

    ref_type = (ref_type or "").strip()
    if not ref_type:
        raise ValueError("ref type is required")
    repo = (repo or "").strip() or None
    ref = (ref or "").strip() or None
    host = (host or "").strip() or None
    note = (note or "").strip() or None
    now = int(time.time())
    with _kb.write_txn(conn, allow_nested=True):
        _require_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO task_refs (task_id, ref_type, repo, ref, host, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, ref_type, repo, ref, host, note, now),
        )
        _kb._append_event(
            conn, task_id, "ref_added", {"ref_type": ref_type, "repo": repo, "ref": ref},
        )
        return int(cur.lastrowid or 0)


def list_task_refs(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    return [_ref_row_to_dict(r) for r in _task_rows(conn, "task_refs", task_id, "created_at ASC, id ASC")]


def remove_task_ref(conn: sqlite3.Connection, ref_id: int) -> Optional[dict]:
    """Delete one ref by id; returns the removed row (as a dict) or None when no row matched."""
    from hermes_cli import kanban_db as _kb

    with _kb.write_txn(conn, allow_nested=True):
        row = conn.execute("SELECT * FROM task_refs WHERE id = ?", (ref_id,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM task_refs WHERE id = ?", (ref_id,))
        removed = _ref_row_to_dict(row)
        _kb._append_event(
            conn, removed["task_id"], "ref_removed",
            {"ref_type": removed["ref_type"], "repo": removed["repo"], "ref": removed["ref"]},
        )
        return removed


def find_task_refs(
    conn: sqlite3.Connection, *, repo: Optional[str] = None, ref: Optional[str] = None,
) -> list[dict]:
    """Reverse lookup within one board's connection: refs (plus the owning task's title/status)
    matching ``repo`` and/or ``ref``. At least one of the two is required."""
    repo = (repo or "").strip() or None
    ref = (ref or "").strip() or None
    if not repo and not ref:
        raise ValueError("find requires --repo and/or --ref")
    clauses = []
    params: list = []
    if repo:
        clauses.append("tr.repo = ?")
        params.append(repo)
    if ref:
        clauses.append("tr.ref = ?")
        params.append(ref)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT tr.*, t.title AS task_title, t.status AS task_status "
        f"FROM task_refs tr JOIN tasks t ON t.id = tr.task_id "
        f"WHERE {where} ORDER BY tr.created_at DESC",
        tuple(params),
    ).fetchall()
    return [_ref_row_to_dict(r, with_task=True) for r in rows]


def _ref_row_to_dict(row: sqlite3.Row, *, with_task: bool = False) -> dict:
    d = {
        "id": row["id"], "task_id": row["task_id"], "ref_type": row["ref_type"],
        "repo": row["repo"], "ref": row["ref"], "host": row["host"], "note": row["note"],
        "created_at": row["created_at"],
    }
    if with_task:
        d["task_title"] = row["task_title"]
        d["task_status"] = row["task_status"]
    return d
