"""Who owns the voice player, and what happens to a streamed turn when someone else ends it.

Review findings 1-3 for the streaming-TTS path: (1) a turn only starts on an idle VoiceClient or
through the installed mixer, and never replaces a source it does not own; (2) the whole-file
timeout in ``play_in_voice_channel`` stops its own clip, never a streamed turn sharing the mixer;
(3) when the player, the mixer or a cleanup cuts a turn from outside, the handle is aborted and
released on the event loop so synthesis stops and the receiver / inactivity timer are restored.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from discord_voice_fakes import GUILD, TEXT_CHANNEL, FakeVoiceClient

from gateway.platforms.base import AudioFormat
from plugins.platforms.discord.voice_stream import FRAME_BYTES, SILENCE_FRAME

FMT = AudioFormat(sample_rate=24000, channels=1, sample_width=2)
CHAT = str(TEXT_CHANNEL)


def _pcm(ms=100):
    return b"\x01\x00" * (24 * ms)


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def _clip():
    import discord

    class _Clip(discord.AudioSource):
        """A whole-file source someone else started."""

        def __init__(self):
            self.cleaned = False

        def read(self):
            return SILENCE_FRAME

        def cleanup(self):
            self.cleaned = True

    return _Clip()


def _mixer_on(adapter, vc):
    pytest.importorskip("numpy")
    from plugins.platforms.discord.voice_mixer import VoiceMixer
    mixer = VoiceMixer()
    vc.play(mixer)
    adapter._voice_mixers[GUILD] = mixer
    return mixer


class TestFinding1NeverReplaceASourceWeDoNotOwn:
    @pytest.mark.asyncio
    async def test_paused_whole_file_source_is_not_replaced(self, make_adapter):
        adapter, vc = make_adapter()
        clip = _clip()
        vc.play(clip)
        vc.pause()  # not playing, but still somebody's live audio
        assert vc.is_playing() is False and vc.source is clip
        assert await adapter.begin_streaming_tts(CHAT, FMT) is None
        assert vc.source is clip and vc.plays == 1 and not clip.cleaned
        assert GUILD not in adapter._voice_streams

    @pytest.mark.asyncio
    async def test_paused_clip_survives_even_with_a_mixer_installed(self, make_adapter):
        adapter, vc = make_adapter()
        _mixer_on(adapter, vc)  # a mixer is registered, but it is not what the client is playing
        vc.stop()
        clip = _clip()
        vc.play(clip)
        vc.pause()
        assert await adapter.begin_streaming_tts(CHAT, FMT) is None
        assert vc.source is clip and vc.plays == 2 and not clip.cleaned

    @pytest.mark.asyncio
    async def test_declined_turn_leaves_no_stream_or_paused_receiver(self, make_adapter):
        adapter, vc = make_adapter({"barge_in": False})
        receiver = MagicMock()
        adapter._voice_receivers[GUILD] = receiver
        vc.play(_clip())
        vc.pause()
        assert await adapter.begin_streaming_tts(CHAT, FMT) is None
        receiver.pause.assert_not_called()
        assert GUILD not in adapter._voice_streams

    @pytest.mark.asyncio
    async def test_a_finished_whole_file_clip_does_not_block_streaming(self, make_adapter):
        """discord.py keeps the ended player on ``vc.source`` until ``stop()``: that is idle."""
        adapter, vc = make_adapter()

        class _Short(type(_clip())):
            def __init__(self):
                super().__init__()
                self.left = 2

            def read(self):
                self.left -= 1
                return SILENCE_FRAME if self.left >= 0 else b""

        vc.play(_Short())
        assert vc.wait_idle() and vc.source is not None
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        assert handle is not None
        await adapter.write_streaming_tts(handle, _pcm())
        await adapter.finish_streaming_tts(handle)
        assert vc.wait_idle()

    @pytest.mark.asyncio
    async def test_late_cut_of_an_old_turn_never_stops_a_clip_that_followed_it(self, make_adapter):
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _pcm())
        await adapter.finish_streaming_tts(handle)
        assert vc.wait_idle()
        clip = _clip()
        vc.play(clip)
        await adapter.abort_streaming_tts(handle)
        assert vc.is_playing() and vc.source is clip and not clip.cleaned


class TestFinding2LegacyTimeoutScopedToItsOwnClip:
    @staticmethod
    def _legacy(adapter, *, timeout, pcm):
        async def _timeout(_path):
            return timeout

        adapter._playback_timeout_for_audio = _timeout
        # Patch the module copy the adapter resolves (flat ``voice_mixer`` when another test file
        # put the plugin dir on sys.path, else the package module), not a fixed import path.
        from plugins.platforms.discord.adapter import _voice_mixer_module
        return patch.object(_voice_mixer_module(), "decode_to_pcm", return_value=pcm)

    @pytest.mark.asyncio
    async def test_returns_when_its_clip_ends_even_though_a_stream_is_still_open(self, make_adapter):
        from plugins.platforms.discord.voice_mixer import FRAME_SIZE
        adapter, vc = make_adapter()
        mixer = _mixer_on(adapter, vc)
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _pcm(200))
        with self._legacy(adapter, timeout=0.5, pcm=b"\x01\x00" * (FRAME_SIZE // 2) * 2):
            assert await asyncio.wait_for(adapter.play_in_voice_channel(GUILD, "clip.mp3"), 3) is True
        assert handle.cut_reason == "" and not handle.aborted
        assert mixer.speech_active  # the stream is still on the mixer

    @pytest.mark.asyncio
    async def test_timeout_stops_the_clip_and_leaves_the_stream_playing(self, make_adapter):
        from plugins.platforms.discord.voice_mixer import FRAME_SIZE
        adapter, vc = make_adapter(vc=FakeVoiceClient(frame_interval=0.01))
        mixer = _mixer_on(adapter, vc)
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _pcm(200))
        long_clip = b"\x01\x00" * (FRAME_SIZE // 2) * 2000  # 40 s: cannot finish inside the timeout
        with self._legacy(adapter, timeout=0.2, pcm=long_clip):
            assert await asyncio.wait_for(adapter.play_in_voice_channel(GUILD, "clip.mp3"), 3) is True
        assert handle.cut_reason == "" and not handle.aborted
        assert mixer.speech_active
        await adapter.write_streaming_tts(handle, _pcm(100))
        await adapter.finish_streaming_tts(handle)
        assert handle.cut_reason == ""

    def test_mixer_stop_speech_can_be_scoped_to_one_clip(self):
        pytest.importorskip("numpy")
        from plugins.platforms.discord.voice_mixer import FRAME_SIZE, VoiceMixer
        from plugins.platforms.discord.voice_stream import FrameQueue
        mixer = VoiceMixer()
        queue = FrameQueue(10)
        mixer.play_speech_stream(queue)
        clip = mixer.play_speech(b"\x01\x00" * FRAME_SIZE)
        assert mixer.speech_playing(clip)
        mixer.stop_speech(clip)
        assert not mixer.speech_playing(clip)
        assert queue.cut_reason == "" and mixer.speech_active  # stream untouched, bed still ducked
        mixer.stop_speech()
        assert queue.cut_reason == "player-stopped" and not mixer.speech_active


class TestFinding3OutsideCutsReleaseTheTurn:
    @staticmethod
    def _released(adapter, handle):
        return handle.aborted and handle.released and GUILD not in adapter._voice_streams

    @pytest.mark.asyncio
    async def test_direct_path_external_player_stop_aborts_and_releases(self, make_adapter):
        adapter, vc = make_adapter({"barge_in": False})
        receiver = MagicMock()
        adapter._voice_receivers[GUILD] = receiver
        adapter._reset_voice_timeout = MagicMock()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _pcm(200))
        receiver.pause.assert_called_once()
        vc.stop()  # someone else's /stop, a reconnect, a disconnect handler...
        await _until(lambda: self._released(adapter, handle))
        receiver.resume.assert_called_once()
        adapter._reset_voice_timeout.assert_called_once_with(GUILD)
        assert handle.cut_reason == "player-stopped"
        await adapter.write_streaming_tts(handle, _pcm(100))  # the writer's next call is a no-op
        await adapter.finish_streaming_tts(handle)
        receiver.resume.assert_called_once()

    @pytest.mark.asyncio
    async def test_direct_path_external_stop_does_not_touch_a_clip_that_replaced_it(self, make_adapter):
        adapter, vc = make_adapter()
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        vc.stop()
        clip = _clip()
        vc.play(clip)
        await _until(lambda: self._released(adapter, handle))
        assert vc.is_playing() and vc.source is clip and not clip.cleaned

    @pytest.mark.asyncio
    async def test_mixer_stop_speech_aborts_and_releases_the_turn(self, make_adapter):
        adapter, vc = make_adapter()
        adapter._reset_voice_timeout = MagicMock()
        mixer = _mixer_on(adapter, vc)
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        await adapter.write_streaming_tts(handle, _pcm(200))
        mixer.stop_speech()
        await _until(lambda: self._released(adapter, handle))
        adapter._reset_voice_timeout.assert_called_once_with(GUILD)
        assert vc.source is mixer and vc.is_playing()

    @pytest.mark.asyncio
    async def test_mixer_cleanup_aborts_and_releases_the_turn(self, make_adapter):
        adapter, vc = make_adapter()
        mixer = _mixer_on(adapter, vc)
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        mixer.cleanup()  # discord.py cleans the source up when the player ends
        await _until(lambda: self._released(adapter, handle))

    @pytest.mark.asyncio
    async def test_cut_from_a_foreign_thread_is_released_on_the_event_loop(self, make_adapter):
        adapter, vc = make_adapter()
        mixer = _mixer_on(adapter, vc)
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        loop = asyncio.get_running_loop()
        released_on = []
        real = adapter._release_voice_stream

        def _spy(h):
            released_on.append(threading.current_thread())
            real(h)

        adapter._release_voice_stream = _spy
        await loop.run_in_executor(None, mixer.stop_speech)
        await _until(lambda: self._released(adapter, handle))
        assert released_on and all(t is threading.main_thread() for t in released_on)

    @pytest.mark.asyncio
    async def test_a_supersede_or_barge_in_still_releases_exactly_once(self, make_adapter):
        adapter, vc = make_adapter({"barge_in": False}, vc=FakeVoiceClient(frame_interval=0.01))
        receiver = MagicMock()
        adapter._voice_receivers[GUILD] = receiver
        first = await adapter.begin_streaming_tts(CHAT, FMT)
        second = await adapter.begin_streaming_tts(CHAT, FMT)
        await asyncio.sleep(0.1)  # the hop for the superseded turn lands, and must be a no-op
        assert first.cut_reason == "superseded" and first.released
        assert adapter._voice_streams[GUILD] is second and not second.released
        await adapter.finish_streaming_tts(second)
        assert receiver.pause.call_count == 2 and receiver.resume.call_count == 2

    @pytest.mark.asyncio
    async def test_a_declined_turn_is_not_released_or_logged_as_a_turn(self, make_adapter, caplog):
        adapter, vc = make_adapter()
        with patch.object(vc, "play", side_effect=RuntimeError("lost the race")), \
                caplog.at_level("INFO", logger="plugins.platforms.discord.adapter_voice_stream"):
            assert await adapter.begin_streaming_tts(CHAT, FMT) is None
            await asyncio.sleep(0.05)
        assert "voice stream ended" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_closed_event_loop_does_not_break_the_player_thread(self, make_adapter):
        adapter, vc = make_adapter()
        mixer = _mixer_on(adapter, vc)
        handle = await adapter.begin_streaming_tts(CHAT, FMT)
        handle.loop = MagicMock()
        handle.loop.call_soon_threadsafe.side_effect = RuntimeError("Event loop is closed")
        mixer.stop_speech()  # must not raise on the caller's thread
        assert handle.cut_reason == "player-stopped"
