"""set_remote_host: per-task host pin read by the direct-Herdr bridge's dispatch gate
(_board_uses_direct_herdr_dispatch in kanban_db_dispatch_herdr.py) to skip its fast local-spawn
path for a task that belongs to a specific other machine (card t_a87265e1's GSD incident)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


def test_set_remote_host_round_trips(conn):
    task_id = kb.create_task(conn, title="host-pinned card", created_by="test")
    assert kb.set_remote_host(conn, task_id, "DESKTOP-F8K8CPE") is True

    task = kb.get_task(conn, task_id)
    assert task.remote_host == "DESKTOP-F8K8CPE"


def test_set_remote_host_clears_on_none_or_blank(conn):
    task_id = kb.create_task(conn, title="host-pinned card", created_by="test")
    kb.set_remote_host(conn, task_id, "some-host")

    kb.set_remote_host(conn, task_id, "")

    assert kb.get_task(conn, task_id).remote_host is None


def test_set_remote_host_unknown_task_returns_false(conn):
    assert kb.set_remote_host(conn, "t_does_not_exist", "some-host") is False


def test_set_agent_kind_override_round_trips(conn):
    task_id = kb.create_task(conn, title="kind-pinned card", created_by="test")
    assert kb.set_agent_kind_override(conn, task_id, "pi") is True

    assert kb.get_task(conn, task_id).agent_kind_override == "pi"


def test_set_agent_kind_override_clears_on_none_or_blank(conn):
    task_id = kb.create_task(conn, title="kind-pinned card", created_by="test")
    kb.set_agent_kind_override(conn, task_id, "opencode")

    kb.set_agent_kind_override(conn, task_id, "")

    assert kb.get_task(conn, task_id).agent_kind_override is None
