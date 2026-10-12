"""``_handle_voice_channel_input`` hands the dispatched event to a per-call hook.

The realtime voice delegate needs the exact ``MessageEvent`` its spoken request became so it can
tell Hermes's lifecycle hooks apart from every other voice input. The hook is a keyword argument of
that one call; nothing about the shared voice-input callback is rebound.
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
async def test_the_hook_receives_the_dispatched_event_with_a_unique_correlation_key() -> None:
    runner, adapter = _runner_and_adapter()
    captured = []

    await runner._handle_voice_channel_input(_GUILD, _USER, "first", adapter=adapter,
                                             on_realtime_event_created=captured.append)
    await runner._handle_voice_channel_input(_GUILD, _USER, "second", adapter=adapter,
                                             on_realtime_event_created=captured.append)

    dispatched = [call.args[0] for call in adapter.handle_message.await_args_list]
    assert captured == dispatched and len(dispatched) == 2
    keys = [event.metadata["realtime_delegate_event_key"] for event in dispatched]
    assert all(keys) and keys[0] != keys[1]
    assert all(event.allow_gateway_control is False for event in dispatched)
    assert all(event.metadata["gateway_session_key"] == "discord:group:700:42" for event in dispatched)
    assert all(event.metadata["gateway_session_id"] == "last-chat-session" for event in dispatched)
    assert all(event.metadata["gateway_session_strict"] is True for event in dispatched)
    assert runner.session_store.lookup_by_session_key.call_count == 2


@pytest.mark.asyncio
async def test_without_a_hook_the_event_is_still_dispatched_normally() -> None:
    runner, adapter = _runner_and_adapter()

    await runner._handle_voice_channel_input(_GUILD, _USER, "hello", adapter=adapter)

    assert adapter.handle_message.await_count == 1
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "hello"
    assert event.allow_gateway_control is True
    assert "gateway_session_strict" not in event.metadata


@pytest.mark.asyncio
async def test_the_hook_runs_before_the_event_is_handed_to_the_adapter() -> None:
    runner, adapter = _runner_and_adapter()
    order = []
    adapter.handle_message = AsyncMock(side_effect=lambda event: order.append("dispatched"))

    await runner._handle_voice_channel_input(_GUILD, _USER, "hello", adapter=adapter,
                                             on_realtime_event_created=lambda event: order.append("hook"))

    assert order == ["hook", "dispatched"]


@pytest.mark.asyncio
async def test_bound_voice_callback_forwards_the_per_call_realtime_hook() -> None:
    runner, adapter = _runner_and_adapter()
    captured = []
    runner._bind_voice_input_callback(adapter)

    await adapter._voice_input_callback(guild_id=_GUILD, user_id=_USER, transcript="delegated",
                                         on_realtime_event_created=captured.append)

    assert len(captured) == 1
    assert adapter.handle_message.await_args.args[0] is captured[0]
