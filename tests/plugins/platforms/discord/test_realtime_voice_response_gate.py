"""Realtime voice never asks for a response while one is in flight or imminent: the provider rejects
that with ``conversation_already_has_active_response``. Covers the three senders (tool-output
acknowledgement, relayed results, and the race with server VAD's own response after speech_stopped).

All fake-driven (see realtime_fakes.py): no network, no credentials, no billing."""

import asyncio
import json

import pytest

from realtime_fakes import ALLOWED, FakeDelegate, make_session, stereo_tone

from plugins.platforms.discord.realtime_voice import (
    ASK_HERMES,
    RESPONSE_HOLD_S,
    VAD_RESPONSE_GRACE_S,
    DelegationResult,
    RealtimeVoiceConfig,
)


async def _speak(session, clock):
    session.feed_frame(ALLOWED, stereo_tone(300), now=clock())
    await session.pump_once(clock.advance(0.06))


def _tool_call(call_id="c1", request="What's the weather?"):
    return {"type": "response.function_call_arguments.done", "name": ASK_HERMES, "call_id": call_id,
            "item_id": "fc" + call_id, "output_index": 0, "response_id": "r0",
            "arguments": json.dumps({"request": request})}


async def _created(session, rid):
    await session.handle_event({"type": "response.created", "response": {"id": rid}})


async def _done(session, rid):
    await session.handle_event({"type": "response.done", "response": {"id": rid}})


async def _settle():
    for _ in range(4):
        await asyncio.sleep(0)


def _creates(tx):
    return len(tx.of_type("response.create"))


def _spoken_notes(tx):
    return [e for e in tx.of_type("conversation.item.create") if e["item"]["type"] == "message"]


@pytest.mark.asyncio
async def test_a_result_finishing_while_the_user_speaks_waits_for_the_vad_response():
    session, tx, _, delegate, clock = make_session()
    await session.start()
    try:
        await _speak(session, clock)
        await session.handle_event(_tool_call())
        await _settle()
        await _created(session, "ack")
        await _done(session, "ack")
        assert _creates(tx) == 1
        await session.handle_event({"type": "input_audio_buffer.speech_started"})
        delegate.finish()
        await _settle()
        await session.handle_event({"type": "input_audio_buffer.speech_stopped"})
        assert _creates(tx) == 1 and not _spoken_notes(tx)       # server VAD is about to respond itself
        await _created(session, "vad")
        assert _creates(tx) == 1
        await _done(session, "vad")
        assert _creates(tx) == 2 and len(_spoken_notes(tx)) == 1   # relayed once that response is done
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_result_is_not_stranded_when_server_vad_never_responds():
    session, tx, _, delegate, clock = make_session()
    await session.start()
    try:
        await _speak(session, clock)
        await session.handle_event(_tool_call())
        await _settle()
        await _created(session, "ack")
        await _done(session, "ack")
        await session.handle_event({"type": "input_audio_buffer.speech_started"})
        delegate.finish()
        await _settle()
        await session.handle_event({"type": "input_audio_buffer.speech_stopped"})   # noise: no response follows
        assert _creates(tx) == 1
        clock.advance(VAD_RESPONSE_GRACE_S + 0.1)
        await asyncio.sleep(0.2)                                                      # the pump loop notices
        assert _creates(tx) == 2 and len(_spoken_notes(tx)) == 1
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_results_finishing_together_or_concurrently_are_voiced_one_response_at_a_time():
    cfg = RealtimeVoiceConfig(enabled=True, delegate_max_inflight=2)
    session, tx, _, delegate, clock = make_session(cfg, delegate=FakeDelegate())
    await session.start()
    try:
        session._pending_results.extend([
            (type("J", (), {"job_id": "h1"})(), DelegationResult("done", text="first")),
            (type("J", (), {"job_id": "h2"})(), DelegationResult("done", text="second")),
        ])
        await asyncio.gather(session._deliver_results(), session._deliver_results())
        assert _creates(tx) == 1 and len(_spoken_notes(tx)) == 1       # the second waits its turn
        await _created(session, "r1")
        await _done(session, "r1")
        assert _creates(tx) == 2 and len(_spoken_notes(tx)) == 2
        texts = [e["item"]["content"][0]["text"] for e in _spoken_notes(tx)]
        assert "first" in texts[0] and "second" in texts[1]
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_tool_call_made_mid_response_is_acknowledged_once_that_response_is_done():
    session, tx, _, delegate, clock = make_session(cfg=RealtimeVoiceConfig(enabled=True, delegate_max_inflight=2))
    await session.start()
    try:
        await _speak(session, clock)
        await _created(session, "r0")                       # the response that is making the calls
        await session.handle_event(_tool_call("a", "first request"))
        await session.handle_event(_tool_call("b", "second request"))
        await _settle()
        assert len(tx.of_type("conversation.item.create")) == 2     # both outputs are already in the conversation
        assert _creates(tx) == 0                                    # but no response is requested mid-response
        await _done(session, "r0")
        assert _creates(tx) == 1                                    # one acknowledgement covers both
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_request_the_provider_never_answers_stops_blocking_after_the_hold():
    session, tx, _, delegate, clock = make_session()
    await session.start()
    try:
        session._pending_results.append((type("J", (), {"job_id": "h1"})(), DelegationResult("done", text="one")))
        await session._deliver_results()
        session._pending_results.append((type("J", (), {"job_id": "h2"})(), DelegationResult("done", text="two")))
        await session._deliver_results()
        assert _creates(tx) == 1                            # no response.created yet: still held
        clock.advance(RESPONSE_HOLD_S + 0.1)
        await session._deliver_results()
        assert _creates(tx) == 2
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_collision_that_still_happens_is_counted():
    session, tx, *_ = make_session()
    await session.start()
    try:
        await session.handle_event({"type": "error", "error": {
            "type": "invalid_request_error", "code": "conversation_already_has_active_response"}})
        assert session.counters["response_collisions"] == 1 and not session.closed
    finally:
        await session.close("test")
