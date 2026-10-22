"""Tests for per-board passive Kanban notification routing."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn


def _initialize_test_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()


@pytest.mark.asyncio
async def test_board_destination_overrides_origin_and_uses_notify_only(tmp_path, monkeypatch):
    _initialize_test_home(monkeypatch, tmp_path)
    runner = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
    runner._reply_metadata = lambda event: None
    runner._active_profile_name = lambda: "default"
    runner.config = SimpleNamespace(extra={"kanban": {"notification_channels": {
        "connect": {"platform": "discord", "chat_id": "board-channel", "guild_id": "guild"},
    }}})
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="origin-dm", chat_type="dm", user_id="user",
        profile="default", scope_id="guild",
    )
    kb.create_board("connect")
    with kbc.connect(board="connect") as conn:
        task_id = kb.create_task(conn, title="connect work")

    assert await runner._kanban_auto_subscribe(
        MessageEvent(text="/kanban create", source=source), task_id, "connect")

    with kbc.connect(board="connect") as conn:
        subs = kbn.list_notify_subs(conn, task_id)
    assert len(subs) == 1
    assert subs[0]["platform"] == "discord"
    assert subs[0]["chat_id"] == "board-channel"
    assert subs[0]["chat_type"] == "channel"
    assert subs[0]["notifier_profile"] == "default"
    assert subs[0]["delivery_mode"] == "notify"
    assert subs[0]["delivery_metadata"]["guild_id"] == "guild"


@pytest.mark.asyncio
async def test_unmapped_board_keeps_origin_notify_and_wake_behavior(tmp_path, monkeypatch):
    _initialize_test_home(monkeypatch, tmp_path)
    runner = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
    runner._reply_metadata = lambda event: None
    runner._active_profile_name = lambda: "default"
    runner.config = SimpleNamespace(extra={"kanban": {"notification_channels": {}}})
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="origin-dm", chat_type="dm", user_id="user", profile="default",
    )
    kb.create_board("other")
    with kbc.connect(board="other") as conn:
        task_id = kb.create_task(conn, title="other work")

    assert await runner._kanban_auto_subscribe(
        MessageEvent(text="/kanban create", source=source), task_id, "other")

    with kbc.connect(board="other") as conn:
        subs = kbn.list_notify_subs(conn, task_id)
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "origin-dm"
    assert subs[0]["delivery_mode"] == "notify+wake"
