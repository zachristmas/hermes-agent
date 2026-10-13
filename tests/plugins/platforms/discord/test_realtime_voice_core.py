"""Realtime voice core: config, resampling, ingress privacy/cost, full duplex, barge-in, output caps,
bounded delegation, lifecycle limits, latency spans and the OpenAI transport framing.

All fake-driven (see realtime_fakes.py): no network, no credentials, no billing."""

import array
import asyncio
import base64
import datetime as dt
import json
import math
import sys

import pytest

from realtime_fakes import (
    ALLOWED, BOT, STRANGER, Clock, FakeDelegate, FakePlayback, FakeTransport, delta, make_session, mono24, stereo_tone,
)

from plugins.platforms.discord.realtime_voice import (
    ACK_STYLES,
    ASK_HERMES,
    DailyAudioBudget,
    DelegationBridge,
    DelegationResult,
    IngressResampler,
    OpenAIRealtimeTransport,
    RealtimePrices,
    RealtimeUnavailable,
    RealtimeVoiceConfig,
    RealtimeVoiceSession,
    SpeakerFocus,
    parse_snowflake,
    parse_speaker_ids,
    UsageMeter,
    session_update_event,
)


def _samples(pcm: bytes) -> array.array:
    a = array.array("h", pcm)
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _rms(samples) -> float:
    return math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))


# --- config ----------------------------------------------------------------------------------


class TestConfig:
    def test_disabled_by_default(self):
        assert RealtimeVoiceConfig().enabled is False
        assert RealtimeVoiceConfig.from_mapping(None).enabled is False
        assert RealtimeVoiceConfig.from_mapping("yes").enabled is False

    def test_clamps_and_falls_back(self):
        cfg = RealtimeVoiceConfig.from_mapping({
            "enabled": "true", "max_session_seconds": 99999, "idle_close_seconds": "nan",
            "delegate_max_inflight": 50, "model": "  ", "prices_per_1m_tokens": {"audio_in": -1, "text_in": "x"},
        })
        assert cfg.enabled is True
        assert cfg.max_session_s == 3600
        assert cfg.idle_close_s == RealtimeVoiceConfig().idle_close_s
        assert cfg.delegate_max_inflight == 4
        assert cfg.model == "gpt-realtime"
        assert cfg.prices.audio_in is None and cfg.prices.text_in is None and not cfg.prices.complete

    def test_session_update_shape(self):
        ev = session_update_event(RealtimeVoiceConfig(enabled=True, instructions="Be brief."))
        s = ev["session"]
        assert ev["type"] == "session.update" and s["type"] == "realtime"
        assert s["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert s["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
        td = s["audio"]["input"]["turn_detection"]
        assert td["type"] == "server_vad" and td["interrupt_response"] is True and td["create_response"] is True
        assert [t["name"] for t in s["tools"]] == [ASK_HERMES]   # exactly one tool, no approvals
        assert s["max_output_tokens"] == 1024
        assert s["instructions"].endswith("Be brief.")
        # English by default, and honest about delegating memory/tools to the regular Hermes session.
        assert "English" in s["instructions"] and "unless the user explicitly asks" in s["instructions"]
        assert "no tools, memory" not in s["instructions"]
        assert "Never claim that Hermes has no memory" in s["instructions"]


# --- resampling ------------------------------------------------------------------------------


class TestIngressResampler:
    def test_geometry_and_level(self):
        out = IngressResampler().convert(stereo_tone(440, frames=5))
        s = _samples(out)
        assert len(s) == 5 * 480                      # 20 ms at 24 kHz mono per input frame
        assert abs(_rms(s[480:]) - 8000 / math.sqrt(2)) < 150   # passband level kept

    def test_chunk_independent(self):
        pcm = stereo_tone(1000, frames=6)
        whole = IngressResampler().convert(pcm)
        r = IngressResampler()
        split = b"".join(r.convert(pcm[i:i + 1000]) for i in range(0, len(pcm), 1000))
        assert split == whole

    def test_alias_band_suppressed(self):
        # 20 kHz would fold to 4 kHz at 24 kHz; the [1,2,1]/4 filter passes cos^2(pi*20/48) ~ 0.067
        s = _samples(IngressResampler().convert(stereo_tone(20000, frames=5)))
        assert _rms(s[480:]) < 0.1 * (8000 / math.sqrt(2))


def test_speaker_focus_holds_then_releases():
    f = SpeakerFocus(600)
    assert f.admit(1, 0.0) == (True, True)
    assert f.admit(2, 0.3) == (False, False)
    assert f.admit(1, 0.5) == (True, False)
    assert f.admit(2, 1.0) == (False, False)
    assert f.admit(2, 1.2) == (True, True)


# --- ingress: privacy, focus, cost --------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_allowlisted_audio_leaves_the_machine():
    session, tx, *_ = make_session()
    await session.start()
    try:
        session.feed_frame(STRANGER, stereo_tone(300), now=1000.0)
        session.feed_frame(BOT, stereo_tone(300), now=1000.0)
        session.feed_frame(0, stereo_tone(300), now=1000.0)
        await session.pump_once(1000.06)
        assert tx.of_type("input_audio_buffer.append") == []
        assert session.counters["frames_dropped_unallowed"] == 1
        session.feed_frame(ALLOWED, stereo_tone(300), now=1000.1)
        await session.pump_once(1000.12)
        assert len(tx.appended_pcm()) == 960        # one 20 ms frame of 24 kHz mono
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_vad_tail_is_bounded_then_stops():
    cfg = RealtimeVoiceConfig(enabled=True, vad_tail_ms=300)
    session, tx, _, _, clock = make_session(cfg)
    await session.start()
    try:
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock())
        await session.pump_once(clock())
        for _ in range(20):                           # 2 s of pump ticks with no speech
            await session.pump_once(clock.advance(0.1))
        silence = [e for e in tx.of_type("input_audio_buffer.append")][1:]
        sent_ms = sum(len(base64.b64decode(e["audio"])) for e in silence) / 48
        assert 290 <= sent_ms <= 300                  # enough for VAD to end the turn, no more
        assert session.counters["silence_appends"] == 3
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_ingress_overflow_drops_oldest_and_counts():
    cfg = RealtimeVoiceConfig(enabled=True, ingress_queue_frames=25)
    session, *_ = make_session(cfg)
    for i in range(30):
        session.feed_frame(ALLOWED, stereo_tone(300), now=1000.0 + i * 0.02)
    assert session.counters["frames_dropped_overflow"] == 5


# --- output, full duplex, barge-in ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_duplex_ingress_continues_while_model_speaks():
    session, tx, playback, _, clock = make_session()
    await session.start()
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(2400)))         # 100 ms of model audio
        assert len(playback.play(2)) == 2                                 # it is playing
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock())       # user talks over it
        await session.pump_once(clock.advance(0.06))
        assert len(tx.appended_pcm()) == 960                               # still streamed upstream
        assert not playback.turns[-1].queue.cut_reason                    # local side never self-cuts
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_output_frames_are_discord_geometry():
    session, tx, playback, *_ = make_session()
    await session.start()
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(24000 // 10 * 3)))  # 300 ms
        await session.handle_event({"type": "response.output_audio.done", "response_id": "r1"})
        frames = playback.play(100)
        assert len(frames) == 15 and all(len(f) == 3840 for f in frames)
        assert playback.turns[-1].queue.finished
    finally:
        await session.close("test")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, 7, ["error"], {"type": "error"}, "no.such.event"])
async def test_server_events_with_an_unknown_or_malformed_type_are_ignored(kind):
    session, tx, playback, *_ = make_session()
    await session.start()
    try:
        await session.handle_event({"type": kind})
        assert not session.closed and session.counters["errors"] == 0
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_barge_in_cuts_playback_and_truncates_to_heard_audio():
    playback = FakePlayback(lead_frames=2)
    session, tx, _, _, clock = make_session(playback=playback)
    await session.start()
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(24000), item="item_A"))  # 1 s queued
        playback.play(2 + 10)                                                     # lead + 200 ms heard
        await session.handle_event({"type": "input_audio_buffer.speech_started", "audio_start_ms": 5,
                                    "item_id": "u1"})
        q = playback.turns[-1].queue
        assert q.cut_reason == "barge-in" and q.read() is None                   # silent from next frame
        (trunc,) = tx.of_type("conversation.item.truncate")
        assert trunc == {"type": "conversation.item.truncate", "item_id": "item_A",
                         "content_index": 0, "audio_end_ms": 200}
        assert tx.of_type("response.cancel") == []    # server VAD interrupt_response cancels upstream
        await session.handle_event(delta("r1", mono24(2400), item="item_A"))      # late audio dropped
        assert session.counters["late_deltas"] == 1 and len(playback.turns) == 1
        assert session.history[-1]["kind"] == "barge-in"
        assert "barge_to_cut_ms" in session.history[-1]
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_output_cap_cancels_response():
    cfg = RealtimeVoiceConfig(enabled=True, max_response_s=2)
    session, tx, playback, *_ = make_session(cfg)
    await session.start()
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(24000 * 3)))   # 3 s > 2 s cap, nothing drained
        assert playback.turns[-1].queue.cut_reason == "output-cap"
        assert len(tx.of_type("response.cancel")) == 1
        assert session.counters["output_capped"] == 1
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_no_output_while_hermes_owns_the_floor():
    playback = FakePlayback()
    playback.blocked = True
    session, tx, *_ = make_session(playback=playback)
    await session.start()
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(2400)))
        assert playback.turns == [] and session.counters["player_busy"] == 1
        await session.handle_event(delta("r1", mono24(2400)))   # rest of that response stays silent
        assert session.counters["late_deltas"] == 1
    finally:
        await session.close("test")


# --- delegation --------------------------------------------------------------------------------


async def _speak(session, clock):
    session.feed_frame(ALLOWED, stereo_tone(300), now=clock())
    await session.pump_once(clock.advance(0.06))


async def _silent_response(session, rid):
    """A response that opens and closes without audio, as the provider reports it."""
    await session.handle_event({"type": "response.created", "response": {"id": rid}})
    await session.handle_event({"type": "response.done", "response": {"id": rid}})


def _tool_call(args, name=ASK_HERMES, call_id="c1"):
    return {"type": "response.function_call_arguments.done", "name": name, "call_id": call_id,
            "item_id": "fc1", "output_index": 0, "response_id": "r1", "arguments": json.dumps(args)}


@pytest.mark.asyncio
async def test_tool_call_acks_now_and_relays_result_when_floor_is_free():
    session, tx, playback, delegate, clock = make_session()
    await session.start()
    try:
        await _speak(session, clock)
        await session.handle_event(_tool_call({"request": "What's the weather in Raleigh?"}))
        await asyncio.sleep(0)
        (out,) = tx.of_type("conversation.item.create")
        payload = json.loads(out["item"]["output"])
        assert out["item"]["type"] == "function_call_output" and out["item"]["call_id"] == "c1"
        assert {k: payload[k] for k in ("status", "job_id")} == {"status": "started", "job_id": "h1"}
        assert "acknowledgement" in payload["next"]                        # ack guidance rides the tool output
        assert len(tx.of_type("response.create")) == 1                    # voice layer acknowledges
        assert delegate.requests[0].user_id == ALLOWED and delegate.requests[0].guild_id == 111
        timings = session._delegations["h1"]
        # the ack plays (voice layer), then the user starts talking again
        await session.handle_event({"type": "response.created", "response": {"id": "r2"}})
        await session.handle_event(delta("r2", mono24(480)))
        await session.handle_event({"type": "response.output_audio.done", "response_id": "r2"})
        await session.handle_event({"type": "response.done", "response": {"id": "r2"}})
        playback.play(10)
        await session.handle_event({"type": "input_audio_buffer.speech_started"})
        clock.advance(4.0)
        delegate.finish()
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(tx.of_type("response.create")) == 1                    # held: user has the floor
        await session.handle_event({"type": "input_audio_buffer.speech_stopped"})
        # server VAD answers that utterance itself, so the result waits for its response.done
        assert len(tx.of_type("response.create")) == 1
        assert not [e for e in tx.of_type("conversation.item.create") if e["item"]["type"] == "message"]
        await _silent_response(session, "v1")
        system = [e for e in tx.of_type("conversation.item.create") if e["item"]["type"] == "message"]
        assert system and system[-1]["item"]["role"] == "system"
        assert "It is 21 degrees." in system[-1]["item"]["content"][0]["text"]
        assert len(tx.of_type("response.create")) == 2
        await session.handle_event({"type": "response.created", "response": {"id": "r3"}})
        clock.advance(0.3)
        await session.handle_event(delta("r3", mono24(480)))
        report = timings.report()
        assert report["tool_call_to_ack_audio_ms"] == 0.0     # fake clock did not move before the ack
        assert report["delegated_work_ms"] == 4000.0          # Hermes's own latency, reported apart
        assert report["result_to_audio_ms"] == 300.0
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_each_ack_instruction_is_a_different_style_and_states_no_result():
    cfg = RealtimeVoiceConfig(enabled=True, delegate_max_inflight=4, delegate_per_minute=30)   # 4 is the clamp
    session, tx, _, _, clock = make_session(cfg)
    await session.start()
    try:
        await _speak(session, clock)
        for i in range(4):
            await session.handle_event(_tool_call({"request": f"question {i}"}, call_id=f"c{i}"))
        outs = [json.loads(e["item"]["output"]) for e in tx.of_type("conversation.item.create")
                if e["item"]["type"] == "function_call_output"]
        nexts = [o["next"] for o in outs]
        assert [o["status"] for o in outs] == ["started"] * 4
        assert len(set(nexts)) == 4                                       # never the same style twice in a row
        assert all("do not state any result yet" in n for n in nexts)
        nexts += [session._ack_guidance() for _ in range(len(ACK_STYLES) - 4 + 1)]
        assert nexts[len(ACK_STYLES)] == nexts[0]                         # the rotation wraps
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_refused_hand_off_is_not_acknowledged_as_work_in_progress():
    session, tx, _, delegate, clock = make_session()
    await session.start()
    try:
        await _speak(session, clock)
        await session.handle_event(_tool_call({"request": "/approve"}))
        (out,) = tx.of_type("conversation.item.create")
        payload = json.loads(out["item"]["output"])
        assert payload["status"] == "rejected" and payload["reason"] == "commands-not-allowed"
        assert "Hermes was not asked" in payload["next"] and "working on it" not in payload["next"]
        assert delegate.requests == []
        assert session._ack_index == 0                                    # a refusal does not use up an ack style
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_identical_delegations_from_different_speakers_keep_their_own_results():
    cfg = RealtimeVoiceConfig(enabled=True, delegate_max_inflight=2)
    delegate = FakeDelegate()
    session, tx, _, _, _ = make_session(cfg, delegate=delegate)
    await session.start()
    try:
        first = session.bridge.submit(111, ALLOWED, "same request")
        second = session.bridge.submit(111, ALLOWED + 1, "same request")
        assert first["status"] == second["status"] == "started"
        await asyncio.sleep(0)
        delegate.finish(index=1, result=DelegationResult("done", "second speaker result"))
        for _ in range(3):
            await asyncio.sleep(0)
        await _silent_response(session, "r1")       # the first relayed result is voiced before the next
        delegate.finish(index=0, result=DelegationResult("done", "first speaker result"))
        for _ in range(3):
            await asyncio.sleep(0)
        messages = [e["item"]["content"][0]["text"] for e in tx.of_type("conversation.item.create")
                    if e["item"].get("role") == "system"]
        assert any("second speaker result" in text and "h2" in text for text in messages)
        assert any("first speaker result" in text and "h1" in text for text in messages)
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_hermes_delivered_result_is_noted_not_spoken():
    session, tx, _, delegate, clock = make_session()
    await session.start()
    try:
        await _speak(session, clock)
        await session.handle_event(_tool_call({"request": "Summarise my inbox"}))
        await asyncio.sleep(0)
        await _silent_response(session, "ack")
        delegate.finish(result=DelegationResult("done", delivered_by="hermes"))
        for _ in range(3):
            await asyncio.sleep(0)
        notes = [e for e in tx.of_type("conversation.item.create") if e["item"]["type"] == "message"]
        assert notes and "delivered its answer itself" in notes[-1]["item"]["content"][0]["text"]
        assert len(tx.of_type("response.create")) == 1          # only the original ack
        assert session.counters["results_noted"] == 1
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_a_job_hermes_did_not_take_is_said_plainly_without_an_answer():
    session, tx, _, delegate, clock = make_session()
    await session.start()
    try:
        await _speak(session, clock)
        await session.handle_event(_tool_call({"request": "What's on my calendar?"}))
        await asyncio.sleep(0)
        await _silent_response(session, "ack")
        delegate.finish(result=DelegationResult("not-admitted"))
        for _ in range(3):
            await asyncio.sleep(0)
        notes = [e for e in tx.of_type("conversation.item.create") if e["item"]["type"] == "message"]
        assert "did not take job h1" in notes[-1]["item"]["content"][0]["text"]
        assert len(tx.of_type("response.create")) == 2          # the ack, then the spoken notice
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_unknown_tool_and_command_requests_are_rejected():
    session, tx, _, delegate, clock = make_session()
    await session.start()
    try:
        await _speak(session, clock)
        await session.handle_event(_tool_call({"request": "x"}, name="run_shell", call_id="a"))
        await session.handle_event(_tool_call({"request": "/approve"}, call_id="b"))
        await session.handle_event(_tool_call({"request": "  /yolo on"}, call_id="c"))
        outs = [json.loads(e["item"]["output"]) for e in tx.of_type("conversation.item.create")]
        assert [o["reason"] for o in outs] == ["unknown-tool", "commands-not-allowed", "commands-not-allowed"]
        assert delegate.requests == []
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_bridge_bounds_inflight_rate_and_length():
    clock = Clock()
    delegate = FakeDelegate()
    done = []
    cfg = RealtimeVoiceConfig(enabled=True, delegate_max_inflight=1, delegate_per_minute=2,
                              delegate_max_chars=20)
    bridge = DelegationBridge(delegate, cfg, on_complete=lambda j, r: done.append((j.job_id, r.status)),
                              clock=clock)
    assert bridge.submit(1, 0, "hi")["reason"] == "no-speaker"
    assert bridge.submit(1, 5, "x" * 21)["reason"] == "too-long"
    assert bridge.submit(1, 5, "first")["status"] == "started"
    await asyncio.sleep(0)
    assert bridge.submit(1, 5, "second")["reason"] == "busy"
    delegate.finish()
    for _ in range(3):
        await asyncio.sleep(0)
    assert done == [("h1", "done")] and bridge.inflight == 0
    assert bridge.submit(1, 5, "second")["status"] == "started"
    await asyncio.sleep(0)
    delegate.finish()
    for _ in range(3):
        await asyncio.sleep(0)
    assert bridge.submit(1, 5, "third")["reason"] == "rate-limited"
    clock.advance(61)
    assert bridge.submit(1, 5, "third")["status"] == "started"
    bridge.cancel_waiters()


@pytest.mark.asyncio
async def test_bridge_timeout_reports_but_keeps_the_slot_until_hermes_finishes():
    delegate = FakeDelegate()
    done = []
    cfg = RealtimeVoiceConfig(enabled=True, delegate_timeout_s=0.05)   # constructor: no clamp
    bridge = DelegationBridge(delegate, cfg, on_complete=lambda j, r: done.append(r.status))
    assert bridge.submit(1, 5, "slow job")["status"] == "started"
    await asyncio.sleep(0.15)
    assert done == ["timeout"]
    assert bridge.inflight == 1                              # Hermes is still working: no new job
    assert bridge.submit(1, 5, "another")["reason"] == "busy"
    assert not delegate.futures[0].cancelled()               # never cancelled from the voice layer
    delegate.finish()
    for _ in range(3):
        await asyncio.sleep(0)
    assert bridge.inflight == 0


# --- lifecycle, cost, latency --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_limits_max_session_idle_and_daily_budget():
    cfg = RealtimeVoiceConfig(enabled=True, max_session_s=60, idle_close_s=20)
    session, _, _, _, clock = make_session(cfg)
    await session.start()
    try:
        assert session.check_limits(clock.advance(10)) == ""
        assert session.check_limits(clock.advance(15)) == "idle"
        session._last_activity = clock()
        assert session.check_limits(clock.advance(40)) == "max-session"
    finally:
        await session.close("test")
    budget = DailyAudioBudget(1.0)
    session, *_ = make_session(budget=budget)
    budget.consume(1.0)
    with pytest.raises(RealtimeUnavailable):
        await session.start()


def test_daily_budget_rolls_over():
    day = [dt.date(2026, 10, 10)]
    b = DailyAudioBudget(10, today=lambda: day[0])
    b.consume(10)
    assert b.exhausted
    day[0] = dt.date(2026, 10, 11)
    assert not b.exhausted and b.used_s == 0


@pytest.mark.asyncio
async def test_input_batch_is_capped_by_remaining_daily_audio_budget():
    budget = DailyAudioBudget(0.05)
    budget.consume(0.04)
    session, tx, *_ = make_session(budget=budget)
    await session.start()
    try:
        session.feed_frame(ALLOWED, stereo_tone(300), now=1000.0)
        session.feed_frame(ALLOWED, stereo_tone(300, frames=2), now=1000.02)
        await session.pump_once(1000.06)
        sent_s = len(tx.appended_pcm()) / (24000 * 2)
        assert sent_s <= 0.01
        assert budget.used_s <= budget.limit_s
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_output_delta_is_capped_by_remaining_daily_audio_budget():
    budget = DailyAudioBudget(0.05)
    budget.consume(0.04)
    session, tx, *_ = make_session(budget=budget)
    await session.start()
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "budgeted"}})
        await session.handle_event(delta("budgeted", mono24(24000)))
        assert session.meter.audio_out_s <= 0.01
        assert budget.used_s <= budget.limit_s
        assert session._out is not None
        assert session._out.turn.queue.buffered <= 10
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_usd_cap_only_with_prices_and_usage_metering():
    usage = {"input_token_details": {"audio_tokens": 1_000_000, "text_tokens": 0, "cached_tokens": 0},
             "output_token_details": {"audio_tokens": 100_000, "text_tokens": 0}}
    unpriced, *_ = make_session(RealtimeVoiceConfig(enabled=True, max_session_usd=1))
    await unpriced.start()
    await unpriced.handle_event({"type": "response.done", "response": {"id": "r", "usage": usage}})
    assert unpriced.meter.usd(unpriced.cfg.prices) is None and unpriced.check_limits(unpriced._clock()) == ""
    assert unpriced.meter.tokens["audio_in"] == 1_000_000
    await unpriced.close("test")
    prices = RealtimePrices(audio_in=2.0, audio_out=4.0, text_in=1.0, text_out=1.0)   # test numbers only
    priced, *_ = make_session(RealtimeVoiceConfig(enabled=True, max_session_usd=2.0, prices=prices))
    await priced.start()
    await priced.handle_event({"type": "response.done", "response": {"id": "r", "usage": usage}})
    assert priced.meter.usd(prices) == pytest.approx(2.4)
    assert priced.check_limits(priced._clock()) == "usd-cap"
    await priced.close("test")


def test_usage_meter_tolerates_garbage():
    m = UsageMeter()
    m.add_usage(None)
    m.add_usage({"input_token_details": {"audio_tokens": "x"}, "output_token_details": None})
    assert m.tokens["audio_in"] == 0 and m.responses == 1


@pytest.mark.asyncio
async def test_stage_latency_spans_for_a_spoken_turn():
    session, tx, playback, _, clock = make_session()
    await session.start()
    try:
        await session.handle_event({"type": "input_audio_buffer.speech_started"})
        clock.advance(1.0)
        await session.handle_event({"type": "input_audio_buffer.speech_stopped"})
        clock.advance(0.150)
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        clock.advance(0.250)
        await session.handle_event(delta("r1", mono24(480)))
        await session.handle_event({"type": "response.output_audio.done", "response_id": "r1"})
        playback.play(5)
        q = playback.turns[-1].queue
        q._first_out_at = clock() + 0.040          # the player thread pulled it 40 ms later
        session.reap_output()
        report = session.history[-1]
        assert report["vad_to_response_ms"] == 150.0
        assert report["vad_to_first_audio_ms"] == 400.0
        assert report["delta_to_playout_ms"] == 40.0
        assert report["end_of_speech_to_playout_ms"] == 440.0
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_close_is_idempotent_and_releases_everything():
    session, tx, playback, delegate, clock = make_session()
    await session.start()
    await _speak(session, clock)
    await session.handle_event(_tool_call({"request": "long job"}))
    await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
    await session.handle_event(delta("r1", mono24(2400)))
    await session.close("left")
    await session.close("again")
    assert session.closed_reason == "left" and tx.closed
    assert playback.turns[-1].queue.cut_reason == "session-closed"
    assert all(t.done() for t in session._tasks)
    session.feed_frame(ALLOWED, stereo_tone(300))
    assert len(session._ingress) == 0
    assert not delegate.futures[0].cancelled()     # Hermes's own turn is not ours to cancel


@pytest.mark.asyncio
async def test_transport_close_ends_the_session():
    session, tx, *_ = make_session()
    await session.start()
    await tx.inbox.put(None)
    for _ in range(5):
        await asyncio.sleep(0)
    assert session.closed_reason == "transport-closed"


# --- OpenAI transport framing (fake socket, no network) -----------------------------------------


class _Msg:
    def __init__(self, kind, data=None):
        self.type = type("T", (), {"name": kind})()
        self.data = data


class _FakeWS:
    def __init__(self, messages):
        self.messages, self.sent, self.closed = list(messages), [], False

    async def send_str(self, s):
        self.sent.append(s)

    async def receive(self):
        return self.messages.pop(0)

    async def close(self):
        self.closed = True


class _FakeHTTP:
    def __init__(self, ws):
        self.ws, self.calls = ws, []

    async def ws_connect(self, url, **kw):
        self.calls.append((url, kw))
        return self.ws


@pytest.mark.asyncio
async def test_openai_transport_needs_key_and_never_connects_without_it():
    http = _FakeHTTP(_FakeWS([]))
    t = OpenAIRealtimeTransport(model="gpt-realtime", session_factory=lambda: http, environ={})
    with pytest.raises(RealtimeUnavailable):
        await t.connect()
    assert http.calls == []


@pytest.mark.asyncio
async def test_openai_transport_framing():
    ws = _FakeWS([_Msg("PING"), _Msg("TEXT", "not json"), _Msg("TEXT", '{"type":"session.created"}'),
                  _Msg("CLOSE")])
    http = _FakeHTTP(ws)
    t = OpenAIRealtimeTransport(model="gpt-realtime", api_key_env="RT_KEY",
                                session_factory=lambda: http, environ={"RT_KEY": "sk-test"})
    await t.connect()
    url, kw = http.calls[0]
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime"
    assert kw["headers"] == {"Authorization": "Bearer sk-test"}
    await t.send({"type": "response.create"})
    assert json.loads(ws.sent[0]) == {"type": "response.create"}
    assert await t.recv() == {"type": "session.created"}
    assert await t.recv() is None
    await t.close()
    assert ws.closed


# --- exact-ID allowlist: fail closed, before any provider audio ----------------------------------


@pytest.mark.parametrize("raw,expected", [
    ([42], {42}), (["42", 43], {42, 43}), ((7,), {7}), ({9}, {9}), ([42, 42], {42}),
    ([(1 << 64) - 1], {(1 << 64) - 1}),
])
def test_speaker_ids_parse_only_exact_numeric_ids(raw, expected):
    assert parse_speaker_ids(raw) == frozenset(expected)


@pytest.mark.parametrize("raw", [
    [], (), set(), None, "42", b"42", 42, {"42": 1}, ["*"], ["everyone"], [True], [False], [42.0], [None],
    ["042"], [" 42"], ["42 "], ["+42"], ["-42"], ["4_2"], ["0x2a"], ["４２"], ["٤٢"], [0], [-1], [1 << 64], [42, "x"],
])
def test_speaker_ids_reject_everything_else_all_or_nothing(raw):
    with pytest.raises(ValueError):
        parse_speaker_ids(raw)


def test_snowflake_zero_is_not_an_id():
    assert parse_snowflake("1") == 1
    for bad in ("0", 0, "00", "", None, 1.0, True):
        with pytest.raises(ValueError):
            parse_snowflake(bad)


@pytest.mark.parametrize("allowed", [[], (), set(), None, "42", ["*"], [True], [42.0], ["４２"]])
def test_a_session_cannot_be_built_without_an_exact_allowlist(allowed):
    with pytest.raises(ValueError):
        make_session(allowed=allowed)


@pytest.mark.asyncio
async def test_nothing_but_session_config_is_sent_until_an_allowed_speaker_talks():
    session, tx, _, _, clock = make_session()
    await session.start()
    try:
        for _ in range(10):
            await session.pump_once(clock.advance(0.06))
        assert [e["type"] for e in tx.sent] == ["session.update"]
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_inexact_speaker_identities_never_produce_provider_audio_or_vad_tail():
    session, tx, _, _, clock = make_session()
    await session.start()
    try:
        for who in (str(ALLOWED), float(ALLOWED), b"42", STRANGER):
            session.feed_frame(who, stereo_tone(300), now=clock.t)
        for _ in range(20):                            # long enough for a VAD tail to be owed
            await session.pump_once(clock.advance(0.06))
        assert tx.of_type("input_audio_buffer.append") == []
        assert session.counters["frames_dropped_unallowed"] == 4 and session.counters["frames_sent"] == 0
        assert session._focus.user_id == 0 and session._last_voiced_at is None
    finally:
        await session.close("test")


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", [lambda uid: False, lambda uid: None, lambda uid: "yes", lambda uid: 1,
                                  lambda uid: 1 / 0])
async def test_the_live_gate_fails_closed_for_anything_but_true(gate):
    session, tx, _, _, clock = make_session(is_allowed=gate)
    await session.start()
    try:
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock.t)
        await session.pump_once(clock.advance(0.06))
        assert tx.of_type("input_audio_buffer.append") == [] and session.counters["frames_dropped_unallowed"] == 1
    finally:
        await session.close("test")


@pytest.mark.asyncio
async def test_the_live_gate_can_narrow_but_never_widen_the_allowlist():
    session, tx, _, _, clock = make_session(is_allowed=lambda uid: True)
    await session.start()
    try:
        session.feed_frame(STRANGER, stereo_tone(300), now=clock.t)
        await session.pump_once(clock.advance(0.06))
        assert tx.of_type("input_audio_buffer.append") == []
        session.feed_frame(ALLOWED, stereo_tone(300), now=clock.t)
        await session.pump_once(clock.advance(0.06))
        assert len(tx.appended_pcm()) == 960
    finally:
        await session.close("test")
