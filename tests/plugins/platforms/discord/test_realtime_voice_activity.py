"""Realtime voice reports real conversation activity to the adapter, so the Discord voice-channel
inactivity timer does not leave mid-conversation (Realtime bypasses the STT listen loop that used to
re-arm it). All fake-driven (see realtime_fakes.py)."""

import asyncio
import json

import pytest

from realtime_fakes import ALLOWED, delta, make_session, mono24, stereo_tone

from plugins.platforms.discord.realtime_voice import ACTIVITY_NOTIFY_S, ASK_HERMES


def _tool_call(args):
    return {"type": "response.function_call_arguments.done", "name": ASK_HERMES, "call_id": "c1",
            "item_id": "fc1", "output_index": 0, "response_id": "r1", "arguments": json.dumps(args)}


@pytest.mark.asyncio
async def test_speech_and_model_audio_report_activity():
    beats = []
    session, tx, playback, delegate, clock = make_session(on_activity=lambda: beats.append(clock()))
    await session.start()
    try:
        await session.handle_event({"type": "input_audio_buffer.speech_started"})
        assert len(beats) == 1
        clock.advance(ACTIVITY_NOTIFY_S)
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(480)))
        assert len(beats) == 2
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_activity_reports_are_throttled():
    beats = []
    session, tx, playback, delegate, clock = make_session(on_activity=lambda: beats.append(clock()))
    await session.start()
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        for _ in range(50):                       # a response streams a delta every ~20 ms
            await session.handle_event(delta("r1", mono24(480)))
            clock.advance(0.02)
        assert len(beats) == 1
        clock.advance(ACTIVITY_NOTIFY_S)
        await session.handle_event(delta("r1", mono24(480)))
        assert len(beats) == 2
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_silent_open_mic_is_not_activity():
    beats = []
    session, tx, playback, delegate, clock = make_session(on_activity=lambda: beats.append(clock()))
    await session.start()
    try:
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock())
        await session.pump_once(clock.advance(0.06))
        assert tx.appended_pcm() and beats == []   # audio reached the provider; no speech event, no beat
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_finished_delegation_reports_activity_even_when_nobody_spoke():
    beats = []
    session, tx, playback, delegate, clock = make_session(on_activity=lambda: beats.append(clock()))
    await session.start()
    try:
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock())   # focus the speaker so the hand-off starts
        await session.pump_once(clock.advance(0.06))
        await session.handle_event(_tool_call({"request": "What's the weather in Raleigh?"}))
        await asyncio.sleep(0)                    # let the delegate start
        assert len(beats) == 1                    # the hand-off itself
        clock.advance(60.0)
        delegate.finish()
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(beats) == 2                    # the result arriving, before it is spoken
    finally:
        await session.close("test")
