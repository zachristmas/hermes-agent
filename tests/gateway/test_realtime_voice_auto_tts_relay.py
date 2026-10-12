"""The base adapter's auto-TTS stays quiet for a turn a live Realtime session claimed, and only that turn.

The real adapter and delegate run here; only the Realtime session object (a ``closed`` flag) is a stand-in,
so this is wiring evidence, not live provider or Discord audio.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType, SessionSource
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.adapter_realtime_voice import GatewayVoiceDelegate
from plugins.platforms.discord.realtime_voice import DelegationRequest

_GUILD, _TEXT, _USER = 1, 700, 42
_KEY = "realtime_delegate_event_key"
_SOURCE = SessionSource(platform=Platform.DISCORD, chat_id=str(_TEXT), chat_type="group",
                        user_id=str(_USER), user_name="speaker", guild_id=str(_GUILD))


def _voice_event(text="what is on my calendar", key=None) -> MessageEvent:
    return MessageEvent(source=_SOURCE, text=text, message_type=MessageType.VOICE,
                        raw_message=SimpleNamespace(guild_id=_GUILD, guild=None),
                        metadata={_KEY: key} if key else {})


@pytest.mark.asyncio
async def test_base_auto_tts_skips_a_claimed_turn_and_keeps_every_other() -> None:
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._should_auto_tts_for_chat = lambda _chat_id: True
    adapter._realtime_sessions[_GUILD] = SimpleNamespace(closed=False)
    dispatched = {}

    async def gateway_entry(guild_id, user_id, transcript, *, on_realtime_event_created=None):
        dispatched["event"] = _voice_event(transcript, key="job-key")
        on_realtime_event_created(dispatched["event"])

    adapter._voice_input_callback = gateway_entry
    task = asyncio.ensure_future(
        GatewayVoiceDelegate(adapter, _GUILD).run(DelegationRequest("h1", _GUILD, _USER, "what is on my calendar")))
    for _ in range(5):
        await asyncio.sleep(0)
    event, interrupt = dispatched["event"], asyncio.Event()

    def wants(turn_event):
        return adapter._wants_auto_tts(turn_event, "agent:main:discord:group:700", interrupt, "Noon.", [])

    assert wants(_voice_event(event.text, key="someone-elses")) is True
    assert wants(_voice_event(event.text)) is True
    assert wants(event) is False
    adapter._realtime_sessions[_GUILD].closed = True
    assert wants(event) is True                                  # session gone: Hermes speaks for itself
    task.cancel()
