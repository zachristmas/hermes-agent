"""The gateway's real StreamingTTSConsumer driving the real DiscordAdapter streaming contract.

Only the TTS provider (deterministic 24 kHz PCM) and the voice client are fakes. Covers the
consumer outcomes the gateway acts on: completion suppresses the whole-file reply, a barge-in stops
synthesis without replaying the reply, and a decline or a provider failure before any audio leaves
the whole-file fallback available."""

import array
import asyncio
import math
import sys
import threading
import time

import pytest

from discord_voice_fakes import ALLOWED_USER, GUILD, TEXT_CHANNEL, FakeVoiceClient

from gateway.streaming_tts_consumer import StreamingTTSConsumer
from plugins.platforms.discord.voice_stream import SILENCE_FRAME

CHAT = str(TEXT_CHANNEL)


class LevelStreamer:
    """24 kHz mono provider: each clause yields ``chunks`` 20 ms chunks at a constant level,
    paced like a real-time provider when ``pace`` is set."""

    sample_rate, channels, sample_width = 24000, 1, 2

    def __init__(self, *, level=1000, chunks=10, pace=0.0, fail=False):
        self.level, self.chunks, self.pace, self.fail = level, chunks, pace, fail
        self.pulled = 0
        self.first_chunk = threading.Event()

    def stream(self, text):
        if self.fail:
            raise RuntimeError("provider unavailable")
        arr = array.array("h", [self.level] * 480)
        if sys.byteorder == "big":
            arr.byteswap()
        for _ in range(self.chunks):
            self.pulled += 1
            self.first_chunk.set()
            if self.pace:
                time.sleep(self.pace)
            yield arr.tobytes()


def _speech_frame(amplitude=8000):
    arr = array.array("h")
    for i in range(960):
        v = int(amplitude * math.sin(2 * math.pi * 300 * i / 48000))
        arr.extend((v, v))
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def _consumer(monkeypatch, adapter, streamer):
    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda cfg: streamer)
    tts = StreamingTTSConsumer(adapter, CHAT, {}, asyncio.get_running_loop())
    tts.start()
    return tts


def _levels(vc):
    out = []
    for f in list(vc.sent):
        if f != SILENCE_FRAME:
            arr = array.array("h", f[:2])
            if sys.byteorder == "big":
                arr.byteswap()
            out.append(arr[0])
    return out


@pytest.mark.asyncio
async def test_completed_stream_suppresses_the_whole_file_reply(make_adapter, monkeypatch):
    adapter, vc = make_adapter()
    tts = _consumer(monkeypatch, adapter, LevelStreamer(chunks=10))
    tts.on_delta("Hello there, this is the first sentence. ")
    tts.on_delta("And here is a second one.")
    tts.finish()
    assert await tts.wait_complete(timeout=10)
    assert tts.completed and tts.suppress_whole_file and not tts.partial
    assert vc.wait_idle()
    assert _levels(vc).count(1000) == 20  # two clauses x 10 chunks x 20 ms
    assert GUILD not in adapter._voice_streams


@pytest.mark.asyncio
async def test_barge_in_stops_synthesis_and_never_replays(make_adapter, monkeypatch):
    adapter, vc = make_adapter(vc=FakeVoiceClient(frame_interval=0.02))
    streamer = LevelStreamer(chunks=200, pace=0.02)  # a 4 s clause, produced in real time
    tts = _consumer(monkeypatch, adapter, streamer)
    tts.on_delta("This is a long answer that the user is going to talk over.")
    tts.finish()
    deadline = time.monotonic() + 5
    while GUILD not in adapter._voice_streams or 1000 not in _levels(vc):
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    sink = adapter._voice_frame_sink(GUILD)
    start = time.monotonic()
    for i in range(20):
        sink(ALLOWED_USER, _speech_frame(), start + i * 0.02)
    await asyncio.sleep(0.1)
    pulled_at_cut = streamer.pulled
    await tts.wait_complete(timeout=10)
    assert tts.done
    assert streamer.pulled <= pulled_at_cut + 1  # at most the chunk already in flight
    assert streamer.pulled < 200
    assert tts.suppress_whole_file  # audio was heard: never replay the reply from the start
    assert vc.wait_idle()
    assert GUILD not in adapter._voice_streams


@pytest.mark.asyncio
async def test_declined_stream_leaves_whole_file_fallback(make_adapter, monkeypatch):
    adapter, vc = make_adapter({"enabled": False})
    tts = _consumer(monkeypatch, adapter, LevelStreamer())
    tts.on_delta("Hello there.")
    tts.finish()
    await tts.wait_complete(timeout=5)
    assert not tts.suppress_whole_file and not tts.completed
    assert vc.plays == 0


@pytest.mark.asyncio
async def test_provider_failure_before_audio_leaves_whole_file_fallback(make_adapter, monkeypatch):
    adapter, vc = make_adapter()
    tts = _consumer(monkeypatch, adapter, LevelStreamer(fail=True))
    tts.on_delta("Hello there.")
    tts.finish()
    await tts.wait_complete(timeout=5)
    assert not tts.suppress_whole_file and not tts.completed
    assert vc.plays == 0 and GUILD not in adapter._voice_streams
