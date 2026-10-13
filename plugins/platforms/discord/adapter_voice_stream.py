"""Streaming TTS into a Discord voice channel, with allowlisted barge-in (opt-in).

Implements the gateway streaming-TTS contract (``supports``/``begin``/``write``/``finish``/
``abort_streaming_tts``) for chats bound to a connected voice channel, so a reply starts playing
while it is still being synthesised. Off unless ``discord.voice_streaming.enabled``; every decline
(disabled, unbound chat, disconnected, unsupported format, someone else's audio playing) returns
None and the gateway keeps its whole-file TTS path.

Playback ownership: with a ``VoiceMixer`` installed the turn joins it as one more speech child
(the continuous mixer is never stopped or replaced); otherwise the turn plays as the VoiceClient's
only source and is the only thing this module ever stops. Each turn has its own ``FrameQueue``:
cutting it (supersede, barge-in, leave, stall, abort) silences the turn within one frame and drops
its late audio, so nothing from an old turn plays after a new one starts.

Barge-in: the voice receiver hands each decoded 20 ms frame to ``_voice_frame_sink`` on its socket
thread. Sustained speech from a non-bot speaker hops to the event loop, where the adapter's own
``_is_allowed_user`` decides; an allowed speaker cuts the turn and the handle is marked aborted so
the gateway stops synthesising. The utterance itself still reaches the agent through the normal
transcription path, which is what interrupts the running turn. Logs carry counts and timings only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from gateway.platforms.base import AudioFormat, StreamingTTSHandle
from plugins.platforms.discord.voice_stream import (
    FRAME_BYTES,
    SILENCE_FRAME,
    BargeInGate,
    FrameQueue,
    PCMFramer,
    UnsupportedAudioFormat,
    VoiceStreamConfig,
)

logger = logging.getLogger(__name__)

_BACKPRESSURE_POLL_S = 0.01


class VoiceStreamStalled(RuntimeError):
    """The voice turn cannot make progress (player wedged or dead, or the VC disconnected)."""


@dataclass
class DiscordStreamingTTSHandle(StreamingTTSHandle):
    guild_id: int = 0
    queue: Any = None                 # voice_stream.FrameQueue: this turn's identity
    framer: Any = None                # voice_stream.PCMFramer
    source: Any = None                # our StreamSource on the direct path; None through the mixer
    gate: Optional[BargeInGate] = None
    loop: Any = None
    bot_user_id: int = 0
    paused_receiver: Any = None       # legacy echo guard, resumed on release
    player_error: Optional[BaseException] = None
    watchdog: Any = None              # asyncio.Task: ends the turn when the producer starves
    holds_realtime_floor: bool = False  # took the realtime floor; handed back exactly once on release
    released: bool = False

    @property
    def cut_reason(self) -> str:
        return self.queue.cut_reason if self.queue is not None else ""


def _voice_mixer_module():
    """Same resolution as ``adapter._voice_mixer_module`` (flat sibling import, else the package),
    so the streams and the installed mixer always come from one module copy."""
    try:
        import voice_mixer
        return voice_mixer
    except ImportError:
        from plugins.platforms.discord import voice_mixer
        return voice_mixer


class DiscordVoiceStreamMixin:
    _voice_clients: Dict[int, Any]
    _voice_receivers: Dict[int, Any]
    _voice_text_channels: Dict[int, Any]
    _voice_mixers: Dict[int, Any]
    _voice_fx_cfg: Dict[str, Any]
    _client: Any
    _is_allowed_user: Callable[..., bool]
    _lead_silence_bytes: Callable[[], bytes]
    _cancel_voice_timeout: Callable[[int], None]
    _reset_voice_timeout: Callable[[int], None]
    _realtime_take_floor: Callable[[int], Any]
    _realtime_claims_chat: Callable[[str], bool]
    _realtime_return_floor: Callable[[int], None]
    _realtime_playback: Dict[int, Any]

    def _init_voice_streaming(self) -> None:
        self._voice_stream_cfg = self._load_voice_stream_config()
        self._voice_streams: Dict[int, DiscordStreamingTTSHandle] = {}  # guild_id -> active turn

    def _load_voice_stream_config(self) -> VoiceStreamConfig:
        """``discord.voice_streaming`` from config.yaml; any problem means the default (off)."""
        try:
            from hermes_cli.config import read_raw_config
            raw = ((read_raw_config() or {}).get("discord") or {}).get("voice_streaming")
        except Exception as e:
            logger.debug("Could not load discord.voice_streaming config: %s", e, exc_info=True)
            raw = None
        return VoiceStreamConfig.from_mapping(raw)

    # --- contract -------------------------------------------------------------------------

    def _voice_stream_target(self, chat_id: str, audio_format: AudioFormat):
        """(guild_id, connected vc) when *chat_id* is a voice-bound text channel and streaming is
        on and the format is framable; None otherwise."""
        cfg = getattr(self, "_voice_stream_cfg", None)
        if cfg is None or not cfg.enabled:
            return None
        guild_id = next((g for g, tc in self._voice_text_channels.items() if str(tc) == str(chat_id)), None)
        if guild_id is None:
            return None
        vc = self._voice_clients.get(guild_id)
        if vc is None or not vc.is_connected():
            return None
        try:
            PCMFramer(audio_format.sample_rate, audio_format.channels, audio_format.sample_width)
        except UnsupportedAudioFormat:
            return None
        return guild_id, vc

    @staticmethod
    def _voice_client_idle(vc) -> bool:
        """True when no live audio owns the VoiceClient: nothing playing and nothing paused. A
        paused clip is someone's audio waiting to resume (``is_playing`` is False for it), so it
        is never replaced. discord.py keeps an *ended* player on ``vc.source`` until ``stop()``;
        that is idle, and a later ``play`` replaces it harmlessly."""
        if vc.is_playing():
            return False
        is_paused = getattr(vc, "is_paused", None)
        return not (callable(is_paused) and is_paused())

    def supports_streaming_tts(self, chat_id: str, audio_format: AudioFormat) -> bool:
        # A live realtime session speaks for a delegated turn itself: decline before any synthesis.
        return not self._realtime_claims_chat(chat_id) and self._voice_stream_target(chat_id, audio_format) is not None

    async def begin_streaming_tts(
        self, chat_id: str, audio_format: AudioFormat, metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[DiscordStreamingTTSHandle]:
        target = self._voice_stream_target(chat_id, audio_format)
        if target is None:
            return None
        guild_id, vc = target
        cfg: VoiceStreamConfig = self._voice_stream_cfg
        mixer = self._voice_mixers.get(guild_id)
        mixer_owns_player = mixer is not None and vc.is_playing() and vc.source is mixer
        if mixer is not None and not mixer_owns_player:
            # A stale/uninstalled mixer is not an idle path: its speech queue has no consumer,
            # while vc.play would replace whatever currently owns the player.
            return None
        previous = self._voice_streams.get(guild_id)
        realtime = (getattr(self, "_realtime_playback", None) or {}).get(guild_id)
        if not mixer_owns_player and not self._voice_client_idle(vc):
            # Busy is acceptable only when the player is ours to replace: our own prior turn, or
            # this adapter's realtime output, which yields the floor below. Never anyone else's audio.
            ours = previous is not None and previous.source is not None and vc.source is previous.source
            realtime_owned = realtime is not None and realtime._source is not None and vc.source is realtime._source
            if not (ours or realtime_owned):
                return None
        use_mixer = mixer_owns_player
        took_floor = False
        if realtime is not None:
            # Hermes owns the floor while it speaks: cut realtime audio first so the voices never overlap.
            await self._realtime_take_floor(guild_id)
            took_floor = True
        if previous is not None:
            # Replace only our prior turn; identity checks prevent stopping someone else's source.
            self._cut_voice_stream(previous, "superseded")
        handle = DiscordStreamingTTSHandle(
            chat_id=str(chat_id), audio_format=audio_format, guild_id=guild_id,
            queue=FrameQueue(cfg.buffer_frames),
            framer=PCMFramer(audio_format.sample_rate, audio_format.channels, audio_format.sample_width),
            gate=BargeInGate(rms_threshold=cfg.barge_in_rms, sustain_ms=cfg.barge_in_ms) if cfg.barge_in else None,
            loop=asyncio.get_running_loop(),
            bot_user_id=int(getattr(getattr(vc, "user", None), "id", 0) or 0),
            holds_realtime_floor=took_floor,
        )
        handle.queue.on_cut = lambda reason: self._voice_queue_cut(handle)
        # Queue the lead-in silence before the player starts so speech follows it directly.
        lead = [SILENCE_FRAME] * (len(self._lead_silence_bytes()) // FRAME_BYTES)
        while lead and handle.queue.offer(lead[-1]):
            lead.pop()
        if not self._start_voice_stream_playback(handle, vc, mixer if use_mixer else None):
            if handle.holds_realtime_floor:
                handle.holds_realtime_floor = False
                self._realtime_return_floor(guild_id)
            return None
        self._voice_streams[guild_id] = handle
        self._cancel_voice_timeout(guild_id)
        handle.watchdog = asyncio.ensure_future(self._voice_producer_watchdog(handle))
        await self._offer_voice_frames(handle, lead)
        return handle

    def _start_voice_stream_playback(self, handle: DiscordStreamingTTSHandle, vc, mixer) -> bool:
        if mixer is not None:
            mixer.play_speech_stream(handle.queue, gain=float(self._voice_fx_cfg.get("speech_gain", 1.0)))
            return True

        def _after(error):  # player thread: record only, the writer reports it
            if error is not None:
                handle.player_error = error

        handle.source = _voice_mixer_module().StreamSource(handle.queue)
        try:
            vc.play(handle.source, after=_after)
        except Exception as e:  # lost a race with other playback, or the VC just dropped
            logger.debug("Voice stream declined: play failed (guild=%s): %s", handle.guild_id, e, exc_info=True)
            handle.queue.on_cut = None  # never registered: nothing to release
            handle.queue.cut("declined")
            return False
        if not handle.gate:
            receiver = self._voice_receivers.get(handle.guild_id)
            if receiver is not None:
                receiver.pause()  # legacy echo guard when barge-in is off
                handle.paused_receiver = receiver
        return True

    async def write_streaming_tts(self, handle: StreamingTTSHandle, chunk: bytes) -> None:
        if not isinstance(handle, DiscordStreamingTTSHandle):
            return
        error, handle.player_error = handle.player_error, None
        if error is not None:
            self._cut_voice_stream(handle, "player-error")
            raise VoiceStreamStalled(f"voice player failed: {error}")
        if handle.cut_reason or handle.aborted:
            return  # late audio for a turn that is over: dropped
        vc = self._voice_clients.get(handle.guild_id)
        if vc is None or not vc.is_connected():
            self._cut_voice_stream(handle, "disconnected")
            raise VoiceStreamStalled("voice channel disconnected")
        await self._offer_voice_frames(handle, handle.framer.feed(chunk))

    async def finish_streaming_tts(self, handle: StreamingTTSHandle, *, interrupted: bool = False) -> None:
        if not isinstance(handle, DiscordStreamingTTSHandle):
            return
        if interrupted:
            self._cut_voice_stream(handle, "interrupted")
            return
        handle.queue.close()
        if handle.holds_realtime_floor:
            # All provider audio buffered before Hermes took the floor must be stale on resume.
            # Reset only the realtime playback queue; the Hermes stream's queue is independent.
            playback = self._realtime_playback.get(handle.guild_id)
            if playback is not None:
                playback.current = None
        if not handle.cut_reason:
            await self._offer_voice_frames(handle, handle.framer.flush())
            await self._wait_voice_stream(handle, lambda: handle.queue.finished)
        self._release_voice_stream(handle)

    async def abort_streaming_tts(self, handle: StreamingTTSHandle, error: Optional[str] = None) -> None:
        if isinstance(handle, DiscordStreamingTTSHandle):
            self._cut_voice_stream(handle, "aborted")

    # --- flow control -----------------------------------------------------------------------

    async def _offer_voice_frames(self, handle: DiscordStreamingTTSHandle, frames) -> None:
        for frame in frames:
            if not handle.queue.offer(frame):
                await self._wait_voice_stream(handle, lambda f=frame: handle.queue.offer(f))

    async def _wait_voice_stream(self, handle: DiscordStreamingTTSHandle, done: Callable[[], bool]) -> None:
        """Poll *done* while the player keeps pulling frames; no progress for the stall timeout
        (or a disconnect) cuts the turn and raises."""
        timeout = self._voice_stream_cfg.stall_timeout_s
        progress, deadline = handle.queue.frames_out, time.monotonic() + timeout
        while not done():
            if handle.cut_reason:
                return
            vc = self._voice_clients.get(handle.guild_id)
            if vc is None or not vc.is_connected():
                self._cut_voice_stream(handle, "disconnected")
                raise VoiceStreamStalled("voice channel disconnected")
            if handle.queue.frames_out != progress:
                progress, deadline = handle.queue.frames_out, time.monotonic() + timeout
            elif time.monotonic() > deadline:
                self._cut_voice_stream(handle, "stalled")
                raise VoiceStreamStalled(f"voice player made no progress for {timeout:.1f}s")
            await asyncio.sleep(_BACKPRESSURE_POLL_S)

    async def _voice_producer_watchdog(self, handle: DiscordStreamingTTSHandle) -> None:
        """End a turn whose producer went quiet without finishing or aborting (a cancelled drain
        task, a hung provider, a very long tool call). The player would otherwise pull silence
        forever: the duck holds, the inactivity timer stays off, and with barge-in off the receiver
        stays paused. Starved means: nothing queued, nothing new offered, writer not closed, for
        ``producer_timeout_seconds``. A full queue is backpressure, not starvation (that is the
        stall timeout's job in ``_wait_voice_stream``)."""
        q = handle.queue
        limit = self._voice_stream_cfg.producer_timeout_s
        poll = min(0.25, limit / 4)
        seen, idle_since = q.frames_in, time.monotonic()
        while not handle.released and not q.closed and not q.cut_reason:
            await asyncio.sleep(poll)
            now = time.monotonic()
            if q.frames_in != seen or q.buffered:
                seen, idle_since = q.frames_in, now
            elif now - idle_since > limit:
                logger.warning(
                    "Voice stream producer starved for %.1fs (guild=%s); ending the reply", limit, handle.guild_id,
                )
                self._cut_voice_stream(handle, "producer-starved")
                return

    # --- turn teardown ---------------------------------------------------------------------

    def _cut_voice_stream(self, handle: DiscordStreamingTTSHandle, reason: str) -> None:
        """End *handle*'s turn now (first reason wins) and stop our own source, never anyone else's."""
        handle.queue.cut(reason)
        handle.aborted = True
        vc = self._voice_clients.get(handle.guild_id)
        if handle.source is not None and vc is not None and vc.source is handle.source:
            try:
                vc.stop()
            except Exception as e:
                logger.debug("Voice stream stop failed (guild=%s): %s", handle.guild_id, e, exc_info=True)
        self._release_voice_stream(handle)

    def _voice_queue_cut(self, handle: DiscordStreamingTTSHandle) -> None:
        """``FrameQueue.on_cut``: the turn's queue was cut, possibly by the player or the mixer on
        their own thread (``vc.stop``, ``stop_speech``, source cleanup). Hop to the event loop and
        finish the teardown there, so the handle is marked aborted (synthesis stops), and the
        receiver and inactivity timer are restored. Never raises into the cutting thread."""
        if handle.released:
            return
        try:
            handle.loop.call_soon_threadsafe(self._voice_queue_cut_on_loop, handle)
        except Exception:  # loop already closed: the process is shutting down
            logger.debug("Voice stream cut notification dropped (guild=%s)", handle.guild_id, exc_info=True)

    def _voice_queue_cut_on_loop(self, handle: DiscordStreamingTTSHandle) -> None:
        if not handle.released:
            self._cut_voice_stream(handle, handle.cut_reason or "cut")

    def _release_voice_stream(self, handle: DiscordStreamingTTSHandle) -> None:
        if handle.released:
            return
        handle.released = True
        watchdog, handle.watchdog = handle.watchdog, None
        if watchdog is not None and not watchdog.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:  # released outside a running loop
                current = None
            if watchdog is not current:  # the watchdog itself returns right after its own cut
                watchdog.cancel()
        if self._voice_streams.get(handle.guild_id) is handle:
            self._voice_streams.pop(handle.guild_id, None)
            if handle.guild_id in self._voice_clients:
                self._reset_voice_timeout(handle.guild_id)
        if handle.paused_receiver is not None:
            handle.paused_receiver.resume()
        if handle.holds_realtime_floor:
            handle.holds_realtime_floor = False
            self._realtime_return_floor(handle.guild_id)
        q = handle.queue
        logger.info(
            "Discord voice stream ended (guild=%s, reason=%s, frames_in=%d, frames_out=%d, "
            "underruns=%d, dropped=%d, timings=%s)",
            handle.guild_id, q.cut_reason or "completed", q.frames_in, q.frames_out,
            q.underruns, q.dropped, q.timings_ms(),
        )

    async def _close_voice_stream(self, guild_id: int, reason: str) -> None:
        handle = (getattr(self, "_voice_streams", None) or {}).get(guild_id)
        if handle is not None:
            self._cut_voice_stream(handle, reason)

    # --- barge-in --------------------------------------------------------------------------

    def _voice_frame_sink(self, guild_id: int) -> Callable[..., None]:
        """Receiver callback for one decoded 48 kHz stereo frame, on the socket thread."""

        def sink(user_id: int, pcm: bytes, now: Optional[float] = None) -> None:
            try:
                handle = self._voice_streams.get(guild_id)
                if handle is None or handle.gate is None or handle.queue.finished:
                    return
                if not user_id or user_id == handle.bot_user_id:
                    return  # unmapped SSRC, or our own audio
                if handle.gate.feed(user_id, pcm, time.monotonic() if now is None else now):
                    handle.loop.call_soon_threadsafe(self._voice_barge_in, handle, user_id)
            except Exception:
                logger.debug("Voice barge-in sink failed (guild=%s)", guild_id, exc_info=True)

        return sink

    def _voice_barge_in(self, handle: DiscordStreamingTTSHandle, user_id: int) -> None:
        """Event loop: the speaker must pass the adapter's normal voice authorization."""
        if handle.released or self._voice_streams.get(handle.guild_id) is not handle:
            return
        try:
            guild = self._client.get_guild(handle.guild_id) if self._client is not None else None
            allowed = self._is_allowed_user(str(user_id), guild=guild, is_dm=False)
            if allowed is not True:
                return
        except Exception:
            logger.warning("Voice barge-in authorization failed (guild=%s)", handle.guild_id, exc_info=True)
            return
        if allowed:
            logger.info("Voice barge-in: cutting streamed reply (guild=%s)", handle.guild_id)
            self._cut_voice_stream(handle, "barge-in")
