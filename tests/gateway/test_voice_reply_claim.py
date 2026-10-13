"""An adapter that claims a turn's voice reply keeps the gateway from speaking it a second time.

Drives the real ``_hmwa_deliver_turn_response``; only the adapter and the TTS delivery are stand-ins.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource

_SOURCE = SessionSource(platform=Platform.DISCORD, chat_id="700", chat_type="group", user_id="42")


class _Entry:
    session_id = "s1"


def _runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner._voice_mode = {"discord:700": "all"}
    runner._adapter_profile_for_source = lambda source: "default"
    runner._delivery_adapter_for = lambda source: adapter
    runner._voice_key_for_source = lambda source: "discord:700"
    runner._send_voice_reply = AsyncMock()
    return runner


def _adapter(claims):
    adapter = MagicMock(spec=BasePlatformAdapter)
    adapter._streaming_tts_turn_completed.return_value = False
    adapter.claim_voice_reply.side_effect = lambda event, text: claims(event, text)
    return adapter


async def _deliver(runner, event, response):
    return await runner._hmwa_deliver_turn_response(
        event, event.source, _Entry(), "agent:main:discord:group:700", None, {}, [], response, None, False)


@pytest.mark.asyncio
async def test_a_claimed_reply_is_not_spoken_by_the_gateway_and_still_returned_as_text() -> None:
    seen = []
    adapter = _adapter(lambda event, text: seen.append((event, text)) or True)
    runner = _runner(adapter)
    event = MessageEvent(source=_SOURCE, text="weather?", message_type=MessageType.VOICE)

    assert await _deliver(runner, event, "Sunny.") == "Sunny."

    assert seen == [(event, "Sunny.")]
    runner._send_voice_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unclaimed_reply_is_spoken_by_the_gateway() -> None:
    runner = _runner(_adapter(lambda event, text: False))
    event = MessageEvent(source=_SOURCE, text="weather?", message_type=MessageType.TEXT)

    await _deliver(runner, event, "Sunny.")

    runner._send_voice_reply.assert_awaited_once_with(event, "Sunny.")


@pytest.mark.asyncio
async def test_an_empty_reply_is_never_offered_for_a_claim() -> None:
    adapter = _adapter(lambda event, text: True)
    runner = _runner(adapter)

    await _deliver(runner, MessageEvent(source=_SOURCE, text="hm", message_type=MessageType.VOICE), "")

    adapter.claim_voice_reply.assert_not_called()


def test_an_adapter_claims_nothing_by_default() -> None:
    event = MessageEvent(source=_SOURCE, text="weather?", message_type=MessageType.VOICE)
    assert BasePlatformAdapter.claim_voice_reply(MagicMock(), event, "Sunny.") is False
