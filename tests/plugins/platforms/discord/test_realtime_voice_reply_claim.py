"""A live Realtime session speaks a delegated turn's answer, so Hermes's own voice stays quiet for that
turn and only that turn: base auto-TTS, streaming TTS and the gateway's whole-file reply.

The real adapter and delegate run here; only the Realtime session object (a ``closed`` flag) is a
stand-in, so this is wiring evidence, not live provider or Discord audio.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from discord_voice_fakes import ALLOWED_USER, GUILD, TEXT_CHANNEL

from gateway.platforms.base import AudioFormat
from gateway.platforms.event import MessageEvent, MessageType, SessionSource
from gateway.config import Platform
from plugins.platforms.discord.adapter_realtime_voice import GatewayVoiceDelegate
from plugins.platforms.discord.realtime_voice import DelegationRequest

_KEY = "realtime_delegate_event_key"
_FMT = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
_SOURCE = SessionSource(platform=Platform.DISCORD, chat_id=str(TEXT_CHANNEL), chat_type="group",
                        user_id=str(ALLOWED_USER), user_name="speaker", guild_id=str(GUILD))


def _voice_event(text="what is on my calendar", key=None) -> MessageEvent:
    return MessageEvent(source=_SOURCE, text=text, message_type=MessageType.VOICE,
                        raw_message=SimpleNamespace(guild_id=GUILD, guild=None),
                        metadata={_KEY: key} if key else {})


async def _delegate(adapter):
    """Start a delegated job; returns (task, the event the gateway dispatched for it, hook kwargs seen)."""
    dispatched, seen = {}, {}

    async def gateway_entry(guild_id, user_id, transcript, *, resume_chat_session=False, on_event_created=None):
        seen["resume_chat_session"] = resume_chat_session
        dispatched["event"] = _voice_event(transcript)
        on_event_created(dispatched["event"])

    adapter._voice_input_callback = gateway_entry
    task = asyncio.ensure_future(GatewayVoiceDelegate(adapter, GUILD).run(
        DelegationRequest("h1", GUILD, ALLOWED_USER, "what is on my calendar")))
    for _ in range(5):
        await asyncio.sleep(0)
    return task, dispatched["event"], seen


@pytest.mark.asyncio
async def test_the_delegate_keys_its_event_and_takes_the_gateway_controls_away(make_adapter) -> None:
    adapter, _vc = make_adapter()
    adapter._realtime_sessions[GUILD] = SimpleNamespace(closed=False)

    task, event, seen = await _delegate(adapter)

    assert event.metadata[_KEY] and event.allow_gateway_control is False
    assert seen["resume_chat_session"] is True
    task.cancel()


@pytest.mark.asyncio
async def test_base_auto_tts_skips_a_claimed_turn_and_keeps_every_other(make_adapter) -> None:
    adapter, _vc = make_adapter()
    adapter._should_auto_tts_for_chat = lambda _chat_id: True
    adapter._realtime_sessions[GUILD] = SimpleNamespace(closed=False)
    task, event, _ = await _delegate(adapter)
    interrupt = asyncio.Event()

    def wants(turn_event):
        return adapter._wants_auto_tts(turn_event, "agent:main:discord:group:222", interrupt, "Noon.", [])

    assert wants(_voice_event(event.text, key="someone-elses")) is True
    assert wants(_voice_event(event.text)) is True
    assert wants(event) is False
    adapter._realtime_sessions[GUILD].closed = True
    assert wants(event) is True                                  # session gone: Hermes speaks for itself
    task.cancel()


@pytest.mark.asyncio
async def test_the_claimed_reply_is_kept_as_spoken_text_and_resolves_the_job(make_adapter) -> None:
    adapter, _vc = make_adapter()
    adapter._record_discord_processing_complete = lambda *a, **k: None
    adapter._realtime_sessions[GUILD] = SimpleNamespace(closed=False)
    task, event, _ = await _delegate(adapter)

    assert adapter.claim_voice_reply(event, "You have **one** meeting at noon.") is True
    from gateway.platforms.event import ProcessingOutcome
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    result = await asyncio.wait_for(task, 1)
    assert result.status == "done" and result.delivered_by == "voice"
    assert "**" not in result.text and "one meeting at noon" in result.text


@pytest.mark.asyncio
async def test_an_error_reply_is_claimed_but_never_read_aloud(make_adapter) -> None:
    adapter, _vc = make_adapter()
    adapter._realtime_sessions[GUILD] = SimpleNamespace(closed=False)
    task, event, _ = await _delegate(adapter)

    assert adapter.claim_voice_reply(event, "Error: provider timed out") is True
    assert adapter._realtime_pending_job(event).result_text == ""
    task.cancel()


@pytest.mark.asyncio
async def test_only_the_exact_delegated_event_is_claimed(make_adapter) -> None:
    adapter, _vc = make_adapter()
    adapter._realtime_sessions[GUILD] = SimpleNamespace(closed=False)
    task, event, _ = await _delegate(adapter)

    assert adapter.claim_voice_reply(_voice_event(event.text, key="someone-elses"), "Noon.") is False
    assert adapter.claim_voice_reply(_voice_event(event.text), "Noon.") is False
    adapter._realtime_sessions[GUILD].closed = True
    assert adapter.claim_voice_reply(event, "Noon.") is False     # session gone: the gateway speaks
    task.cancel()


@pytest.mark.asyncio
async def test_streaming_tts_is_declined_only_while_a_delegated_job_is_in_flight(make_adapter) -> None:
    adapter, _vc = make_adapter()
    chat = str(TEXT_CHANNEL)
    assert adapter.supports_streaming_tts(chat, _FMT) is True              # no session at all
    adapter._realtime_sessions[GUILD] = SimpleNamespace(closed=False)
    assert adapter.supports_streaming_tts(chat, _FMT) is True              # live session, nothing delegated

    task, _event, _ = await _delegate(adapter)
    assert adapter.supports_streaming_tts(chat, _FMT) is False             # the session speaks this turn

    adapter._realtime_sessions[GUILD].closed = True
    assert adapter.supports_streaming_tts(chat, _FMT) is True              # session closed: Hermes speaks
    adapter._realtime_sessions[GUILD].closed = False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert adapter.supports_streaming_tts(chat, _FMT) is True              # job finished
