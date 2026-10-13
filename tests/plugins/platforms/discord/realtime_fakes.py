"""Fakes for the realtime-voice tests: a scripted Realtime transport, a playback port backed by a
real ``FrameQueue`` that the test drains like discord.py's player, a controllable Hermes delegate and
a manual clock. Passing tests are protocol evidence against these fakes, not live-service evidence."""

import array
import asyncio
import base64
import math
import sys
from typing import Any, Dict, List, Optional

from plugins.platforms.discord.realtime_voice import (
    DelegationRequest,
    DelegationResult,
    PlaybackTurn,
    RealtimeVoiceConfig,
    RealtimeVoiceSession,
)
from plugins.platforms.discord.voice_stream import FrameQueue


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class FakeTransport:
    def __init__(self):
        self.sent: List[Dict[str, Any]] = []
        self.inbox: "asyncio.Queue[Optional[dict]]" = asyncio.Queue()
        self.closed = False

    async def send(self, event):
        self.sent.append(event)

    async def recv(self):
        return await self.inbox.get()

    async def close(self):
        self.closed = True

    def of_type(self, kind: str) -> List[Dict[str, Any]]:
        return [e for e in self.sent if e.get("type") == kind]

    def appended_pcm(self) -> bytes:
        return b"".join(base64.b64decode(e["audio"]) for e in self.of_type("input_audio_buffer.append"))


class FakePlayback:
    def __init__(self, lead_frames: int = 0):
        self.turns: List[PlaybackTurn] = []
        self.blocked = False
        self.refuse = False
        self.lead_frames = lead_frames

    def output_blocked(self) -> bool:
        return self.blocked

    def begin(self, max_frames: int):
        if self.refuse:
            return None
        q = FrameQueue(max_frames + self.lead_frames)
        for _ in range(self.lead_frames):
            q.offer(b"\x00" * 3840)
        turn = PlaybackTurn(q, self.lead_frames)
        self.turns.append(turn)
        return turn

    def play(self, frames: int) -> List[bytes]:
        """Pull up to *frames* frames from the current turn, as the player thread would."""
        out = []
        for _ in range(frames):
            frame = self.turns[-1].queue.read()
            if frame is None:
                break
            out.append(frame)
        return out


class FakeDelegate:
    def __init__(self):
        self.requests: List[DelegationRequest] = []
        self.futures: List[asyncio.Future] = []

    async def run(self, request):
        self.requests.append(request)
        fut = asyncio.get_running_loop().create_future()
        self.futures.append(fut)
        return await fut

    def finish(self, index=-1, result=None):
        self.futures[index].set_result(result or DelegationResult("done", text="It is 21 degrees."))


def stereo_tone(freq: float, frames: int = 1, amplitude: int = 8000, start: int = 0) -> bytes:
    """48 kHz stereo s16le sine, *frames* x 20 ms."""
    arr = array.array("h")
    for i in range(start, start + 960 * frames):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / 48000))
        arr.extend((v, v))
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def mono24(samples: int, value: int = 1000) -> bytes:
    arr = array.array("h", [value] * samples)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def delta(rid: str, pcm: bytes, item: str = "item_1") -> Dict[str, Any]:
    return {"type": "response.output_audio.delta", "response_id": rid, "item_id": item,
            "content_index": 0, "output_index": 0, "delta": base64.b64encode(pcm).decode()}


ALLOWED, STRANGER, BOT = 42, 99, 1


def make_session(cfg: Optional[RealtimeVoiceConfig] = None, *, clock=None, playback=None, delegate=None,
                 allowed=(ALLOWED,), budget=None, is_allowed=None, control_authorized=None, on_activity=None,
                 recall=None):
    clock = clock or Clock()
    transport = FakeTransport()
    playback = playback or FakePlayback()
    delegate = delegate or FakeDelegate()

    async def connect():
        return transport

    session = RealtimeVoiceSession(
        cfg or RealtimeVoiceConfig(enabled=True), connect=connect, playback=playback, delegate=delegate,
        allowed_speakers=allowed, is_allowed=is_allowed, control_authorized=control_authorized, guild_id=111,
        bot_user_id=BOT, clock=clock, daily_budget=budget, on_activity=on_activity,
        recall=recall,
    )
    return session, transport, playback, delegate, clock
