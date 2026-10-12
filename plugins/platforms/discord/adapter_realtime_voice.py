"""Realtime (full-duplex) voice on ``DiscordAdapter`` (opt-in, off unless ``discord.realtime_voice``).

Wires ``realtime_voice.RealtimeVoiceSession`` to a joined voice channel:

* ingress: the voice receiver's ``frame_sink`` fans out to the streaming barge-in sink and to the
  session; the receiver is never paused while realtime is on (that pause is what made the old path
  half-duplex), and the whole-utterance STT path is suppressed so one utterance is not answered twice;
* egress: each model response is its own ``FrameQueue``, played through the ``VoiceMixer`` when one
  owns the player, else as the VoiceClient's only source when it is idle. discord.py then does Opus
  encoding, DAVE ``encrypt_opus`` and transport encryption exactly as for every other source;
* floor: a Hermes reply (streaming TTS or a played file) owns the player. Realtime output is cut when
  one begins and is not started while one is active, so the two voices never overlap;
* delegation: ``GatewayVoiceDelegate`` submits a request through ``_voice_input_callback``, the same
  entry a transcribed utterance takes, so authorization, the session, memory, tools and approval
  prompts are Hermes's own. The gateway correlates the final result to that exact event and the live
  Realtime session speaks the captured answer without a duplicate Hermes voice reply.

* Hermes `/realtime-voice start` directly enters ``start_realtime_voice`` as the explicit, authorized
  start action. It is a cost-bearing provider connection, so callers must use that door only after
  authorization; there is no separate confirmation token.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import secrets
import threading
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.platforms.discord.realtime_voice import (
    DailyAudioBudget,
    DelegationRequest,
    DelegationResult,
    OpenAIRealtimeTransport,
    PlaybackTurn,
    RealtimeUnavailable,
    RealtimeVoiceConfig,
    RealtimeVoiceSession,
    parse_snowflake,
    parse_speaker_ids,
)
from plugins.platforms.discord.voice_stream import FRAME_BYTES, SILENCE_FRAME, FrameQueue

logger = logging.getLogger(__name__)
_REALTIME_TRANSPORT_CLASS = OpenAIRealtimeTransport

# Hard ceilings for the canary: also the defaults. A configured limit may only tighten them; a value
# above either ceiling disables the control rather than being clamped.
CANARY_MAX_SESSION_S = 600.0       # 10 minutes per session
CANARY_DAILY_AUDIO_S = 1800.0      # 30 minutes of audio per day


@dataclasses.dataclass(frozen=True)
class RealtimeVoiceControlConfig:
    """``discord.realtime_voice_control``: a second, explicit canary gate. It names the one guild and
    the exact user IDs allowed to control and speak to realtime voice; it cannot enable voice itself.
    Anything malformed disables the control rather than loosening it."""
    enabled: bool = False
    guild_id: Optional[int] = None
    user_ids: frozenset = frozenset()
    max_session_s: float = CANARY_MAX_SESSION_S
    daily_audio_budget_s: float = CANARY_DAILY_AUDIO_S

    @classmethod
    def from_mapping(cls, raw: Any) -> "RealtimeVoiceControlConfig":
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return cls()
        try:
            guild_id = parse_snowflake(raw.get("guild_id"))
            users = parse_speaker_ids(raw.get("user_ids"))
            limits = []
            for key, default, cap in (("max_session_seconds", CANARY_MAX_SESSION_S, CANARY_MAX_SESSION_S),
                                      ("daily_audio_budget_seconds", CANARY_DAILY_AUDIO_S, CANARY_DAILY_AUDIO_S)):
                value = raw.get(key, default)
                if isinstance(value, bool) or not 0 < float(value) <= cap:
                    return cls()
                limits.append(float(value))
        except (TypeError, ValueError):
            return cls()
        return cls(True, guild_id, users, limits[0], limits[1])


@dataclasses.dataclass(frozen=True)
class StartTarget:
    """What a confirmation covers: the voice channel the session would join and the model and caps it
    would run with. Any difference at redemption time voids the grant."""
    guild_id: int
    channel_id: int
    model: str
    max_session_s: float
    daily_audio_budget_s: float


@dataclasses.dataclass(frozen=True)
class StartResult:
    started: bool
    message: str


@dataclasses.dataclass(frozen=True)
class _Grant:
    token: str
    target: StartTarget
    expires_at: float


class RealtimeVoiceControl:
    """Authorization state for one test guild and exact-user allowlist."""
    def __init__(self, adapter: Any, config: RealtimeVoiceControlConfig, *,
                 daily_audio_budget_s: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
        self.adapter, self.config, self._clock = adapter, config, clock
        self.budget = DailyAudioBudget(config.daily_audio_budget_s if daily_audio_budget_s is None
                                       else daily_audio_budget_s)

    def authorized(self, guild_id: Any, user_id: Any) -> bool:
        c = self.config
        return (c.enabled and type(guild_id) is int and guild_id == c.guild_id
                and type(user_id) is int and user_id in c.user_ids)

    async def execute(self, *, guild_id: int, user_id: int, action: str) -> str:
        a, c = self.adapter, self.config
        if not c.enabled:
            return "Realtime canary control is disabled."
        if not self.authorized(guild_id, user_id):
            return "Realtime canary control is not authorized here."
        if action == "status":
            session = (getattr(a, "_realtime_sessions", {}) or {}).get(guild_id)
            return "Realtime voice is active." if session is not None and not session.closed else "Realtime voice is inactive."
        if action == "stop":
            await a.stop_realtime_voice(guild_id, "stopped")
            return "Realtime voice stopped."
        if action != "start":
            return "Unknown realtime voice action."
        # Explicit invocation by the allowlisted requester is the start confirmation.
        return (await a.start_realtime_voice(guild_id, requester_id=user_id)).message


def _voice_mixer_module():
    try:
        import voice_mixer
        return voice_mixer
    except ImportError:
        from plugins.platforms.discord import voice_mixer
        return voice_mixer


class DiscordRealtimePlayback:
    """``PlaybackPort`` for one guild, with the streaming path's ownership rules: join the mixer
    when it owns the player, else play only on an idle VoiceClient, never stop someone else's audio."""

    def __init__(self, adapter: Any, guild_id: int):
        self._adapter, self._guild_id = adapter, guild_id
        self.current: Optional[FrameQueue] = None
        self._source: Any = None
        self._hermes_floor = False
        self._player_lock = threading.Lock()

    def output_blocked(self) -> bool:
        a = self._adapter
        if self._hermes_floor or self._guild_id in getattr(a, "_realtime_hermes_playing", {}):
            return True
        return a._voice_streams.get(self._guild_id) is not None

    def begin(self, max_frames: int) -> Optional[PlaybackTurn]:
        a, gid = self._adapter, self._guild_id
        vc = a._voice_clients.get(gid)
        if vc is None or not vc.is_connected():
            return None
        mixer = a._voice_mixers.get(gid)
        if not a._voice_client_idle(vc) and not (mixer is not None and vc.source is mixer and vc.is_playing()):
            return None
        return self._begin_locked(max_frames)

    def _begin_locked(self, max_frames: int) -> Optional[PlaybackTurn]:
        a, gid = self._adapter, self._guild_id
        vc = a._voice_clients.get(gid)
        if vc is None or not vc.is_connected():
            return None
        if self._hermes_floor or gid in getattr(a, "_realtime_hermes_playing", {}):
            return None
        lead = len(a._lead_silence_bytes()) // FRAME_BYTES
        queue = FrameQueue(max_frames + lead)
        for _ in range(lead):
            queue.offer(SILENCE_FRAME)
        mixer = a._voice_mixers.get(gid)
        if mixer is None and gid in getattr(a, "_realtime_hermes_playing", {}):
            return None
        if mixer is not None:
            if not (vc.is_playing() and vc.source is mixer):
                return None
            mixer.play_speech_stream(queue, gain=float(a._voice_fx_cfg.get("speech_gain", 1.0)))
        else:
            if not a._voice_client_idle(vc):
                return None
            source = _voice_mixer_module().StreamSource(queue)
            try:
                vc.play(source)
            except Exception as e:
                logger.debug("Realtime playback declined (guild=%s): %s", gid, e)
                return None
            self._source = source
        self.current = queue
        return PlaybackTurn(queue, lead_frames=lead)

    def release_direct_player(self) -> None:
        """Stop only this port's source; keep its current queue for floor truncation."""
        source, self._source = self._source, None
        vc = self._adapter._voice_clients.get(self._guild_id)
        if source is not None and vc is not None and vc.source is source:
            try:
                vc.stop()
            except Exception:
                logger.debug("Realtime player release failed (guild=%s)", self._guild_id, exc_info=True)

    def yield_floor(self, reason: str) -> None:
        """Hermes is about to speak: atomically prevent new Realtime player ownership."""
        with self._player_lock:
            self._hermes_floor = True
            queue = self.current
            if queue is not None and not queue.finished:
                queue.cut(reason)
            self.release_direct_player()

    def resume_floor(self) -> None:
        self._hermes_floor = False



class _PendingJob:
    """A delegated request waiting for Hermes lifecycle hooks, correlated by its stable event key."""

    __slots__ = ("text", "user_id", "started", "done", "event_key", "relay", "result_text")

    def __init__(self, text: str, user_id: int, loop: asyncio.AbstractEventLoop):
        self.text, self.user_id = text, int(user_id)
        self.relay = False        # Realtime speaks this job's answer, so Hermes's own voice stays quiet
        self.result_text = ""     # Hermes's final reply text, captured before it is delivered
        self.started: asyncio.Future = loop.create_future()
        self.done: asyncio.Future = loop.create_future()
        self.event_key: Optional[str] = None


class GatewayVoiceDelegate:
    """``HermesDelegate`` through the gateway's own spoken-input entry point.

    The request enters exactly as a transcribed utterance from the speaker who asked (same
    ``_voice_input_callback``), so Hermes applies its allowlist, session routing, memory, tools and
    approval prompts unchanged. That entry can drop input silently (unauthorized speaker, duplicate
    transcript, unbound channel), so a job Hermes has not started within ``delegate_admit_s`` is
    ``not-admitted``, and an admitted job holds its slot at most ``delegate_slot_max_s``.
    While the realtime session is live, Hermes's own voice for that one turn is suppressed (see
    ``_realtime_relays_event``) and its final reply text is captured for the voice layer to speak, so
    the result is ``delivered_by="voice"`` (one voice, no duplicate). Its text reply still goes to the
    channel. A job that never claimed the relay keeps ``delivered_by="hermes"``: Hermes answers in the
    text channel and its own voice, and Realtime only notes that it did."""

    def __init__(self, adapter: Any, guild_id: int):
        self._adapter, self._guild_id = adapter, guild_id

    async def run(self, request: DelegationRequest) -> DelegationResult:
        a = self._adapter
        callback = getattr(a, "_voice_input_callback", None)
        if callback is None:
            return DelegationResult("failed", delivered_by="hermes")
        cfg = getattr(a, "_realtime_cfg", None) or RealtimeVoiceConfig()
        job = _PendingJob(request.text, request.user_id, asyncio.get_running_loop())
        pending: List[_PendingJob] = a._realtime_pending_jobs.setdefault(self._guild_id, [])
        pending.append(job)
        event_holder: Dict[str, Any] = {}

        def capture(event: Any) -> None:
            if "event" in event_holder:
                return
            event_holder["event"] = event
            job.event_key = str((getattr(event, "metadata", None) or {}).get("realtime_delegate_event_key") or id(event))
            a._realtime_processing_events[id(event)] = self._guild_id

        try:
            await callback(guild_id=self._guild_id, user_id=request.user_id, transcript=request.text,
                           on_realtime_event_created=capture)
            event = event_holder.get("event")
            try:
                await asyncio.wait_for(asyncio.shield(job.started), cfg.delegate_admit_s)
            except asyncio.TimeoutError:
                return DelegationResult("not-admitted", delivered_by="voice")
            try:
                ok = await asyncio.wait_for(asyncio.shield(job.done), cfg.delegate_slot_max_s)
            except asyncio.TimeoutError:
                return DelegationResult("timeout", delivered_by="voice" if job.relay else "hermes")
            if job.relay:
                return DelegationResult("done" if ok else "failed", text=job.result_text, delivered_by="voice")
            return DelegationResult("done" if ok else "failed", delivered_by="hermes")
        except asyncio.CancelledError:
            if not job.done.done():
                job.done.cancel()
            raise
        finally:
            if job in pending:
                pending.remove(job)
            event = event_holder.get("event")
            if event is not None:
                a._realtime_processing_events.pop(id(event), None)
            if not pending:
                a._realtime_pending_jobs.pop(self._guild_id, None)




_ERROR_PREFIX = "Error:"


def _spoken_text(text: Any, limit: int) -> str:
    """Reply text as it would be read aloud: markdown stripped when the TTS normalizer is available,
    capped to ``limit + 1`` characters so the session can tell it was clipped (it enforces the cap)."""
    raw = text if isinstance(text, str) else ""
    try:
        from tools.tts_text_normalize import _strip_markdown_for_tts
        raw = _strip_markdown_for_tts(raw) or ""
    except Exception:  # health: allow BLE001 -- the normalizer is optional polish; raw text is still speakable
        logger.debug("Realtime result normalization unavailable", exc_info=True)
    return raw.strip()[: max(limit, 0) + 1]


class DiscordRealtimeVoiceMixin:
    _voice_clients: Dict[int, Any]
    _voice_receivers: Dict[int, Any]
    _voice_mixers: Dict[int, Any]
    _voice_streams: Dict[int, Any]
    _voice_fx_cfg: Dict[str, Any]
    _client: Any
    _is_allowed_user: Callable[..., bool]
    _voice_frame_sink: Callable[[int], Callable[..., None]]
    _realtime_pending_jobs: Dict[int, List[_PendingJob]]
    _realtime_processing_events: Dict[int, int]

    def _init_realtime_voice(self) -> None:
        self._realtime_cfg = self._load_realtime_voice_config()
        control_cfg = self._load_realtime_voice_control_config()
        self._realtime_control = RealtimeVoiceControl(
            self, control_cfg,
            daily_audio_budget_s=min(control_cfg.daily_audio_budget_s, self._realtime_cfg.daily_audio_budget_s))
        self._realtime_sessions: Dict[int, RealtimeVoiceSession] = {}
        self._realtime_starting: set = set()
        self._realtime_playback: Dict[int, DiscordRealtimePlayback] = {}
        self._realtime_pending_jobs: Dict[int, List[_PendingJob]] = {}
        self._realtime_processing_events: Dict[int, int] = {}
        self._realtime_hermes_playing: Dict[int, int] = {}   # guild_id -> Hermes replies holding the floor

    def _load_realtime_voice_config(self) -> RealtimeVoiceConfig:
        try:
            from hermes_cli.config import load_config
            discord_config = load_config().get("discord") or {}
        except Exception as e:
            logger.debug("Could not load discord.realtime_voice config: %s", e, exc_info=True)
            discord_config = {}
        return RealtimeVoiceConfig.from_mapping(discord_config.get("realtime_voice"))

    def _load_realtime_voice_control_config(self) -> RealtimeVoiceControlConfig:
        try:
            from hermes_cli.config import load_config
            return RealtimeVoiceControlConfig.from_mapping(
                (load_config().get("discord") or {}).get("realtime_voice_control"))
        except Exception as e:
            logger.debug("Could not load discord.realtime_voice_control config: %s", e, exc_info=True)
            return RealtimeVoiceControlConfig()

    def realtime_voice_active(self, guild_id: int) -> bool:
        session = (getattr(self, "_realtime_sessions", None) or {}).get(guild_id)
        return session is not None and not session.closed

    def _realtime_cfg_still_enabled(self) -> bool:
        try:
            from hermes_cli.config import load_config
            raw = (load_config().get("discord") or {}).get("realtime_voice") or {}
        except Exception as e:
            logger.debug("Could not recheck Realtime voice feature config: %s", e, exc_info=True)
            return False
        return isinstance(raw, Mapping) and raw.get("enabled") is True

    def _realtime_start_target(self, guild_id: int) -> Optional[StartTarget]:
        """The channel and limits a start would use right now, or None when there is nothing to join."""
        control, cfg = self._realtime_control, self._realtime_cfg
        vc = self._voice_clients.get(guild_id)
        channel_id = getattr(getattr(vc, "channel", None), "id", None)
        receiver = self._voice_receivers.get(guild_id)
        try:
            connected = vc is not None and vc.is_connected()
        except Exception:
            connected = False
        if (not connected or type(channel_id) is not int
                or receiver is None or not getattr(receiver, "_running", False)):
            return None
        return StartTarget(guild_id, channel_id, cfg.model,
                           min(control.config.max_session_s, cfg.max_session_s), control.budget.limit_s)

    def request_realtime_voice_start(self, guild_id: int, requester_id: int) -> str:
        """Legacy helper: direct invocation now starts; do not issue confirmation tokens."""
        if not self._realtime_control.authorized(guild_id, requester_id):
            return "Realtime canary control is not authorized here."
        if not self._realtime_cfg.enabled:
            return "Realtime voice is disabled in config."
        if self.realtime_voice_active(guild_id):
            return "Realtime voice is already active."
        if self._realtime_start_target(guild_id) is None:
            return "Hermes is not connected to a voice channel. Run /voice join while you are in one."
        return "Use /realtime-voice start to start."

    async def start_realtime_voice(self, guild_id: int, *, requester_id: int) -> StartResult:
        """Open a session only on the user's explicit ``/realtime-voice start`` invocation.
        The one-command action is itself the confirmation; validate exact canary authorization,
        joined voice transport and daily/session caps before opening any provider connection."""
        control, cfg = self._realtime_control, self._realtime_cfg
        if not cfg.enabled:
            return StartResult(False, "Realtime voice is disabled in config.")
        if not control.authorized(guild_id, requester_id):
            return StartResult(False, "Realtime canary control is not authorized here.")
        if self.realtime_voice_active(guild_id) or guild_id in self._realtime_starting:
            return StartResult(False, "Realtime voice is already active.")
        target = self._realtime_start_target(guild_id)
        if target is None:
            return StartResult(False, "Hermes is not connected to a voice channel. Run /voice join while you are in one.")
        if control.budget.exhausted:
            return StartResult(False, "Realtime canary daily audio budget is exhausted.")
        self._realtime_starting.add(guild_id)
        try:
            return await self._open_realtime_session(guild_id, target, requester_id)
        finally:
            self._realtime_starting.discard(guild_id)

    async def _open_realtime_session(self, guild_id: int, target: StartTarget, requester_id: int) -> StartResult:
        control = self._realtime_control
        session_cfg = dataclasses.replace(
            self._realtime_cfg, max_session_s=target.max_session_s,
            daily_audio_budget_s=target.daily_audio_budget_s)
        vc = self._voice_clients[guild_id]
        receiver = self._voice_receivers[guild_id]
        guild = self._client.get_guild(guild_id) if self._client is not None else None
        playback = DiscordRealtimePlayback(self, guild_id)
        transport_cls = OpenAIRealtimeTransport
        from agent.secret_scope import (
            build_profile_secret_scope, get_secret, is_multiplex_active,
            reset_secret_scope, set_secret_scope,
        )
        from hermes_constants import get_hermes_home

        api_key_env = session_cfg.api_key_env
        injected_transport = getattr(self, "_realtime_transport_factory", None)
        if injected_transport is not None:
            transport = injected_transport(session_cfg)
        elif transport_cls is not _REALTIME_TRANSPORT_CLASS:
            transport = transport_cls(model=session_cfg.model, api_key_env=api_key_env)
        else:
            if is_multiplex_active():
                profile_home = Path(get_hermes_home())
                secret_token = set_secret_scope(
                    build_profile_secret_scope(profile_home), profile_home=str(profile_home))
            else:
                secret_token = None
            try:
                api_key = get_secret(api_key_env, "") or ""
            finally:
                if secret_token is not None:
                    reset_secret_scope(secret_token)
            if not api_key:
                raise RealtimeUnavailable(f"{api_key_env} is not configured")
            transport = transport_cls(model=session_cfg.model, api_key=api_key)
        def is_allowed(uid: int) -> bool:
            if not self._realtime_control.authorized(guild_id, int(uid)):
                return False
            return self._is_allowed_user(str(uid), guild=guild, is_dm=False) is True

        session = RealtimeVoiceSession(
            session_cfg, connect=transport.connect, playback=playback,
            delegate=GatewayVoiceDelegate(self, guild_id),
            allowed_speakers=control.config.user_ids,
            # Both gates must agree; the control allowlist cannot be bypassed via gateway auth.
            is_allowed=is_allowed,
            control_authorized=lambda gid, uid=requester_id: (
                gid == guild_id and self._realtime_control.authorized(gid, uid)
            ),
            guild_id=guild_id, bot_user_id=int(getattr(getattr(vc, "user", None), "id", 0) or 0),
            daily_budget=control.budget,
            on_closed=lambda reason, gid=guild_id: self._realtime_session_closed(gid),
        )
        try:
            await session.start()
        except RealtimeUnavailable as e:
            logger.warning("Realtime voice unavailable (guild=%s): %s", guild_id, e)
            return StartResult(False, "Realtime voice failed to start: the provider is unavailable "
                                      "(is the API key configured?).")
        except Exception as e:  # health: allow BLE001 -- provider/websocket errors are arbitrary; only the type is logged so nothing credential-bearing reaches the log
            logger.warning("Realtime voice failed to start (guild=%s): %s", guild_id, type(e).__name__)
            return StartResult(False, "Realtime voice failed to start.")
        self._realtime_sessions[guild_id] = session
        self._realtime_playback[guild_id] = playback
        barge_sink = self._voice_frame_sink(guild_id)

        def sink(user_id: int, pcm: bytes, now: Optional[float] = None) -> None:
            if session.closed:
                return
            barge_sink(user_id, pcm, now)        # Hermes's streamed replies stay interruptible
            session.feed_frame(user_id, pcm, now)    # gates every speaker before queueing

        receiver.frame_sink = sink
        receiver.discard_pending()   # audio from before realtime started is not a turn for it
        receiver.resume()            # full duplex: listening never pauses while realtime is on
        return StartResult(True, f"Realtime canary started ({target.max_session_s / 60:.0f}-minute session cap; "
                                 f"{target.daily_audio_budget_s / 60:.0f} minutes of daily audio). The realtime "
                                 "voice converses directly; memory, tools and anything else go to your regular "
                                 "Hermes session when it asks Hermes, and Hermes answers in the text channel.")

    async def stop_realtime_voice(self, guild_id: int, reason: str = "stopped") -> None:
        session = (getattr(self, "_realtime_sessions", None) or {}).get(guild_id)
        if session is not None:
            await session.close(reason)

    async def handle_realtime_voice_control(self, interaction: Any, action: str) -> str:
        """Adapter seam for explicit Discord control; start occurs only on its slash action."""
        guild = getattr(interaction, "guild", None)
        user = getattr(interaction, "user", None)
        if guild is None or user is None:
            return "Realtime canary control is not authorized here."
        try:
            return await self._realtime_control.execute(
                guild_id=int(guild.id), user_id=int(user.id), action=action)
        except Exception:
            logger.exception("Realtime voice control failed")
            return "Realtime voice control failed."

    def _register_realtime_voice_slash(self, tree) -> None:
        """``/realtime-voice``: start opens the explicitly requested private canary session directly;
        the invoking allowlisted user's slash action is the confirmation. This only exists when enabled."""
        if not self._realtime_cfg.enabled:
            return
        import discord

        @tree.command(name="realtime-voice", description="Start or control private realtime voice test")
        @discord.app_commands.describe(action="start, stop, or check status")
        @discord.app_commands.choices(action=[
            discord.app_commands.Choice(name="start", value="start"),
            discord.app_commands.Choice(name="stop", value="stop"),
            discord.app_commands.Choice(name="status", value="status"),
        ])
        async def slash_realtime_voice(interaction: discord.Interaction, action: str):
            guild_id = getattr(getattr(interaction, "guild", None), "id", None)
            user_id = getattr(getattr(interaction, "user", None), "id", None)
            if (type(guild_id) is not int or type(user_id) is not int
                    or not self._realtime_control.authorized(guild_id, user_id)):
                await interaction.response.send_message(
                    "Realtime canary control is not authorized here.", ephemeral=True)
                return
            if not await self._check_slash_authorization(interaction, "/realtime-voice"):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self.handle_realtime_voice_control(interaction, action)
            await interaction.edit_original_response(content=result)

    def _realtime_session_closed(self, guild_id: int) -> None:
        self._realtime_sessions.pop(guild_id, None)
        self._realtime_playback.pop(guild_id, None)
        receiver = self._voice_receivers.get(guild_id)
        if receiver is not None:
            receiver.frame_sink = self._voice_frame_sink(guild_id)
        for job in self._realtime_pending_jobs.pop(guild_id, []):
            for future in (job.started, job.done):
                if not future.done():
                    future.cancel()
        for event_id, event_guild in list(self._realtime_processing_events.items()):
            if event_guild == guild_id:
                self._realtime_processing_events.pop(event_id, None)


    async def _realtime_hermes_floor(self, guild_id: int, speaking: bool) -> None:
        """A Hermes reply starts (True) or stops (False) speaking in *guild_id*. Calls must pair:
        realtime stays silent until every Hermes reply that took the floor has given it back."""
        if speaking:
            await self._realtime_take_floor(guild_id)
        else:
            self._realtime_return_floor(guild_id)

    async def _realtime_take_floor(self, guild_id: int) -> None:
        # Held before the first await, so a model response arriving meanwhile cannot claim the player.
        playing = getattr(self, "_realtime_hermes_playing", None)
        if playing is None:   # no realtime state at all (adapter built without __init__): nothing to yield
            return
        playing[guild_id] = playing.get(guild_id, 0) + 1
        playback = self._realtime_playback.get(guild_id)
        if playback is not None:
            playback.yield_floor("hermes-speaking")
        session = self._realtime_sessions.get(guild_id)
        if session is not None:
            await session.yield_floor("hermes-speaking")

    def _realtime_return_floor(self, guild_id: int) -> None:
        playing = getattr(self, "_realtime_hermes_playing", None)
        if not playing or guild_id not in playing:
            return
        playing[guild_id] -= 1
        if playing[guild_id] > 0:
            return
        del playing[guild_id]
        playback = self._realtime_playback.get(guild_id)
        if playback is not None:
            playback.release_direct_player()
            playback.current = None
            playback.resume_floor()
        session = self._realtime_sessions.get(guild_id)
        if session is not None:
            session.resume_floor()

    def _realtime_pending_job(self, event: Any) -> Optional[_PendingJob]:
        guild_id = self._realtime_event_guild_id(event)
        text = str(getattr(event, "text", None) or "")
        user_id = getattr(event, "user_id", None)
        if user_id is None:
            user_id = getattr(getattr(event, "source", None), "user_id", None)
        if user_id is None:
            user_id = getattr(getattr(getattr(event, "raw_message", None), "author", None), "id", None)
        jobs = self._realtime_pending_jobs.get(guild_id, [])
        key = str((getattr(event, "metadata", None) or {}).get("realtime_delegate_event_key") or "")
        if key:
            # A keyed event belongs to exactly one job or to none; it never falls back to text.
            exact = [job for job in jobs if job.event_key == key and not job.done.done()]
            return exact[0] if len(exact) == 1 else None
        # Keyless events only (voice-channel events are always keyed by the gateway).
        matches = [job for job in jobs if job.event_key is None and job.text == text
                   and str(job.user_id) == str(user_id) and not job.done.done()]
        return matches[0] if len(matches) == 1 else None

    def _realtime_relays_event(self, event: Any) -> bool:
        """True when this event is a Realtime-delegated request whose answer the live Realtime session
        will speak: Hermes's own voice (streaming TTS, ``_send_voice_reply``, base auto-TTS) must stay
        quiet for that turn only. Keyed by the event's correlation token, never by text, so unrelated
        voice or typed turns keep Hermes's voice. A session that has closed relays nothing, so Hermes
        falls back to speaking for itself."""
        guild_id = self._realtime_event_guild_id(event)
        if not guild_id or not self.realtime_voice_active(guild_id):
            return False
        if not (getattr(event, "metadata", None) or {}).get("realtime_delegate_event_key"):
            return False
        job = self._realtime_pending_job(event)
        if job is None:
            return False
        job.relay = True
        return True

    def _realtime_capture_result(self, event: Any, text: str) -> bool:
        """Final reply text of a relayed turn, kept (spoken form, bounded) for the voice layer.
        Returns True when the event is relayed; the caller then skips Hermes's own voice."""
        if not self._realtime_relays_event(event):
            return False
        job = self._realtime_pending_job(event)
        if job is not None:
            job.result_text = _spoken_text(text, self._realtime_cfg.result_max_chars)
            event._realtime_final_response = job.result_text
        return True

    def _wants_auto_tts(self, event: Any, session_key: str, interrupt_event: Any,
                        text_content: str, media_files: list) -> bool:
        """Base auto-TTS, except for a turn the live session claimed: Realtime speaks that answer. The
        claim is taken last, so it only happens when Hermes would otherwise speak."""
        return (super()._wants_auto_tts(event, session_key, interrupt_event, text_content, media_files)
                and not self._realtime_relays_event(event))

    def _realtime_event_guild_id(self, event: Any) -> int:
        raw = getattr(event, "raw_message", None)
        return int(getattr(raw, "guild_id", 0) or 0)

    def _realtime_note_processing_start(self, event: Any) -> None:
        """Hermes admitted the VOICE event carrying a delegated request."""
        job = self._realtime_pending_job(event)
        if job is not None and not job.started.done():
            job.started.set_result(True)

    def _realtime_note_processing_complete(self, event: Any, success: bool) -> None:
        """Resolve the delegated job whose spoken request this VOICE event carried."""
        if getattr(getattr(event, "message_type", None), "value", None) not in ("voice", "text"):
            return
        job = self._realtime_pending_job(event)
        if job is None:
            return
        if not job.started.done():
            job.started.set_result(True)
        if success and not job.result_text:
            job.result_text = _spoken_text(getattr(event, "_realtime_final_response", ""),
                                           self._realtime_cfg.result_max_chars)
            job.relay = bool(job.result_text)
        job.done.set_result(bool(success))
