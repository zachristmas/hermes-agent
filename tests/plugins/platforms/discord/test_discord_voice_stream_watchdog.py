"""Producer-starvation watchdog for a streamed voice turn.

A turn whose writer goes quiet without finishing or aborting (a cancelled drain task, a hung
provider, a very long tool call) would otherwise play silence forever: the duck holds, the
inactivity timer stays off and, with barge-in off, the receiver stays paused. The watchdog ends
such a turn after ``producer_timeout_seconds``; backpressure (a full queue), a slow-but-live
producer and a closed, draining stream are not starvation.
"""

import asyncio
import dataclasses
from unittest.mock import MagicMock

import pytest

from discord_voice_fakes import GUILD, TEXT_CHANNEL, FakeVoiceClient

from gateway.platforms.base import AudioFormat

FMT = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
CHAT = str(TEXT_CHANNEL)


def _pcm(ms=100):
    return b"\x01\x00" * (24 * ms)


def _limit(adapter, seconds):
    """Shrink the producer timeout (the config clamps it to >= 1 s) so the tests stay fast."""
    adapter._voice_stream_cfg = dataclasses.replace(adapter._voice_stream_cfg, producer_timeout_s=seconds)


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


class TestProducerStarvation:
    @pytest.mark.asyncio
    async def test_a_silent_producer_ends_the_turn_and_restores_the_session(self, make_adapter):
        adapter, vc = make_adapter({"barge_in": False})
        _limit(adapter, 0.3)
        receiver = MagicMock()
        adapter._voice_receivers[GUILD] = receiver
        adapter._reset_voice_timeout = MagicMock()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _pcm(100))
        # ...and then the writer never writes, finishes or aborts again.
        await _until(lambda: handle.released)
        assert handle.cut_reason == "producer-starved" and handle.aborted
        receiver.resume.assert_called_once()
        adapter._reset_voice_timeout.assert_called_once_with(GUILD)
        assert GUILD not in adapter._voice_streams
        assert vc.wait_idle()  # our source was stopped: nothing is left pulling silence
        await adapter.write_streaming_tts(handle, _pcm(100))  # the writer's late call is a no-op
        await adapter.finish_streaming_tts(handle)
        receiver.resume.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_starved_mixer_turn_releases_the_duck_and_keeps_the_mixer(self, make_adapter):
        pytest.importorskip("numpy")
        from plugins.platforms.discord.adapter import _voice_mixer_module
        adapter, vc = make_adapter()
        _limit(adapter, 0.3)
        mixer = _voice_mixer_module().VoiceMixer()
        vc.play(mixer)
        adapter._voice_mixers[GUILD] = mixer
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await _until(lambda: mixer.speech_active)
        await _until(lambda: handle.released)
        assert handle.cut_reason == "producer-starved"
        await _until(lambda: not mixer.speech_active)
        assert vc.is_playing() and vc.source is mixer

    @pytest.mark.asyncio
    async def test_a_slow_but_live_producer_is_not_cut(self, make_adapter):
        adapter, vc = make_adapter()
        _limit(adapter, 0.3)
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        for _ in range(8):  # gaps of 150 ms, each shorter than the 300 ms limit
            await adapter.write_streaming_tts(handle, _pcm(100))
            await asyncio.sleep(0.15)
        assert handle.cut_reason == "" and not handle.aborted
        await adapter.finish_streaming_tts(handle)
        assert handle.cut_reason == "" and handle.released

    @pytest.mark.asyncio
    async def test_backpressure_is_not_starvation(self, make_adapter):
        """A full queue means the writer is blocked on the player: that is the stall timeout's job."""
        adapter, vc = make_adapter({"buffer_ms": 100})
        _limit(adapter, 0.3)
        adapter._voice_stream_cfg = dataclasses.replace(adapter._voice_stream_cfg, stall_timeout_s=60)
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        vc.stall()  # the player stops pulling: the queue fills and the writer blocks
        writer = asyncio.ensure_future(adapter.write_streaming_tts(handle, _pcm(1000)))
        await asyncio.sleep(0.8)  # well past the producer limit
        assert not writer.done() and handle.cut_reason == ""
        await adapter.abort_streaming_tts(handle)
        await asyncio.wait_for(writer, 3)

    @pytest.mark.asyncio
    async def test_a_closed_stream_still_draining_is_not_cut(self, make_adapter):
        adapter, vc = make_adapter(vc=FakeVoiceClient(frame_interval=0.02))
        _limit(adapter, 0.3)
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _pcm(800))  # 40 frames: ~0.8 s of playback
        await adapter.finish_streaming_tts(handle)  # waits for the drain, past the producer limit
        assert handle.cut_reason == "" and handle.released and not handle.aborted

    @pytest.mark.asyncio
    async def test_releasing_a_turn_stops_its_watchdog(self, make_adapter):
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        watchdog = handle.watchdog
        assert watchdog is not None and not watchdog.done()
        await adapter.write_streaming_tts(handle, _pcm(100))
        await adapter.finish_streaming_tts(handle)
        await asyncio.sleep(0.02)
        assert handle.watchdog is None and watchdog.done()

    @pytest.mark.asyncio
    async def test_leaving_the_channel_stops_the_watchdog(self, make_adapter):
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        watchdog = handle.watchdog
        await adapter.leave_voice_channel(GUILD)
        await asyncio.sleep(0.02)
        assert handle.cut_reason == "left" and watchdog.done()
