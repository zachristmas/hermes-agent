"""DiscordAdapter streaming-TTS contract (adapter_voice_stream.py) against fake voice clients.

Covers opt-in and whole-file fallback, playback ownership (never stopping audio the adapter did not
start; joining the VoiceMixer instead of replacing it), streaming before the reply is complete,
bounded buffering with backpressure and stall detection, turn fencing on supersede / barge-in /
leave, and allowlisted barge-in from decoded receiver frames.
"""

import array
import asyncio
import sys
import time

import pytest

from discord_voice_fakes import ALLOWED_USER, GUILD, STRANGER, TEXT_CHANNEL, FakeVoiceClient

from gateway.platforms.base import AudioFormat
from plugins.platforms.discord.voice_stream import FRAME_BYTES, SILENCE_FRAME

FMT = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
CHAT = str(TEXT_CHANNEL)


def _level_pcm(level, ms):
    """24 kHz mono PCM held at a constant *level*: each frame's samples identify its turn."""
    arr = array.array("h", [level] * (24 * ms))
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def _level(frame):
    arr = array.array("h", frame[:4])
    if sys.byteorder == "big":
        arr.byteswap()
    return arr[0]


def _levels(vc):
    return [_level(f) for f in list(vc.sent) if f != SILENCE_FRAME]


def _speech_frame(amplitude=8000):
    import math
    arr = array.array("h")
    for i in range(960):
        v = int(amplitude * math.sin(2 * math.pi * 300 * i / 48000))
        arr.extend((v, v))
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def _talk(adapter, user_id, frames, *, interval=0.02):
    """Feed decoded frames through the receiver sink on another thread, like the socket reader."""
    sink = adapter._voice_frame_sink(GUILD)
    loop = asyncio.get_running_loop()
    start = time.monotonic()

    def _feed():
        for i in range(frames):
            sink(user_id, _speech_frame(), start + i * interval)

    await loop.run_in_executor(None, _feed)
    await asyncio.sleep(0.05)  # let call_soon_threadsafe callbacks run


class TestOptInAndFallback:
    @pytest.mark.asyncio
    async def test_disabled_by_default_declines(self, make_adapter):
        adapter, vc = make_adapter({"enabled": False})
        assert adapter.supports_streaming_tts(CHAT, FMT) is False
        assert await adapter.begin_streaming_tts(CHAT, FMT) is None
        assert vc.plays == 0

    @pytest.mark.asyncio
    async def test_chat_not_bound_to_a_voice_channel_declines(self, make_adapter):
        adapter, vc = make_adapter()
        assert adapter.supports_streaming_tts("333", FMT) is False
        assert await adapter.begin_streaming_tts("333", FMT) is None

    @pytest.mark.asyncio
    async def test_disconnected_voice_client_declines(self, make_adapter):
        adapter, vc = make_adapter()
        vc.connected = False
        assert adapter.supports_streaming_tts(CHAT, FMT) is False
        assert await adapter.begin_streaming_tts(CHAT, FMT) is None

    @pytest.mark.asyncio
    async def test_unsupported_format_declines(self, make_adapter):
        adapter, vc = make_adapter()
        assert await adapter.begin_streaming_tts(CHAT, AudioFormat(24000, 1, 1)) is None
        assert vc.plays == 0

    @pytest.mark.asyncio
    async def test_foreign_playback_is_never_stopped(self, make_adapter):
        import discord
        adapter, vc = make_adapter()

        class _Clip(discord.AudioSource):
            def read(self):
                return SILENCE_FRAME

        vc.play(_Clip())
        assert await adapter.begin_streaming_tts(CHAT, FMT) is None  # whole-file path queues behind it
        assert vc.is_playing() and vc.plays == 1


class TestStreamingPlayback:
    @pytest.mark.asyncio
    async def test_audio_plays_before_the_reply_is_finished(self, make_adapter):
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        assert handle is not None
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 200))
        await _until(lambda: 1000 in _levels(vc))  # audible while the turn is still open
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 200))
        await adapter.finish_streaming_tts(handle)
        assert vc.wait_idle()
        assert _levels(vc).count(1000) == 20  # 400 ms, in order, nothing lost or replayed
        assert all(len(f) == FRAME_BYTES for f in vc.sent)
        assert GUILD not in adapter._voice_streams

    @pytest.mark.asyncio
    async def test_lead_silence_precedes_speech(self, make_adapter):
        adapter, vc = make_adapter(voice_fx={"lead_silence_ms": 100})
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 40))
        await adapter.finish_streaming_tts(handle)
        vc.wait_idle()
        first_speech = next(i for i, f in enumerate(vc.sent) if f != SILENCE_FRAME)
        assert first_speech >= 5 and _level(vc.sent[first_speech]) == 1000

    @pytest.mark.asyncio
    async def test_backpressure_bounds_the_queue(self, make_adapter):
        adapter, vc = make_adapter({"buffer_ms": 100}, vc=FakeVoiceClient(frame_interval=0.02))
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        writer = asyncio.ensure_future(adapter.write_streaming_tts(handle, _level_pcm(1000, 1000)))
        await asyncio.sleep(0.2)
        assert not writer.done()  # 50 frames cannot fit a 5-frame buffer at real-time drain
        assert handle.queue.buffered <= 5
        await writer
        await adapter.finish_streaming_tts(handle)
        assert _levels(vc).count(1000) == 50

    @pytest.mark.asyncio
    async def test_stalled_player_raises_and_releases(self, make_adapter):
        from plugins.platforms.discord.adapter_voice_stream import VoiceStreamStalled
        adapter, vc = make_adapter({"buffer_ms": 100, "stall_timeout_seconds": 0.5})
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await _until(vc.is_playing)
        vc.stall()
        with pytest.raises(VoiceStreamStalled):
            await adapter.write_streaming_tts(handle, _level_pcm(1000, 1000))
        assert handle.cut_reason == "stalled" and GUILD not in adapter._voice_streams
        await adapter.abort_streaming_tts(handle, error="stalled")  # consumer's follow-up abort is a no-op

    @pytest.mark.asyncio
    async def test_disconnect_mid_stream_raises(self, make_adapter):
        from plugins.platforms.discord.adapter_voice_stream import VoiceStreamStalled
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        vc.connected = False
        with pytest.raises(VoiceStreamStalled):
            await adapter.write_streaming_tts(handle, _level_pcm(1000, 100))
        assert handle.cut_reason == "disconnected"

    @pytest.mark.asyncio
    async def test_player_error_surfaces_on_next_write(self, make_adapter):
        from plugins.platforms.discord.adapter_voice_stream import VoiceStreamStalled
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        handle.source.read = lambda: (_ for _ in ()).throw(OSError("socket closed"))
        await _until(lambda: not vc.is_playing())
        with pytest.raises(VoiceStreamStalled):
            await adapter.write_streaming_tts(handle, _level_pcm(1000, 100))

    @pytest.mark.asyncio
    async def test_abort_is_idempotent_and_late_writes_are_dropped(self, make_adapter):
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 100))
        await adapter.abort_streaming_tts(handle, error="cancelled")
        await adapter.abort_streaming_tts(handle, error="cancelled")
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 100))
        await adapter.finish_streaming_tts(handle)
        assert handle.aborted and vc.wait_idle()
        assert GUILD not in adapter._voice_streams

    @pytest.mark.asyncio
    async def test_new_turn_supersedes_and_old_frames_never_play_after_it(self, make_adapter):
        adapter, vc = make_adapter(vc=FakeVoiceClient(frame_interval=0.01))
        first = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(first, _level_pcm(1000, 400))
        second = await adapter.begin_streaming_tts(CHAT, FMT)
        assert first.cut_reason == "superseded" and first.aborted
        await adapter.write_streaming_tts(first, _level_pcm(1000, 200))  # late, from the old turn
        await adapter.write_streaming_tts(second, _level_pcm(2000, 100))
        await adapter.finish_streaming_tts(second)
        vc.wait_idle()
        levels = _levels(vc)
        assert levels.count(2000) == 5
        assert 1000 not in levels[levels.index(2000):]

    @pytest.mark.asyncio
    async def test_leave_tears_down_an_active_stream(self, make_adapter):
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 200))
        await adapter.leave_voice_channel(GUILD)
        await adapter.leave_voice_channel(GUILD)  # repeated shutdown is harmless
        assert handle.cut_reason == "left"
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 100))  # no-op after teardown
        await adapter.finish_streaming_tts(handle)
        assert GUILD not in adapter._voice_streams

    @pytest.mark.asyncio
    async def test_declines_stale_mixer_without_replacing_or_stopping_current_source(self, make_adapter):
        adapter, vc = make_adapter()
        adapter._voice_mixers[GUILD] = object()

        import discord

        class _Clip(discord.AudioSource):
            def __init__(self):
                self.cleaned = False

            def read(self):
                return SILENCE_FRAME

            def cleanup(self):
                self.cleaned = True

        clip = _Clip()
        vc.play(clip)
        assert await adapter.begin_streaming_tts(CHAT, FMT) is None
        assert vc.source is clip and vc.is_playing() and vc.plays == 1 and not clip.cleaned


    @pytest.mark.asyncio
    async def test_declines_stale_mixer_even_when_the_player_is_idle(self, make_adapter):
        adapter, vc = make_adapter()
        adapter._voice_mixers[GUILD] = object()   # registered but never installed on the player
        assert await adapter.begin_streaming_tts(CHAT, FMT) is None
        assert vc.plays == 0 and not vc.is_playing()


class TestMixerOwnership:
    @pytest.mark.asyncio
    async def test_streams_through_the_installed_mixer_without_replacing_it(self, make_adapter):
        pytest.importorskip("numpy")
        from plugins.platforms.discord.voice_mixer import VoiceMixer
        adapter, vc = make_adapter(voice_fx={"speech_gain": 1.0, "lead_silence_ms": 0})
        mixer = VoiceMixer(speech_gain=1.0)
        vc.play(mixer)
        adapter._voice_mixers[GUILD] = mixer
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        assert handle is not None and vc.plays == 1 and vc.source is mixer
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 200))
        await _until(lambda: mixer.speech_active and 1000 in _levels(vc))
        await adapter.finish_streaming_tts(handle)
        await _until(lambda: not mixer.speech_active)
        assert vc.is_playing() and vc.source is mixer  # the continuous mixer keeps running
        assert _levels(vc).count(1000) == 10

    @pytest.mark.asyncio
    async def test_barge_in_cuts_only_the_stream_not_the_mixer(self, make_adapter):
        pytest.importorskip("numpy")
        from plugins.platforms.discord.voice_mixer import VoiceMixer
        adapter, vc = make_adapter(vc=FakeVoiceClient(frame_interval=0.01))
        mixer = VoiceMixer()
        vc.play(mixer)
        adapter._voice_mixers[GUILD] = mixer
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 500))
        await _talk(adapter, ALLOWED_USER, frames=20)
        assert handle.cut_reason == "barge-in"
        await _until(lambda: not mixer.speech_active)
        assert vc.source is mixer and vc.is_playing()


class TestBargeIn:
    @pytest.mark.asyncio
    async def test_sustained_allowed_speech_cuts_the_turn(self, make_adapter):
        adapter, vc = make_adapter(vc=FakeVoiceClient(frame_interval=0.01))
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 1000))
        await _talk(adapter, ALLOWED_USER, frames=20)
        assert handle.cut_reason == "barge-in" and handle.aborted
        played = _levels(vc).count(1000)
        assert vc.wait_idle()
        assert played < 50 and _levels(vc).count(1000) == played
        # The consumer keeps calling the contract for the cut turn: silent no-ops.
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 100))
        await adapter.finish_streaming_tts(handle)
        assert _levels(vc).count(1000) == played

    @pytest.mark.asyncio
    async def test_capture_rearms_for_the_next_turn(self, make_adapter):
        adapter, vc = make_adapter(vc=FakeVoiceClient(frame_interval=0.01))
        for level in (1000, 2000):
            handle = await adapter.begin_streaming_tts(CHAT, FMT)
            await adapter.write_streaming_tts(handle, _level_pcm(level, 600))
            await _talk(adapter, ALLOWED_USER, frames=20)
            assert handle.cut_reason == "barge-in"
        third = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(third, _level_pcm(3000, 100))
        await adapter.finish_streaming_tts(third)
        vc.wait_idle()
        assert _levels(vc).count(3000) == 5

    @pytest.mark.asyncio
    @pytest.mark.parametrize("speaker", [STRANGER, 0, 1])  # not allowlisted, unmapped SSRC, the bot
    async def test_only_allowlisted_speakers_can_interrupt(self, make_adapter, speaker):
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 200))
        await _talk(adapter, speaker, frames=30)
        assert handle.cut_reason == ""
        await adapter.finish_streaming_tts(handle)

    @pytest.mark.asyncio
    async def test_role_allowlist_is_enforced_on_the_loop(self, make_adapter):
        adapter, vc = make_adapter()
        adapter._allowed_user_ids = set()  # role-only allowlist: the guild check decides
        calls = []

        def _is_allowed(user_id, author=None, *, guild=None, is_dm=False, channel_ids=None):
            calls.append((user_id, is_dm))
            return False

        adapter._is_allowed_user = _is_allowed
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 200))
        await _talk(adapter, ALLOWED_USER, frames=30)
        assert handle.cut_reason == "" and calls == [(str(ALLOWED_USER), False)]
        await adapter.finish_streaming_tts(handle)

    @pytest.mark.asyncio
    async def test_brief_interjection_does_not_interrupt(self, make_adapter):
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 200))
        await _talk(adapter, ALLOWED_USER, frames=5)  # 100 ms "mm-hm"
        assert handle.cut_reason == ""
        await adapter.finish_streaming_tts(handle)

    @pytest.mark.asyncio
    async def test_speech_with_no_active_turn_is_ignored(self, make_adapter):
        adapter, vc = make_adapter()
        await _talk(adapter, ALLOWED_USER, frames=30)
        assert vc.plays == 0

    @pytest.mark.asyncio
    async def test_barge_in_disabled_keeps_legacy_echo_guard(self, make_adapter):
        from unittest.mock import MagicMock
        adapter, vc = make_adapter({"barge_in": False})
        receiver = MagicMock()
        adapter._voice_receivers[GUILD] = receiver
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        receiver.pause.assert_called_once()
        await adapter.write_streaming_tts(handle, _level_pcm(1000, 200))
        await _talk(adapter, ALLOWED_USER, frames=30)
        assert handle.cut_reason == ""
        await adapter.finish_streaming_tts(handle)
        receiver.resume.assert_called_once()


def test_config_yaml_reaches_the_adapter(tmp_path, monkeypatch):
    """discord.voice_streaming in a real config.yaml, read through the adapter's own loader."""
    import os
    from pathlib import Path

    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "discord:\n  voice_streaming:\n    enabled: true\n    buffer_ms: 400\n    barge_in_ms: 500\n"
    )
    cfg = DiscordAdapter(PlatformConfig(enabled=True, token="x"))._voice_stream_cfg
    assert (cfg.enabled, cfg.buffer_frames, cfg.barge_in_ms, cfg.barge_in) == (True, 20, 500, True)


def test_voice_config_loaders_have_a_single_owner():
    """Config loaders move to the mixin while timeout limit helpers remain on adapter."""
    from plugins.platforms.discord.adapter import DiscordAdapter
    from plugins.platforms.discord.adapter_voice_config import DiscordVoiceConfigMixin

    for name in ("_load_voice_fx_config", "_load_discord_int_config", "_load_voice_timeout", "_load_playback_timeout"):
        assert name in vars(DiscordVoiceConfigMixin), name
        assert name not in vars(DiscordAdapter), name
    for name in ("_voice_timeout_limit", "_playback_timeout_limit"):
        assert name in vars(DiscordAdapter), name
