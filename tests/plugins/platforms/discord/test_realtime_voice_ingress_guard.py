"""Only authorized speakers' frames ever enter the bounded ingress queue.

``feed_frame`` runs on the receiver's socket thread and the queue drops its oldest frame when full,
so a frame that is only filtered later, in the pump, can still evict an allowlisted speaker's audio.
The static allowlist, the live gateway gate and the canary control gate all apply before the queue.
"""

import pytest

from realtime_fakes import ALLOWED, BOT, STRANGER, make_session, stereo_tone

from plugins.platforms.discord.realtime_voice import RealtimeUnavailable, RealtimeVoiceConfig, RealtimeVoiceSession


def test_unlisted_or_inexact_speakers_never_enter_the_ingress_queue():
    session, *_ = make_session()
    for who in (STRANGER, str(ALLOWED), float(ALLOWED), b"42", True, BOT, 0):
        session.feed_frame(who, stereo_tone(300), now=1000.0)
    assert len(session._ingress) == 0
    assert session.counters["frames_dropped_unallowed"] == 5    # bot and 0 are not speakers at all


def test_a_flood_from_an_unlisted_speaker_cannot_evict_allowlisted_audio():
    session, *_ = make_session(RealtimeVoiceConfig(enabled=True, ingress_queue_frames=25))
    session.feed_frame(ALLOWED, stereo_tone(300), now=1000.0)
    for i in range(100):
        session.feed_frame(STRANGER, stereo_tone(300), now=1000.0 + i * 0.02)
    assert session.counters["frames_dropped_overflow"] == 0
    assert [user for user, _pcm, _at in session._ingress] == [ALLOWED]


@pytest.mark.asyncio
async def test_a_listed_speaker_the_live_gate_denies_is_rejected_before_the_queue():
    session, tx, _, _, clock = make_session(is_allowed=lambda uid: False)
    await session.start()
    try:
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock.t)
        assert len(session._ingress) == 0
        assert session.counters["frames_dropped_unallowed"] == 1
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_an_allowed_speakers_first_frame_after_start_is_queued():
    session, tx, _, _, clock = make_session(is_allowed=lambda uid: True)
    await session.start()
    try:
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock.t)
        assert [user for user, _pcm, _at in session._ingress] == [ALLOWED]
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_live_gate_revocation_stops_queued_and_new_frames_within_the_refresh_window():
    allowed = [True]
    session, tx, _, _, clock = make_session(is_allowed=lambda uid: allowed[0])
    await session.start()
    try:
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock.t)
        assert len(session._ingress) == 1
        allowed[0] = False
        await session.pump_once(clock.advance(RealtimeVoiceSession.LIVE_AUTH_TTL_S + 0.01))
        assert tx.of_type("input_audio_buffer.append") == []     # the queued frame is re-checked
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock.t)
        assert len(session._ingress) == 0                        # and new frames stop at the door
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_frames_flow_while_the_control_gate_holds():
    session, tx, _, _, clock = make_session(control_authorized=lambda gid: True)
    await session.start()
    try:
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock.t)
        assert [user for user, _pcm, _at in session._ingress] == [ALLOWED]
        await session.pump_once(clock.advance(0.06))
        assert len(tx.appended_pcm()) == 960
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_control_revocation_rejects_frames_before_the_queue_and_closes_the_session():
    allowed = [True]
    session, tx, _, _, clock = make_session(control_authorized=lambda gid: gid == 111 and allowed[0])
    await session.start()
    try:
        allowed[0] = False
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock.t)
        assert len(session._ingress) == 0
        await session._recheck_control_policy()
        assert session.closed and session.closed_reason == "control-revoked"
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_session_whose_control_gate_already_denies_never_connects():
    connected = []
    session, tx, *_ = make_session(control_authorized=lambda gid: False)
    real_connect = session._connect

    async def connect():
        connected.append(True)
        return await real_connect()

    session._connect = connect
    with pytest.raises(RealtimeUnavailable):
        await session.start()
    assert connected == [] and session._transport is None
