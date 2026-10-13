"""Building blocks for streaming TTS into a Discord voice channel, and for barge-in.

stdlib only (no discord.py, no numpy): ``FrameQueue.read`` runs on discord.py's player thread and
``BargeInGate.feed`` on its socket-reader thread, while the asyncio loop writes. Each turn gets its
own ``FrameQueue``, so the queue object is the turn's identity: once cut, it never yields audio
again, and frames that arrive late for an interrupted turn are dropped, never played after the
next turn has started. Nothing here records audio or text; stats are counts and timings only.
"""

from __future__ import annotations

import array
import logging
import math
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Discord-native frame geometry (matches discord.opus.Encoder and voice_mixer.py).
SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH, FRAME_MS = 48000, 2, 2, 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000          # 960
FRAME_BYTES = SAMPLES_PER_FRAME * CHANNELS * SAMPLE_WIDTH   # 3840
SILENCE_FRAME = b"\x00" * FRAME_BYTES

_MIN_RATE, _MAX_RATE = 8000, 192000


class UnsupportedAudioFormat(ValueError):
    """The declared PCM format cannot be framed for Discord (the caller falls back)."""


def _int16(data: bytes) -> "array.array[int]":
    samples = array.array("h", data)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _int16_bytes(samples: "array.array[int]") -> bytes:
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


class PCMFramer:
    """s16le PCM at any rate (mono or stereo) -> whole 20 ms 48 kHz stereo frames.

    Linear interpolation addressed by absolute sample index (integer arithmetic), so the output
    does not depend on how the input was chunked; a sample split across chunks is carried over."""

    def __init__(self, sample_rate: int, channels: int, sample_width: int = 2):
        if sample_width != 2 or channels not in (1, 2) or not _MIN_RATE <= int(sample_rate) <= _MAX_RATE:
            raise UnsupportedAudioFormat(f"{sample_rate} Hz / {channels} ch / {sample_width * 8}-bit")
        self._rate, self._channels = int(sample_rate), channels
        self._carry = b""                       # bytes of a sample split across chunks
        self._left: List[int] = []              # buffered input from absolute index _base
        self._right: List[int] = []
        self._base = 0
        self._emitted = 0                       # output samples (per channel) produced so far
        self._out = bytearray()                 # 48 kHz stereo bytes not yet a whole frame

    def feed(self, chunk: bytes) -> List[bytes]:
        data = self._carry + bytes(chunk)
        usable = len(data) - len(data) % (2 * self._channels)
        self._carry = data[usable:]
        samples = _int16(data[:usable])
        if self._channels == 2:
            self._left.extend(samples[0::2])
            self._right.extend(samples[1::2])
        else:
            self._left.extend(samples)
            self._right.extend(samples)
        self._resample(final=False)
        return self._take_frames()

    def flush(self) -> List[bytes]:
        """Emit what is left, padding the last frame with silence."""
        self._resample(final=True)
        frames = self._take_frames()
        if self._out:
            frames.append(bytes(self._out) + b"\x00" * (FRAME_BYTES - len(self._out)))
            self._out.clear()
        return frames

    def _resample(self, *, final: bool) -> None:
        left, right, rate, base = self._left, self._right, self._rate, self._base
        end = base + len(left)  # one past the last buffered input index
        out = array.array("h")
        k = self._emitted
        while True:
            pos, rem = divmod(k * rate, SAMPLE_RATE)
            if pos >= end or (rem and pos + 1 >= end and not final):
                break
            i = pos - base
            if rem == 0 or pos + 1 >= end:  # exact sample hit, or the last sample held on flush
                out.append(left[i])
                out.append(right[i])
            else:
                out.append(left[i] + (left[i + 1] - left[i]) * rem // SAMPLE_RATE)
                out.append(right[i] + (right[i + 1] - right[i]) * rem // SAMPLE_RATE)
            k += 1
        self._emitted = k
        keep_from = min(k * rate // SAMPLE_RATE, end) - base  # earliest index still needed
        if keep_from > 0:
            del left[:keep_from], right[:keep_from]
            self._base += keep_from
        self._out += _int16_bytes(out)

    def _take_frames(self) -> List[bytes]:
        whole = len(self._out) - len(self._out) % FRAME_BYTES
        frames = [bytes(self._out[i:i + FRAME_BYTES]) for i in range(0, whole, FRAME_BYTES)]
        del self._out[:whole]
        return frames


class FrameQueue:
    """One turn's outbound audio: a bounded, thread-safe FIFO of 20 ms frames.

    ``read`` (player thread) returns the next frame, silence while the writer is behind, and
    None once the turn is over (closed and drained, or cut). ``offer`` never blocks: False means
    full and the async writer applies backpressure. After ``cut`` every queued and late frame is
    dropped. ``on_cut(reason)`` runs once, on whichever thread cut the turn and outside the queue
    lock, so the owner can learn that a player, mixer or cleanup ended it from outside; it must
    not block and its failures never reach the caller."""

    def __init__(self, max_frames: int):
        self._frames: "deque[bytes]" = deque()
        self._max = max(1, int(max_frames))
        self._lock = threading.Lock()
        self._closed = False
        self._cut_reason = ""
        self.frames_in = self.frames_out = self.underruns = self.dropped = 0
        self._created_at = time.monotonic()
        self._first_out_at: Optional[float] = None
        self._cut_at: Optional[float] = None
        self._silent_at: Optional[float] = None
        self.on_cut: Optional[Callable[[str], None]] = None

    def offer(self, frame: bytes) -> bool:
        with self._lock:
            if self._cut_reason:
                self.dropped += 1
                return True
            if len(self._frames) >= self._max:
                return False
            self._frames.append(frame)
            self.frames_in += 1
            return True

    def read(self) -> Optional[bytes]:
        with self._lock:
            if self._cut_reason:
                if self._silent_at is None:
                    self._silent_at = time.monotonic()
                return None
            if self._frames:
                self.frames_out += 1
                if self._first_out_at is None:
                    self._first_out_at = time.monotonic()
                return self._frames.popleft()
            if self._closed:
                return None
            self.underruns += 1
            return SILENCE_FRAME

    def close(self) -> None:
        """The writer is done; the player drains what is queued, then the turn ends."""
        with self._lock:
            self._closed = True

    def cut(self, reason: str) -> bool:
        """Stop this turn now. Returns False when it was already cut (first reason wins)."""
        with self._lock:
            if self._cut_reason:
                return False
            self._cut_reason = reason = reason or "cut"
            self._cut_at = time.monotonic()
            self.dropped += len(self._frames)
            self._frames.clear()
        callback = self.on_cut
        if callback is not None:
            try:
                callback(reason)
            except Exception:
                logger.debug("Voice stream on_cut callback failed", exc_info=True)
        return True

    @property
    def cut_reason(self) -> str:
        return self._cut_reason

    @property
    def closed(self) -> bool:
        """The writer called ``close``: no more audio is coming (queued frames may remain)."""
        return self._closed

    @property
    def finished(self) -> bool:
        with self._lock:
            return bool(self._cut_reason) or (self._closed and not self._frames)

    @property
    def first_out_at(self) -> Optional[float]:
        """Monotonic time the player pulled this turn's first queued frame (None until then)."""
        return self._first_out_at

    @property
    def buffered(self) -> int:
        with self._lock:
            return len(self._frames)

    def timings_ms(self) -> Dict[str, float]:
        """Stage timings that happened: begin -> first audible frame, cut -> player saw silence."""
        out: Dict[str, float] = {}
        if self._first_out_at is not None:
            out["first_audio_ms"] = round((self._first_out_at - self._created_at) * 1000, 1)
        if self._cut_at is not None and self._silent_at is not None:
            out["cut_to_silence_ms"] = round((self._silent_at - self._cut_at) * 1000, 1)
        return out


def frame_rms(pcm: bytes) -> float:
    """RMS level (int16 units) of a 48 kHz stereo frame, measured on the left channel."""
    samples = _int16(pcm[: len(pcm) - len(pcm) % 4])[0::2]
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


_BYTES_PER_MS = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH / 1000.0


class _SpeechRun:
    __slots__ = ("speech_ms", "last_voiced_at", "fired")

    def __init__(self) -> None:
        self.speech_ms, self.last_voiced_at, self.fired = 0.0, 0.0, False


class BargeInGate:
    """Per-speaker sustained-speech detector over decoded 20 ms frames.

    A frame at or above ``rms_threshold`` is speech. A speaker's speech counts up while gaps stay
    within ``hangover_ms``; reaching ``sustain_ms`` fires once for that run. Short interjections,
    quiet noise and several people each saying a little never add up to a trigger."""

    def __init__(self, *, rms_threshold: float, sustain_ms: int, hangover_ms: int = 200):
        self._threshold = float(rms_threshold)
        self._sustain_ms = float(sustain_ms)
        self._hangover_s = hangover_ms / 1000.0
        self._lock = threading.Lock()
        self._runs: Dict[Any, _SpeechRun] = {}

    def feed(self, speaker: Any, pcm: bytes, now: float) -> bool:
        voiced = frame_rms(pcm) >= self._threshold
        with self._lock:
            run = self._runs.get(speaker)
            if run is not None and now - run.last_voiced_at > self._hangover_s:
                run = None
            if not voiced:
                if run is None:
                    self._runs.pop(speaker, None)
                return False
            if run is None:
                run = self._runs[speaker] = _SpeechRun()
            run.speech_ms += len(pcm) / _BYTES_PER_MS
            run.last_voiced_at = now
            if run.fired or run.speech_ms < self._sustain_ms:
                return False
            run.fired = True
            return True

    def reset(self) -> None:
        with self._lock:
            self._runs.clear()


def _finite(raw: Any, default: float, lo: float, hi: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(hi, max(lo, value))


def _flag(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower() in {"true", "1", "yes", "on"}
    return default


@dataclass(frozen=True)
class VoiceStreamConfig:
    """``discord.voice_streaming`` from config.yaml; malformed values fall back to defaults."""

    enabled: bool = False
    buffer_frames: int = 50
    stall_timeout_s: float = 5.0
    producer_timeout_s: float = 60.0
    barge_in: bool = True
    barge_in_ms: int = 300
    barge_in_rms: int = 500

    @classmethod
    def from_mapping(cls, raw: Any) -> "VoiceStreamConfig":
        if not isinstance(raw, dict):
            return cls()
        d = cls()
        buffer_ms = _finite(raw.get("buffer_ms"), d.buffer_frames * FRAME_MS, 100, 10000)
        return cls(
            enabled=_flag(raw.get("enabled"), False),
            buffer_frames=int(buffer_ms // FRAME_MS),
            stall_timeout_s=_finite(raw.get("stall_timeout_seconds"), d.stall_timeout_s, 0.5, 60),
            producer_timeout_s=_finite(raw.get("producer_timeout_seconds"), d.producer_timeout_s, 1, 600),
            barge_in=_flag(raw.get("barge_in"), d.barge_in),
            barge_in_ms=int(_finite(raw.get("barge_in_ms"), d.barge_in_ms, 60, 3000)),
            barge_in_rms=int(_finite(raw.get("barge_in_rms"), d.barge_in_rms, 50, 32767)),
        )
