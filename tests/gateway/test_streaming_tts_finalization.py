"""Streaming-TTS finalisation must not cut off a long reply that is still being spoken.

A real-time sink (a Discord voice channel) plays audio no faster than real time, so a reply can
outlast the text that produced it. The finaliser's timeout is therefore *idle* time: it restarts
whenever the consumer synthesises or writes something, and a drain that stops making progress is
still aborted.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from gateway.platforms.base import AudioFormat, StreamingTTSHandle
from gateway.run_turn import GatewayTurnMixin
from gateway.streaming_tts_consumer import StreamingTTSConsumer
from tools.tts_streaming import SentenceChunker


class _Streamer:
    sample_rate, channels, sample_width = 24000, 1, 2

    def __init__(self, chunks):
        self.chunks = chunks

    def stream(self, text):
        for i in range(self.chunks):
            yield b"\x01\x00" * 480


class _PacedAdapter:
    """A real-time sink: each write takes ``per_write`` seconds (or never returns when stuck)."""

    name = "paced"

    def __init__(self, per_write, *, stuck_after=None):
        self.per_write, self.stuck_after = per_write, stuck_after
        self.writes = 0
        self.aborted_with = None
        self.handle = None

    def supports_streaming_tts(self, chat_id, audio_format):
        return True

    async def begin_streaming_tts(self, chat_id, audio_format, metadata=None):
        self.handle = StreamingTTSHandle(chat_id=chat_id, audio_format=audio_format)
        return self.handle

    async def write_streaming_tts(self, handle, chunk):
        self.writes += 1
        if self.stuck_after is not None and self.writes > self.stuck_after:
            await asyncio.sleep(3600)
        await asyncio.sleep(self.per_write)

    async def finish_streaming_tts(self, handle, *, interrupted=False):
        pass

    async def abort_streaming_tts(self, handle, error=None):
        self.aborted_with = error
        handle.aborted = True


def _consumer(adapter, streamer, loop):
    consumer = StreamingTTSConsumer.__new__(StreamingTTSConsumer)
    consumer._adapter, consumer._chat_id, consumer._loop, consumer._metadata = adapter, "chat", loop, None
    consumer._audio_format = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
    consumer._streamer = streamer
    consumer._chunker = SentenceChunker()
    consumer._queue = queue.Queue(maxsize=256)
    consumer._handle = None
    consumer._completed = consumer._partial = consumer._aborted = False
    consumer._finished = consumer._dropped = consumer._suppress_whole_file = False
    consumer._task = None
    consumer._lock = threading.Lock()
    consumer._strip_markdown = lambda t: t
    return consumer


@pytest.mark.asyncio
async def test_a_reply_that_outlasts_the_idle_timeout_is_not_cut_off():
    adapter = _PacedAdapter(per_write=0.05)
    consumer = _consumer(adapter, _Streamer(chunks=16), asyncio.get_running_loop())  # ~0.8 s to play
    consumer.start()
    consumer.on_delta("A reply that takes a while to speak aloud. ")
    consumer.finish()
    started = time.monotonic()
    assert await consumer.wait_while_progressing(idle_timeout=0.3) is True
    assert time.monotonic() - started > 0.5  # it waited well past a flat 0.3 s timeout
    assert consumer.done and consumer.completed and adapter.aborted_with is None
    assert adapter.writes == 16


@pytest.mark.asyncio
async def test_a_flat_timeout_would_have_cut_the_same_reply():
    """Characterises the defect: ``wait_complete`` gives up while the reply is still playing."""
    adapter = _PacedAdapter(per_write=0.05)
    consumer = _consumer(adapter, _Streamer(chunks=16), asyncio.get_running_loop())
    consumer.start()
    consumer.on_delta("A reply that takes a while to speak aloud. ")
    consumer.finish()
    assert await consumer.wait_complete(timeout=0.3) is False
    assert not consumer.done  # still speaking: the gateway would now abort it
    await consumer.wait_complete(timeout=3)


@pytest.mark.asyncio
async def test_a_drain_that_stops_making_progress_still_times_out():
    adapter = _PacedAdapter(per_write=0.01, stuck_after=2)
    consumer = _consumer(adapter, _Streamer(chunks=16), asyncio.get_running_loop())
    consumer.start()
    consumer.on_delta("A reply whose sink wedges after two chunks. ")
    consumer.finish()
    started = time.monotonic()
    assert await consumer.wait_while_progressing(idle_timeout=0.4) is False
    assert 0.35 < time.monotonic() - started < 2.0
    assert not consumer.done
    consumer.abort("test cleanup")
    consumer._task.cancel()  # the wedged write never returns on its own
    with contextlib.suppress(asyncio.CancelledError):
        await consumer._task


@pytest.mark.asyncio
async def test_without_a_task_it_just_reports_the_outcome():
    consumer = _consumer(_PacedAdapter(0), _Streamer(1), asyncio.get_running_loop())
    assert await consumer.wait_while_progressing(idle_timeout=0.1) is False


class _FakeConsumer:
    def __init__(self, *, done_after_wait):
        self.done = False
        self._done_after_wait = done_after_wait
        self.suppress_whole_file = False
        self.calls = []

    def finish(self):
        self.calls.append("finish")

    async def wait_while_progressing(self, idle_timeout):
        self.calls.append(("wait_while_progressing", idle_timeout))
        self.done = self._done_after_wait
        return self.done

    async def wait_complete(self, timeout):
        self.calls.append(("wait_complete", timeout))
        return False

    def abort(self, reason):
        self.calls.append(("abort", reason))


async def _finalize(consumer):
    turn_ctx = SimpleNamespace(streaming_tts_consumer_holder=[consumer], session_key="s", run_generation=1)
    await GatewayTurnMixin._run_agent_finalize_streaming_tts(SimpleNamespace(), turn_ctx, None)


@pytest.mark.asyncio
async def test_the_gateway_finaliser_waits_on_progress_and_does_not_abort_a_finished_drain():
    consumer = _FakeConsumer(done_after_wait=True)
    await _finalize(consumer)
    assert consumer.calls == ["finish", ("wait_while_progressing", 10.0)]


@pytest.mark.asyncio
async def test_the_gateway_finaliser_aborts_a_drain_that_never_finished():
    consumer = _FakeConsumer(done_after_wait=False)
    await _finalize(consumer)
    assert consumer.calls == [
        "finish", ("wait_while_progressing", 10.0),
        ("abort", "streaming TTS finalisation timeout"), ("wait_complete", 2.0),
    ]
