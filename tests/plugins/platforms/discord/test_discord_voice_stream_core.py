"""Pure building blocks of Discord streaming voice (plugins/platforms/discord/voice_stream.py):
the PCM framer, the one-turn bounded frame queue, the barge-in speech gate and config parsing.

stdlib only, no discord.py: these run on discord.py's player / socket-reader threads and are
tested directly.
"""

import array
import math
import sys
import threading

import pytest

from plugins.platforms.discord.voice_stream import (
    FRAME_BYTES,
    SILENCE_FRAME,
    BargeInGate,
    FrameQueue,
    PCMFramer,
    UnsupportedAudioFormat,
    VoiceStreamConfig,
    frame_rms,
)


def _pcm(samples, channels=1):
    """int16 little-endian PCM from a list of per-frame sample values (repeated per channel)."""
    arr = array.array("h", [s for s in samples for _ in range(channels)])
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def _samples(frame):
    arr = array.array("h", frame)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr


def _tone_frame(amplitude, *, ms=20):
    """48 kHz stereo frame carrying a 440 Hz tone at *amplitude* (int16 peak)."""
    n = 48 * ms
    return _pcm([int(amplitude * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(n)], channels=2)


class TestPCMFramer:
    def test_24k_mono_becomes_whole_48k_stereo_frames(self):
        framer = PCMFramer(24000, 1)
        # 30 ms of 24 kHz mono (720 samples) is 1.5 output frames: one now, the padded rest on flush.
        frames = framer.feed(_pcm([1000] * 720))
        assert len(frames) == 1 and len(frames[0]) == FRAME_BYTES
        tail = framer.flush()
        assert len(tail) == 1 and len(tail[0]) == FRAME_BYTES
        # Constant input stays constant after resampling + channel duplication (tail padded with 0).
        assert set(_samples(frames[0])) == {1000}
        assert _samples(tail[0])[0] == 1000 and _samples(tail[0])[-1] == 0

    def test_chunk_boundaries_do_not_change_the_output(self):
        ramp = _pcm([(i * 37) % 20000 - 10000 for i in range(2400)])  # 100 ms @ 24 kHz mono
        whole = PCMFramer(24000, 1)
        expected = whole.feed(ramp) + whole.flush()
        split = PCMFramer(24000, 1)
        got = []
        for cut in range(0, len(ramp), 333):  # odd byte counts split samples mid-way
            got += split.feed(ramp[cut:cut + 333])
        got += split.flush()
        assert got == expected

    def test_48k_stereo_passes_through_untouched(self):
        frame = _tone_frame(8000)
        framer = PCMFramer(48000, 2)
        assert framer.feed(frame[:1000]) == []
        assert framer.feed(frame[1000:]) == [frame]
        assert framer.flush() == []

    def test_non_integer_ratio_rate_is_accepted(self):
        framer = PCMFramer(22050, 1)
        frames = framer.feed(_pcm([500] * 22050)) + framer.flush()  # 1 s of audio
        assert 49 <= len(frames) <= 51

    @pytest.mark.parametrize("rate,channels,width", [(24000, 1, 1), (24000, 3, 2), (0, 1, 2), (400000, 1, 2)])
    def test_unsupported_formats_are_refused(self, rate, channels, width):
        with pytest.raises(UnsupportedAudioFormat):
            PCMFramer(rate, channels, width)


class TestFrameQueue:
    def test_frames_play_in_order_then_underrun_is_silence(self):
        q = FrameQueue(max_frames=4)
        a, b = _tone_frame(100), _tone_frame(200)
        assert q.offer(a) and q.offer(b)
        assert q.read() == a and q.read() == b
        assert q.read() == SILENCE_FRAME  # open but starved: keep the player fed
        assert q.underruns == 1 and not q.finished

    def test_bounded_offer_reports_full(self):
        q = FrameQueue(max_frames=2)
        assert q.offer(SILENCE_FRAME) and q.offer(SILENCE_FRAME)
        assert q.offer(SILENCE_FRAME) is False
        q.read()
        assert q.offer(SILENCE_FRAME) is True

    def test_close_drains_then_ends(self):
        q = FrameQueue(max_frames=4)
        frame = _tone_frame(300)
        q.offer(frame)
        q.close()
        assert not q.finished
        assert q.read() == frame
        assert q.read() is None and q.finished

    def test_cut_drops_queued_and_late_frames(self):
        q = FrameQueue(max_frames=8)
        for _ in range(5):
            q.offer(_tone_frame(1000))
        assert q.cut("barge-in") is True
        assert q.cut("again") is False  # first reason wins
        assert q.cut_reason == "barge-in"
        assert q.read() is None and q.finished
        assert q.offer(_tone_frame(1000)) is True  # late write is accepted and dropped, never raised
        assert q.read() is None
        assert q.dropped == 6

    def test_records_first_audio_and_cut_to_silence_timings(self):
        q = FrameQueue(max_frames=4)
        q.offer(_tone_frame(10))
        q.read()
        q.cut("barge-in")
        q.read()
        timings = q.timings_ms()
        assert set(timings) == {"first_audio_ms", "cut_to_silence_ms"}
        assert all(v >= 0 for v in timings.values())

    def test_concurrent_writer_and_player_lose_nothing(self):
        q = FrameQueue(max_frames=3)
        frames = [_tone_frame(i + 1) for i in range(200)]
        played = []

        def player():
            while True:
                frame = q.read()
                if frame is None:
                    return
                if frame != SILENCE_FRAME:
                    played.append(frame)

        thread = threading.Thread(target=player)
        thread.start()
        for frame in frames:
            while not q.offer(frame):
                pass
        q.close()
        thread.join(timeout=10)
        assert played == frames

    def test_closed_reports_the_writer_is_done(self):
        q = FrameQueue(4)
        assert q.closed is False
        q.offer(b"a")
        q.close()
        assert q.closed is True and not q.finished  # a frame is still queued

    def test_on_cut_fires_once_with_the_first_reason_outside_the_lock(self):
        q = FrameQueue(max_frames=4)
        seen = []
        q.on_cut = lambda reason: seen.append((reason, q.buffered, q.cut_reason))  # re-enters the queue
        assert q.cut("player-stopped") is True
        assert q.cut("barge-in") is False
        assert seen == [("player-stopped", 0, "player-stopped")]

    def test_a_failing_on_cut_never_breaks_the_caller(self):
        q = FrameQueue(max_frames=4)

        def _boom(reason):
            raise RuntimeError("loop closed")

        q.on_cut = _boom
        assert q.cut("aborted") is True and q.cut_reason == "aborted"

    def test_on_cut_is_not_fired_for_a_turn_that_ends_normally(self):
        q = FrameQueue(max_frames=4)
        q.on_cut = lambda reason: pytest.fail("not cut")
        q.offer(SILENCE_FRAME)
        q.close()
        assert q.read() == SILENCE_FRAME and q.read() is None and q.finished


class TestBargeInGate:
    def _feed(self, gate, user, amplitude, *, frames, start=0.0):
        fired = []
        for i in range(frames):
            fired.append(gate.feed(user, _tone_frame(amplitude), start + i * 0.02))
        return fired

    def test_sustained_speech_fires_once(self):
        gate = BargeInGate(rms_threshold=500, sustain_ms=300)
        fired = self._feed(gate, 7, 6000, frames=40)
        assert fired.count(True) == 1
        assert fired.index(True) == 14  # 15 voiced frames = 300 ms

    def test_brief_interjection_and_quiet_noise_never_fire(self):
        gate = BargeInGate(rms_threshold=500, sustain_ms=300)
        assert True not in self._feed(gate, 7, 6000, frames=8)  # "mm-hm": 160 ms
        assert True not in self._feed(gate, 7, 100, frames=100, start=5.0)  # hum below threshold

    def test_pause_longer_than_hangover_restarts_the_count(self):
        gate = BargeInGate(rms_threshold=500, sustain_ms=300, hangover_ms=200)
        assert True not in self._feed(gate, 7, 6000, frames=10)
        assert True not in self._feed(gate, 7, 6000, frames=10, start=1.0)  # 0.8 s gap
        assert True in self._feed(gate, 7, 6000, frames=10, start=1.2)  # continuous with the last run

    def test_speakers_are_counted_separately(self):
        gate = BargeInGate(rms_threshold=500, sustain_ms=300)
        fired = []
        for i in range(20):  # two people alternating 20 ms each never reach 300 ms apiece
            fired.append(gate.feed(1 if i % 2 else 2, _tone_frame(6000), i * 0.02))
        assert True not in fired

    def test_reset_rearms_a_speaker_already_counted(self):
        gate = BargeInGate(rms_threshold=500, sustain_ms=100)
        assert True in self._feed(gate, 7, 6000, frames=10)
        gate.reset()
        assert True in self._feed(gate, 7, 6000, frames=10, start=0.2)

    def test_frame_rms_of_full_scale_and_silence(self):
        assert frame_rms(SILENCE_FRAME) == 0
        assert 4000 < frame_rms(_tone_frame(6000)) < 4500  # sine RMS = peak / sqrt(2)


class TestVoiceStreamConfig:
    def test_defaults_are_off(self):
        cfg = VoiceStreamConfig.from_mapping(None)
        assert cfg.enabled is False and cfg.barge_in is True

    def test_values_parse_and_clamp(self):
        cfg = VoiceStreamConfig.from_mapping({
            "enabled": "true", "buffer_ms": 50, "stall_timeout_seconds": "2.5",
            "producer_timeout_seconds": 99999,
            "barge_in": False, "barge_in_ms": 99999, "barge_in_rms": "800",
        })
        assert cfg.enabled is True and cfg.barge_in is False
        assert cfg.buffer_frames == 5  # clamped up to 100 ms
        assert cfg.stall_timeout_s == 2.5
        assert cfg.producer_timeout_s == 600  # clamped down
        assert cfg.barge_in_ms == 3000 and cfg.barge_in_rms == 800

    @pytest.mark.parametrize("bad", [{"buffer_ms": "lots"}, {"stall_timeout_seconds": float("nan")},
                                     {"stall_timeout_seconds": float("inf")}, {"barge_in_rms": None},
                                     {"producer_timeout_seconds": "soon"}])
    def test_malformed_values_fall_back_to_defaults(self, bad):
        default = VoiceStreamConfig.from_mapping({})
        cfg = VoiceStreamConfig.from_mapping(bad)
        assert (cfg.buffer_frames, cfg.stall_timeout_s, cfg.producer_timeout_s, cfg.barge_in_rms) == (
            default.buffer_frames, default.stall_timeout_s, default.producer_timeout_s, default.barge_in_rms)

    def test_non_mapping_is_disabled(self):
        assert VoiceStreamConfig.from_mapping("yes").enabled is False


def test_shipped_defaults_are_off_and_match_the_parser():
    """``discord.voice_streaming`` in DEFAULT_CONFIG: documented keys, opt-in, parsed unchanged."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    shipped = DEFAULT_CONFIG["discord"]["voice_streaming"]
    assert shipped["enabled"] is False
    assert set(shipped) == {"enabled", "buffer_ms", "stall_timeout_seconds", "producer_timeout_seconds",
                            "barge_in", "barge_in_ms", "barge_in_rms"}
    assert VoiceStreamConfig.from_mapping(shipped) == VoiceStreamConfig()
