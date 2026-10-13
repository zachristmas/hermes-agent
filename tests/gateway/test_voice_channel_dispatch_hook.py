"""``_handle_voice_channel_input`` hands the dispatched event to a per-call hook.

A caller that injects spoken input (the Discord realtime delegate) needs the exact ``MessageEvent``
its request became, before dispatch, so it can tell that event's lifecycle hooks apart from every
other voice input. The hook and ``resume_chat_session`` are keyword arguments of that one call;
nothing about the shared voice-input callback is rebound.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.discord.adapter import DiscordAdapter

_GUILD, _TEXT, _USER = 1, 700, 42


@pytest.fixture(autouse=True)
def _only_the_speaker_is_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DISCORD_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS",
                "DISCORD_ALLOWED_ROLES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", str(_USER))


def _runner_and_adapter():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._allowed_user_ids = {str(_USER)}
    guild = SimpleNamespace(id=_GUILD, name="Hermes Server", get_member=lambda _id: None)
    adapter._client = SimpleNamespace(get_guild=lambda _id: guild, get_channel=lambda _id: None)
    adapter._voice_text_channels = {_GUILD: _TEXT}
    joined = SessionSource(platform=Platform.DISCORD, chat_id=str(_TEXT), chat_type="group",
                           user_id=str(_USER), user_name="speaker", guild_id=str(_GUILD))
    adapter._voice_sources = {_GUILD: joined.to_dict()}
    adapter.handle_message = AsyncMock()
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._voice_mode, runner._session_db, runner.session_store = {}, None, MagicMock()
    runner._session_key_for_source = lambda source: "discord:group:700:42"
    runner.session_store.lookup_by_session_key.return_value = SimpleNamespace(session_id="last-chat-session")
    runner._delivery_adapter_for = lambda source: adapter
    return runner, adapter


@pytest.mark.asyncio
async def test_the_hook_receives_each_dispatched_event_before_it_is_handed_to_the_adapter() -> None:
    runner, adapter = _runner_and_adapter()
    order, captured = [], []
    adapter.handle_message = AsyncMock(side_effect=lambda event: order.append("dispatched"))

    def hook(event):
        captured.append(event)
        order.append("hook")

    await runner._handle_voice_channel_input(_GUILD, _USER, "first", adapter=adapter, on_event_created=hook)
    await runner._handle_voice_channel_input(_GUILD, _USER, "second", adapter=adapter, on_event_created=hook)

    dispatched = [call.args[0] for call in adapter.handle_message.await_args_list]
    assert captured == dispatched and [e.text for e in dispatched] == ["first", "second"]
    assert order == ["hook", "dispatched", "hook", "dispatched"]


@pytest.mark.asyncio
async def test_a_hook_that_changes_the_event_changes_what_is_dispatched() -> None:
    runner, adapter = _runner_and_adapter()

    await runner._handle_voice_channel_input(
        _GUILD, _USER, "hello", adapter=adapter,
        on_event_created=lambda event: setattr(event, "allow_gateway_control", False))

    assert adapter.handle_message.await_args.args[0].allow_gateway_control is False


@pytest.mark.asyncio
async def test_resume_chat_session_pins_the_event_to_the_latest_chat_conversation() -> None:
    runner, adapter = _runner_and_adapter()

    await runner._handle_voice_channel_input(_GUILD, _USER, "hello", adapter=adapter, resume_chat_session=True)

    event = adapter.handle_message.await_args.args[0]
    assert event.metadata["gateway_session_key"] == "discord:group:700:42"
    assert event.metadata["gateway_session_id"] == "last-chat-session"
    assert event.metadata["gateway_session_strict"] is True


@pytest.mark.asyncio
async def test_a_failed_session_lookup_still_dispatches_the_event_unpinned() -> None:
    runner, adapter = _runner_and_adapter()
    runner.session_store.lookup_by_session_key.side_effect = RuntimeError("db locked")

    await runner._handle_voice_channel_input(_GUILD, _USER, "hello", adapter=adapter, resume_chat_session=True)

    assert adapter.handle_message.await_count == 1
    assert "gateway_session_strict" not in adapter.handle_message.await_args.args[0].metadata


@pytest.mark.asyncio
async def test_without_the_arguments_the_event_is_dispatched_normally() -> None:
    runner, adapter = _runner_and_adapter()

    await runner._handle_voice_channel_input(_GUILD, _USER, "hello", adapter=adapter)

    event = adapter.handle_message.await_args.args[0]
    assert event.text == "hello" and event.allow_gateway_control is True
    assert "gateway_session_strict" not in event.metadata
    runner.session_store.lookup_by_session_key.assert_not_called()


@pytest.mark.asyncio
async def test_bound_voice_callback_forwards_the_per_call_arguments() -> None:
    runner, adapter = _runner_and_adapter()
    captured = []
    runner._bind_voice_input_callback(adapter)

    await adapter._voice_input_callback(guild_id=_GUILD, user_id=_USER, transcript="delegated",
                                         resume_chat_session=True, on_event_created=captured.append)

    assert len(captured) == 1
    assert adapter.handle_message.await_args.args[0] is captured[0]
    assert captured[0].metadata["gateway_session_strict"] is True
