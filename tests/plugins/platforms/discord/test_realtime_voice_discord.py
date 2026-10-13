"""Realtime voice wired into the real DiscordAdapter and the production VoiceReceiver.

Real libopus, real NaCl AEAD (rtpsize) and discord.py 2.7's own ``VoiceClient._get_voice_packet``;
fake realtime transport, fake voice client player, and a recording stand-in for the DAVE session
(davey cannot form an MLS group offline). This is codec/packet-path evidence, not live Discord."""

import array
import asyncio
import dataclasses
import math
import os
import struct
import sys
import time
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from discord_voice_fakes import ALLOWED_USER, GUILD, STRANGER, TEXT_CHANNEL, FakeVoiceClient
from realtime_fakes import FakeDelegate, FakeTransport, delta, mono24

from gateway.platforms.base import AudioFormat
from gateway.platforms.event import MessageType, ProcessingOutcome
from plugins.platforms.discord import adapter_realtime_voice as rt_mod
from plugins.platforms.discord.realtime_voice import DelegationRequest, RealtimeUnavailable, RealtimeVoiceConfig

discord = pytest.importorskip("discord")
nacl_secret = pytest.importorskip("nacl.secret")

CHAT = str(TEXT_CHANNEL)
VOICE_CHANNEL = 5001
BOT_SSRC, USER_SSRC, STRANGER_SSRC = 7, 1000, 2000


@pytest.fixture(scope="module")
def opus():
    if not discord.opus.is_loaded():
        try:
            discord.opus._load_default()
        except Exception:
            pass
    if not discord.opus.is_loaded():
        pytest.skip("libopus not available")
    return discord.opus


CONTROL = {"enabled": True, "guild_id": GUILD, "user_ids": [ALLOWED_USER]}


@pytest.fixture
def make_rt_adapter():
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    built = []
    active_config = {"discord": {}}

    def _read_config():
        return active_config

    def _make(realtime=None, *, streaming=True, control=CONTROL):
        discord_cfg = {"voice_streaming": {"enabled": streaming}}
        if realtime is not None:
            discord_cfg["realtime_voice"] = realtime
        if control is not None:
            discord_cfg["realtime_voice_control"] = control
        active_config["discord"] = discord_cfg
        with patch("hermes_cli.config.load_config", return_value={"discord": discord_cfg}), \
             patch("hermes_cli.config.read_raw_config", return_value=active_config), \
             patch("hermes_cli.config.get_config_value", return_value=TEXT_CHANNEL):
            adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
        adapter._client = MagicMock()
        adapter._allowed_user_ids = {str(ALLOWED_USER)}
        adapter._voice_timeout_seconds = 0
        vc = FakeVoiceClient(frame_interval=0.002)
        vc.channel.id = VOICE_CHANNEL
        adapter._voice_clients[GUILD] = vc
        adapter._voice_text_channels[GUILD] = TEXT_CHANNEL
        built.append((adapter, vc))
        return adapter, vc

    yield _make
    for adapter, vc in built:
        vc.stop()


class FakeProvider:
    """Stands in for ``OpenAIRealtimeTransport`` where the adapter looks it up: records every
    construction (a connection attempt) and hands the session a scripted transport. Nothing here
    opens a socket, reads a key or reaches OpenAI."""

    def __init__(self, transport=None, *, unavailable=False):
        self.transport = transport or FakeTransport()
        self.unavailable = unavailable
        self.constructed = []

    def __call__(self, *, model, api_key_env):
        self.constructed.append((model, api_key_env))
        provider = self

        class _Transport:
            async def connect(self_inner):
                if provider.unavailable:
                    raise RealtimeUnavailable("no key")
                return provider.transport

        return _Transport()


def _receiver(adapter, vc, key):
    from plugins.platforms.discord.adapter import VoiceReceiver

    receiver = VoiceReceiver(vc, allowed_user_ids=adapter._allowed_user_ids)
    receiver._creds = (key, None, 0, False)
    receiver._bot_ssrc = BOT_SSRC
    receiver._running = True
    receiver.map_ssrc(USER_SSRC, ALLOWED_USER)
    receiver.map_ssrc(STRANGER_SSRC, STRANGER)
    receiver.frame_sink = adapter._voice_frame_sink(GUILD)
    adapter._voice_receivers[GUILD] = receiver
    return receiver


def _tone(seq, freq=300, amplitude=8000):
    arr = array.array("h")
    for i in range(960):
        v = int(amplitude * math.sin(2 * math.pi * freq * (seq * 960 + i) / 48000))
        arr.extend((v, v))
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def _rtp(key, ssrc, seq, payload):
    header = struct.pack(">BBHII", 0x80, 0x78, seq, seq * 960, ssrc)
    nonce4 = struct.pack(">I", seq)
    sealed = nacl_secret.Aead(key).encrypt(payload, header, nonce4 + b"\x00" * 20)
    return header + sealed.ciphertext + nonce4


async def _feed(receiver, packets):
    def socket_thread():
        for p in packets:
            receiver._on_packet(p)
            time.sleep(0.002)

    await asyncio.get_running_loop().run_in_executor(None, socket_thread)


async def _start(adapter, transport=None, *, requester=ALLOWED_USER):
    """Explicit start action against a no-network provider transport."""
    provider = FakeProvider(transport)
    constructed = []

    def factory(cfg):
        constructed.append((cfg.model, cfg.api_key_env))
        class _Transport:
            async def connect(self):
                if provider.unavailable:
                    raise RealtimeUnavailable("no key")
                return provider.transport
        return _Transport()

    adapter._realtime_transport_factory = factory
    try:
        result = await adapter.start_realtime_voice(GUILD, requester_id=requester)
    finally:
        del adapter._realtime_transport_factory
    return result.started, provider.transport


def _goertzel(samples, freq, rate):
    k = 2 * math.cos(2 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for x in samples:
        s1, s2 = x + k * s1 - s2, s1
    return s1 * s1 + s2 * s2 - k * s1 * s2


RT = {"enabled": True, "ingress_batch_ms": 20}


class _Tree:
    def __init__(self):
        self.commands = {}

    def command(self, *, name, description):
        def register(callback):
            self.commands[name] = callback
            return callback
        return register

    def add_command(self, command):
        self.commands[command.name] = command.callback

    def get_commands(self):
        return []


def _register_slash(adapter):
    tree = _Tree()
    adapter._client.tree = tree
    with patch("hermes_cli.commands.COMMAND_REGISTRY", []), patch(
        "hermes_cli.commands._iter_plugin_command_entries", return_value=[]
    ), patch.object(adapter, "_register_skill_group"):
        adapter._register_slash_commands()
    return tree


def _interaction(user=ALLOWED_USER, guild=GUILD):
    return SimpleNamespace(guild=SimpleNamespace(id=guild), guild_id=guild, user=SimpleNamespace(id=user),
                           response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock())


async def _attempt(adapter, *, requester=ALLOWED_USER, guild=GUILD):
    """One explicit start attempt against a provider that records construction."""
    constructed = []
    provider = FakeProvider()

    def patched_transport_factory(cfg):
        constructed.append((cfg.model, cfg.api_key_env))
        class _Transport:
            async def connect(self):
                if provider.unavailable:
                    raise RealtimeUnavailable("no key")
                return provider.transport
        return _Transport()

    adapter._realtime_transport_factory = patched_transport_factory
    try:
        result = await adapter.start_realtime_voice(guild, requester_id=requester)
    finally:
        del adapter._realtime_transport_factory
    return result, SimpleNamespace(constructed=constructed, transport=provider.transport)


# --- /realtime-voice: its own command, gated by slash auth then the control gate ---------------


def test_realtime_voice_command_is_only_registered_when_the_feature_is_enabled(make_rt_adapter):
    off, _ = make_rt_adapter()
    on, _ = make_rt_adapter(RT)
    assert "realtime-voice" not in _register_slash(off).commands
    assert "realtime-voice" in _register_slash(on).commands


def _sync_fingerprint(adapter):
    """The command-sync fingerprint of the tree ``_register_slash_commands`` actually builds."""
    registered = _register_slash(adapter).commands
    commands = [SimpleNamespace(to_dict=lambda tree, name=name: {
        "name": name, "description": "", "type": 1, "options": []}) for name in registered]
    adapter._client.tree = SimpleNamespace(get_commands=lambda: commands)
    return adapter._desired_command_sync_fingerprint()


def test_sync_fingerprint_changes_when_the_realtime_command_becomes_available(make_rt_adapter):
    off, _ = make_rt_adapter()
    on, _ = make_rt_adapter(RT)
    assert _sync_fingerprint(off) != _sync_fingerprint(on)


def test_sync_fingerprint_follows_the_realtime_flag_even_with_an_identical_command_set(make_rt_adapter):
    adapter, _ = make_rt_adapter(RT)
    enabled = _sync_fingerprint(adapter)
    adapter._realtime_cfg = dataclasses.replace(adapter._realtime_cfg, enabled=False)
    assert adapter._desired_command_sync_fingerprint() != enabled


def test_an_already_synced_realtime_off_fingerprint_does_not_skip_the_realtime_on_sync(
        make_rt_adapter, tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    off, _ = make_rt_adapter()
    on, _ = make_rt_adapter(RT)
    off_fingerprint, on_fingerprint = _sync_fingerprint(off), _sync_fingerprint(on)
    off._record_command_sync_attempt(999, off_fingerprint)
    off._record_command_sync_success(999, off_fingerprint, {"total": 1})
    assert off._command_sync_skip_reason(999, off_fingerprint) == "same slash-command fingerprint already synced"
    assert on._command_sync_skip_reason(999, on_fingerprint) is None


@pytest.mark.asyncio
async def test_slash_start_opens_immediately_for_the_authorized_user(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    adapter._check_slash_authorization = AsyncMock(return_value=True)
    adapter.handle_realtime_voice_control = AsyncMock(return_value="Realtime canary started")
    callback = _register_slash(adapter).commands["realtime-voice"]
    interaction = _interaction()

    await callback(interaction, action="start")

    adapter.handle_realtime_voice_control.assert_awaited_once_with(interaction, "start")
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.edit_original_response.assert_awaited_once_with(content="Realtime canary started")


@pytest.mark.asyncio
async def test_slash_is_stopped_by_standard_authorization_before_control(make_rt_adapter):
    adapter, _ = make_rt_adapter(RT)
    adapter._check_slash_authorization = AsyncMock(return_value=False)
    adapter.handle_realtime_voice_control = AsyncMock()
    interaction = _interaction()

    await _register_slash(adapter).commands["realtime-voice"](interaction, action="start")

    adapter.handle_realtime_voice_control.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    interaction.response.send_message.assert_not_awaited()



# --- the start boundary: every denial happens before any provider connection -------------------


@pytest.mark.asyncio
async def test_disabled_by_default(make_rt_adapter):
    adapter, vc = make_rt_adapter(control=None)
    _receiver(adapter, vc, os.urandom(32))
    result, provider = await _attempt(adapter)
    assert not result.started and provider.constructed == [] and not adapter.realtime_voice_active(GUILD)


def test_realtime_is_off_without_any_config_and_on_only_for_explicit_active_values():
    from plugins.platforms.discord.adapter_realtime_voice import (
        RealtimeVoiceControl,
        DiscordRealtimeVoiceMixin,
    )

    absent = object.__new__(DiscordRealtimeVoiceMixin)
    with patch("hermes_cli.config.load_config", return_value={"discord": {}}):
        assert absent._load_realtime_voice_config().enabled is False
        assert absent._load_realtime_voice_control_config().enabled is False

    active = {
        "realtime_voice": {"enabled": True},
        "realtime_voice_control": {"enabled": True, "guild_id": GUILD, "user_ids": [ALLOWED_USER]},
    }
    adapter = object.__new__(DiscordRealtimeVoiceMixin)
    with patch("hermes_cli.config.load_config", return_value={"discord": active}):
        adapter._realtime_cfg = adapter._load_realtime_voice_config()
        adapter._realtime_control = RealtimeVoiceControl(
            adapter, adapter._load_realtime_voice_control_config())
    assert adapter._realtime_cfg.enabled is True
    assert adapter._realtime_control.config.enabled is True


@pytest.mark.asyncio
async def test_start_rechecks_realtime_control_config_immediately_before_connecting(make_rt_adapter):
    from plugins.platforms.discord.adapter_realtime_voice import RealtimeVoiceControlConfig

    adapter, vc = make_rt_adapter(RT)
    receiver = _receiver(adapter, vc, os.urandom(32))
    adapter._realtime_control.config = dataclasses.replace(
        adapter._realtime_control.config, enabled=False)
    adapter._load_realtime_voice_control_config = lambda: RealtimeVoiceControlConfig()
    provider = FakeProvider()
    adapter._realtime_transport_factory = lambda cfg: provider(model=cfg.model, api_key_env=cfg.api_key_env)
    result = await adapter.start_realtime_voice(GUILD, requester_id=ALLOWED_USER)
    assert not result.started and provider.constructed == []
    assert receiver.frame_sink is not None and not adapter.realtime_voice_active(GUILD)


@pytest.mark.asyncio
async def test_feature_flag_alone_is_not_enough_without_the_control_gate(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT, control=None)
    _receiver(adapter, vc, os.urandom(32))
    result, provider = await _attempt(adapter)
    assert "not authorized" in result.message and provider.constructed == []


@pytest.mark.asyncio
async def test_control_gate_alone_is_not_enough_without_the_feature_flag(make_rt_adapter):
    adapter, vc = make_rt_adapter(streaming=True)               # control armed, realtime_voice absent
    _receiver(adapter, vc, os.urandom(32))
    result, provider = await _attempt(adapter)
    assert "disabled in config" in result.message and provider.constructed == []


@pytest.mark.asyncio
async def test_needs_a_joined_channel_with_a_receiver(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    result, provider = await _attempt(adapter)
    assert not result.started and "Run /voice join" in result.message and provider.constructed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("requester,guild", [(STRANGER, GUILD), (ALLOWED_USER, GUILD + 1), (str(ALLOWED_USER), GUILD),
                                              (True, GUILD), (None, GUILD)])
async def test_unlisted_or_inexact_requesters_and_guilds_are_denied_at_the_boundary(make_rt_adapter, requester, guild):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    result, provider = await _attempt(adapter, requester=requester, guild=guild)
    assert "not authorized" in result.message and provider.constructed == []
    assert not adapter.realtime_voice_active(GUILD)


@pytest.mark.asyncio
async def test_start_accepts_no_test_collaborators_that_could_skip_authorization(make_rt_adapter):
    adapter, vc = make_rt_adapter(control=None, realtime=RT)
    _receiver(adapter, vc, os.urandom(32))
    for injected in ({"connect": AsyncMock()}, {"delegate": object()}, {"clock": time.monotonic},
                     {"config_override": adapter._realtime_cfg}, {"daily_budget": object()},
                     {"_control_auth": (GUILD, ALLOWED_USER)}):
        with pytest.raises(TypeError):
            await adapter.start_realtime_voice(GUILD, requester_id=ALLOWED_USER, **injected)
    assert not adapter.realtime_voice_active(GUILD)


@pytest.mark.asyncio
async def test_concurrent_explicit_starts_open_exactly_one_session(make_rt_adapter):
    other = ALLOWED_USER + 1
    adapter, vc = make_rt_adapter(RT, control={**CONTROL, "user_ids": [ALLOWED_USER, other]})
    _receiver(adapter, vc, os.urandom(32))
    gate = asyncio.Event()
    provider = FakeProvider()
    class _Transport:
        async def connect(self):
            await gate.wait()
            return provider.transport

    adapter._realtime_transport_factory = lambda _cfg: _Transport()
    first = asyncio.ensure_future(adapter.start_realtime_voice(GUILD, requester_id=ALLOWED_USER))
    try:
        for _ in range(100):
            if GUILD in adapter._realtime_starting:
                break
            await asyncio.sleep(0)
        second = await adapter.start_realtime_voice(GUILD, requester_id=other)
        assert not second.started and "already active" in second.message
        gate.set()
        result = await first
        assert result.started and adapter.realtime_voice_active(GUILD)
    finally:
        gate.set()
        if not first.done():
            await first
        del adapter._realtime_transport_factory
        await adapter.stop_realtime_voice(GUILD)







@pytest.mark.asyncio
async def test_another_authorized_user_starts_only_with_their_own_explicit_action(make_rt_adapter):
    other = ALLOWED_USER + 1
    adapter, vc = make_rt_adapter(RT, control={**CONTROL, "user_ids": [ALLOWED_USER, other]})
    _receiver(adapter, vc, os.urandom(32))
    result, provider = await _attempt(adapter, requester=other)
    assert result.started and len(provider.constructed) == 1
    await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_channel_is_checked_at_start_time(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    receiver = _receiver(adapter, vc, os.urandom(32))
    receiver._running = False
    result, provider = await _attempt(adapter)
    assert not result.started and "Run /voice join" in result.message and provider.constructed == []


@pytest.mark.asyncio
async def test_session_config_and_caps_are_derived_at_start_time(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    adapter._realtime_cfg = dataclasses.replace(adapter._realtime_cfg, model="another-model")
    result, provider = await _attempt(adapter)
    assert result.started and provider.constructed == [("another-model", "OPENAI_API_KEY")]
    await adapter.stop_realtime_voice(GUILD)

@pytest.mark.asyncio
async def test_budget_is_enforced_at_start_time(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    adapter._realtime_control.budget.limit_s = 0
    result, provider = await _attempt(adapter)
    assert not result.started and "budget is exhausted" in result.message and provider.constructed == []


@pytest.mark.asyncio
async def test_each_concurrent_start_is_authorized_and_only_one_opens(make_rt_adapter):
    other = ALLOWED_USER + 1
    adapter, vc = make_rt_adapter(RT, control={**CONTROL, "user_ids": [ALLOWED_USER, other]})
    _receiver(adapter, vc, os.urandom(32))
    gate = asyncio.Event()
    provider = FakeProvider()

    class _Slow:
        async def connect(self):
            await gate.wait()
            return provider.transport

    def slow_factory(*, model, api_key_env):
        provider.constructed.append((model, api_key_env))
        class _Transport:
            async def connect(self):
                await gate.wait()
                return provider.transport
        return _Transport()

    adapter._realtime_transport_factory = lambda cfg: slow_factory(model=cfg.model, api_key_env=cfg.api_key_env)
    first_task = asyncio.ensure_future(adapter.start_realtime_voice(GUILD, requester_id=ALLOWED_USER))
    try:
        for _ in range(100):
            if GUILD in adapter._realtime_starting:
                break
            await asyncio.sleep(0)
        second = await adapter.start_realtime_voice(GUILD, requester_id=other)
        gate.set()
        first = await first_task
        assert first.started and not second.started and len(provider.constructed) == 1
    finally:
        gate.set()
        if not first_task.done():
            await first_task
        del adapter._realtime_transport_factory
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_a_missing_provider_key_reports_failure_without_leaving_a_session(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    receiver = _receiver(adapter, vc, os.urandom(32))
    sink_before = receiver.frame_sink
    provider = FakeProvider(unavailable=True)
    class _Unavailable:
        async def connect(self):
            raise RealtimeUnavailable("no key")
    adapter._realtime_transport_factory = lambda _cfg: _Unavailable()
    try:
        result = await adapter.start_realtime_voice(GUILD, requester_id=ALLOWED_USER)
    finally:
        del adapter._realtime_transport_factory
    assert not result.started and "API key" in result.message
    assert not adapter.realtime_voice_active(GUILD) and receiver.frame_sink is sink_before


@pytest.mark.asyncio
async def test_started_session_is_capped_and_only_the_control_allowlist_may_speak(make_rt_adapter):
    adapter, vc = make_rt_adapter({**RT, "max_session_seconds": 3600, "daily_audio_budget_seconds": 7200})
    _receiver(adapter, vc, os.urandom(32))
    ok, _ = await _start(adapter)
    try:
        session = adapter._realtime_sessions[GUILD]
        assert ok and session.cfg.max_session_s == 600 and session.budget.limit_s == 1800
        assert session._allowed_speakers == frozenset({ALLOWED_USER})
        assert session.budget is adapter._realtime_control.budget
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_the_control_gate_is_not_loosened_by_gateway_open_access(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    adapter._allowed_user_ids = {"*"}                     # gateway-wide open mode
    ok, _ = await _start(adapter)
    try:
        session = adapter._realtime_sessions[GUILD]
        assert ok and session._allowed(STRANGER, time.monotonic()) is False
        assert session._allowed(ALLOWED_USER, time.monotonic()) is True
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_gateway_revocation_narrows_the_control_allowlist(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    ok, _ = await _start(adapter)
    adapter._allowed_user_ids = {str(STRANGER)}            # the gateway no longer lists the speaker
    try:
        session = adapter._realtime_sessions[GUILD]
        # A verdict is re-asked once it is LIVE_AUTH_TTL_S old, which bounds the revocation lag.
        assert ok and session._allowed(ALLOWED_USER, time.monotonic() + session.LIVE_AUTH_TTL_S + 0.01) is False
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_real_opus_ingress_reaches_the_model_and_receiver_stays_unpaused(make_rt_adapter, opus):
    adapter, vc = make_rt_adapter(RT)
    key = os.urandom(32)
    receiver = _receiver(adapter, vc, key)
    receiver.pause()                                 # as the legacy echo guard would leave it
    with patch("hermes_cli.config.load_config", return_value={"discord": {
        "realtime_voice": RT,
        "realtime_voice_control": CONTROL,
    }}):
        provider = FakeProvider()
        adapter._realtime_transport_factory = lambda cfg: provider(model=cfg.model, api_key_env=cfg.api_key_env)
        ok, transport = await _start(adapter)
    try:
        assert ok and adapter.realtime_voice_active(GUILD) and receiver._paused is False
        enc = opus.Encoder()
        user = [_rtp(key, USER_SSRC, s, enc.encode(_tone(s), 960)) for s in range(1, 26)]
        stranger = [_rtp(key, STRANGER_SSRC, s, enc.encode(_tone(s, 900), 960)) for s in range(1, 26)]
        await _feed(receiver, stranger)
        await asyncio.sleep(0.1)
        assert transport.of_type("input_audio_buffer.append") == []   # a stranger never leaves the box
        await _feed(receiver, user)
        await asyncio.sleep(0.15)
        pcm = transport.appended_pcm()
        samples = array.array("h", pcm)
        if sys.byteorder == "big":
            samples.byteswap()
        assert len(samples) >= 20 * 480                                  # 24 kHz mono, ~25 frames
        voiced = samples[: 20 * 480]
        assert _goertzel(voiced, 300, 24000) > 50 * _goertzel(voiced, 900, 24000)
        session = adapter._realtime_sessions[GUILD]
        assert session.counters["frames_dropped_unallowed"] == 25
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_whole_utterance_stt_is_suppressed_while_realtime_is_on(make_rt_adapter, opus):
    adapter, vc = make_rt_adapter(RT)
    key = os.urandom(32)
    receiver = _receiver(adapter, vc, key)
    ok, _ = await _start(adapter)
    adapter._process_voice_input = AsyncMock()
    try:
        enc = opus.Encoder()
        await _feed(receiver, [_rtp(key, USER_SSRC, s, enc.encode(_tone(s), 960)) for s in range(1, 60)])
        receiver._last_packet_time = {USER_SSRC: time.monotonic() - 5}   # long since silent
        loop_task = asyncio.ensure_future(adapter._voice_listen_loop(GUILD))
        await asyncio.sleep(0.5)
        loop_task.cancel()
        adapter._process_voice_input.assert_not_awaited()
        assert not receiver._buffers.get(USER_SSRC)
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_model_audio_plays_as_discord_frames_that_opus_encodes(make_rt_adapter, opus):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    provider = FakeProvider()
    adapter._realtime_transport_factory = lambda cfg: provider(model=cfg.model, api_key_env=cfg.api_key_env)
    assert adapter.supports_streaming_tts(CHAT, AudioFormat(24000, 1, 2))
    assert vc.is_connected() and adapter._voice_text_channels[GUILD] == TEXT_CHANNEL
    with patch("hermes_cli.config.load_config", return_value={"discord": {
        "realtime_voice": RT,
        "realtime_voice_control": CONTROL,
    }}):
        ok, transport = await _start(adapter)
    session = adapter._realtime_sessions[GUILD]
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(24000 // 5, 3000)))   # 200 ms
        await session.handle_event({"type": "response.output_audio.done", "response_id": "r1"})
        assert vc.wait_idle(2.0)
        speech = [f for f in vc.sent if any(f)]
        assert len(speech) == 10 and all(len(f) == 3840 for f in vc.sent)
        enc, dec = opus.Encoder(), opus.Decoder()
        for frame in speech:
            assert len(dec.decode(enc.encode(frame, 960))) == 3840
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_hermes_reply_takes_the_floor(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    provider = FakeProvider()
    adapter._realtime_transport_factory = lambda cfg: provider(model=cfg.model, api_key_env=cfg.api_key_env)
    assert adapter.supports_streaming_tts(CHAT, AudioFormat(24000, 1, 2))
    assert vc.is_connected() and adapter._voice_text_channels[GUILD] == TEXT_CHANNEL
    with patch("hermes_cli.config.load_config", return_value={"discord": {
        "realtime_voice": RT,
        "realtime_voice_control": CONTROL,
    }}):
        ok, transport = await _start(adapter)
    session = adapter._realtime_sessions[GUILD]
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(24000 * 3)))         # 3 s of realtime speech
        rt_queue = adapter._realtime_playback[GUILD].current
        handle = await adapter.begin_streaming_tts(CHAT, AudioFormat(24000, 1, 2))
        assert handle is not None                                           # Hermes got the player
        assert rt_queue.cut_reason == "hermes-speaking"
        assert GUILD in adapter._realtime_hermes_playing
        assert session._floor_blocked
        await asyncio.sleep(0)
        truncations = transport.of_type("conversation.item.truncate")
        assert truncations[-1]["item_id"] == "item_1"
        assert truncations[-1]["audio_end_ms"] <= 3000
        assert transport.of_type("response.cancel")[-1]["response_id"] == "r1"
        await session.handle_event({"type": "response.created", "response": {"id": "r2"}})
        await session.handle_event(delta("r2", mono24(2400)))
        assert session.counters["player_busy"] == 1                          # no talking over Hermes
        await asyncio.sleep(0)
        assert transport.of_type("response.cancel")[-1]["response_id"] == "r2"
        await adapter.finish_streaming_tts(handle, interrupted=True)
        session._active_response = "r3"
        await adapter._realtime_hermes_floor(GUILD, True)                   # a Hermes reply file starts
        await asyncio.sleep(0)
        assert adapter._realtime_playback[GUILD].output_blocked()
        assert transport.of_type("response.cancel")[-1]["response_id"] == "r3"
        await adapter._realtime_hermes_floor(GUILD, False)
        assert not adapter._realtime_playback[GUILD].output_blocked()
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_gateway_delegate_uses_the_spoken_input_entry_and_completion_hook(make_rt_adapter):
    from plugins.platforms.discord.adapter_realtime_voice import GatewayVoiceDelegate

    adapter, vc = make_rt_adapter(RT)
    adapter._voice_input_callback = AsyncMock()
    adapter._record_discord_processing_complete = MagicMock()
    delegate = GatewayVoiceDelegate(adapter, GUILD)
    job = asyncio.ensure_future(delegate.run(DelegationRequest("h1", GUILD, ALLOWED_USER, "check my calendar")))
    await asyncio.sleep(0)
    adapter._voice_input_callback.assert_awaited_once_with(
        guild_id=GUILD, user_id=ALLOWED_USER, transcript="check my calendar",
        resume_chat_session=True, on_event_created=ANY)
    other = SimpleNamespace(raw_message=SimpleNamespace(guild_id=GUILD), text="something else",
                            message_type=MessageType.VOICE)
    await adapter.on_processing_complete(other, ProcessingOutcome.SUCCESS)
    assert not job.done()
    event = SimpleNamespace(user_id=str(ALLOWED_USER), raw_message=SimpleNamespace(guild_id=GUILD),
                            text="check my calendar", message_type=MessageType.VOICE)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    result = await asyncio.wait_for(job, 1)
    assert result.status == "done" and result.delivered_by == "hermes"
    assert adapter._realtime_pending_jobs.get(GUILD, []) == []


@pytest.mark.asyncio
async def test_concurrent_delegations_each_capture_their_own_event_without_rebinding_the_callback(make_rt_adapter):
    """The shared voice-input callback is never mutated: each call carries its own capture hook, so
    two overlapping jobs (or an unrelated voice input) cannot be mistaken for each other."""
    import functools

    from plugins.platforms.discord.adapter_realtime_voice import GatewayVoiceDelegate

    adapter, _ = make_rt_adapter(RT)
    seen = []

    async def gateway_entry(guild_id, user_id, transcript, *, adapter=None, resume_chat_session=False,
                            on_event_created=None):
        await asyncio.sleep(0)                               # let the other job interleave
        event = SimpleNamespace(text=transcript, raw_message=SimpleNamespace(guild_id=guild_id),
                                message_type=MessageType.VOICE, allow_gateway_control=True, metadata={})
        seen.append(event)
        on_event_created(event)

    callback = functools.partial(gateway_entry, adapter=adapter)
    adapter._voice_input_callback = callback
    adapter._record_discord_processing_complete = MagicMock()
    delegate = GatewayVoiceDelegate(adapter, GUILD)
    one = asyncio.ensure_future(delegate.run(DelegationRequest("h1", GUILD, ALLOWED_USER, "first request")))
    two = asyncio.ensure_future(delegate.run(DelegationRequest("h2", GUILD, ALLOWED_USER, "second request")))
    for _ in range(5):
        await asyncio.sleep(0)

    assert callback.keywords == {"adapter": adapter}          # untouched
    assert all(event.allow_gateway_control is False for event in seen)
    by_text = {e.text: e for e in seen}
    keys = {text: e.metadata["realtime_delegate_event_key"] for text, e in by_text.items()}
    assert all(keys.values()) and len(set(keys.values())) == 2     # the delegate stamps a unique key per event
    assert {j.event_key for j in adapter._realtime_pending_jobs[GUILD]} == set(keys.values())
    await adapter.on_processing_complete(by_text["second request"], ProcessingOutcome.SUCCESS)
    assert (await asyncio.wait_for(two, 1)).status == "done" and not one.done()
    await adapter.on_processing_complete(by_text["first request"], ProcessingOutcome.FAILURE)
    assert (await asyncio.wait_for(one, 1)).status == "failed"


@pytest.mark.asyncio
async def test_pending_delegate_matching_ignores_identical_text_from_another_speaker(make_rt_adapter):
    from plugins.platforms.discord.adapter_realtime_voice import GatewayVoiceDelegate

    adapter, _ = make_rt_adapter(RT)
    adapter._voice_input_callback = AsyncMock()
    delegate = GatewayVoiceDelegate(adapter, GUILD)
    job = asyncio.ensure_future(delegate.run(DelegationRequest("h1", GUILD, ALLOWED_USER, "same request")))
    await asyncio.sleep(0)
    wrong = SimpleNamespace(raw_message=SimpleNamespace(guild_id=GUILD, guild=None,
                                                        author=SimpleNamespace(id=ALLOWED_USER + 1)),
                            text="same request", message_type=MessageType.VOICE)
    await adapter.on_processing_complete(wrong, ProcessingOutcome.SUCCESS)
    assert not job.done()
    correct = SimpleNamespace(raw_message=SimpleNamespace(guild_id=GUILD, guild=None,
                                                          author=SimpleNamespace(id=ALLOWED_USER)),
                               text="same request", message_type=MessageType.VOICE)
    await adapter.on_processing_complete(correct, ProcessingOutcome.SUCCESS)
    result = await asyncio.wait_for(job, 1)
    assert result.status == "done"


@pytest.mark.asyncio
async def test_production_adapter_cuts_realtime_playback_and_truncates_heard_audio(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    await _start(adapter)
    session = adapter._realtime_sessions[GUILD]
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(24000), item="item_A"))
        queue = adapter._realtime_playback[GUILD].current
        assert queue is not None
        time.sleep(0.1)  # allow a few real player-thread frames to leave the queue
        await session.handle_event({"type": "input_audio_buffer.speech_started"})
        assert queue.cut_reason == "barge-in"
        truncations = session._transport.of_type("conversation.item.truncate")
        assert len(truncations) == 1
        assert truncations[0]["item_id"] == "item_A"
        assert 0 < truncations[0]["audio_end_ms"] < 1000
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_leaving_closes_the_session_and_skips_stt_flush(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    receiver = _receiver(adapter, vc, os.urandom(32))
    with patch("hermes_cli.config.load_config", return_value={"discord": {
        "realtime_voice": RT,
        "realtime_voice_control": CONTROL,
    }}):
        provider = FakeProvider()
        adapter._realtime_transport_factory = lambda cfg: provider(model=cfg.model, api_key_env=cfg.api_key_env)
        ok, transport = await _start(adapter)
    receiver.flush_pending = MagicMock(return_value=[])
    await adapter.leave_voice_channel(GUILD)
    assert transport.closed and not adapter.realtime_voice_active(GUILD)
    receiver.flush_pending.assert_not_called()


@pytest.mark.asyncio
async def test_loopback_through_discordpy_packetizer_and_hermes_receiver(make_rt_adapter, opus):
    """Model audio -> FrameQueue -> libopus -> discord.py ``_get_voice_packet`` (DAVE hook + NaCl
    rtpsize) -> Hermes ``VoiceReceiver._on_packet`` (NaCl + DAVE hook + libopus) -> realtime ingress."""
    from plugins.platforms.discord.adapter_realtime_voice import DiscordRealtimePlayback

    pytest.importorskip("davey")      # the receiver passes davey.MediaType.audio to the DAVE session
    adapter, vc = make_rt_adapter(RT)
    key = os.urandom(32)
    receiver = _receiver(adapter, vc, key)

    class RecordingDave:          # stand-in: a real DaveSession needs Discord's MLS external sender
        ready = True

        def __init__(self):
            self.encrypted = self.decrypted = 0

        def encrypt_opus(self, packet):
            self.encrypted += 1
            return b"DAVE" + packet

        def decrypt(self, user_id, media_type, packet):
            assert packet[:4] == b"DAVE"
            self.decrypted += 1
            return packet[4:]

    dave = RecordingDave()
    sender = object.__new__(discord.VoiceClient)
    sender._connection = SimpleNamespace(ssrc=USER_SSRC, mode="aead_xchacha20_poly1305_rtpsize",
                                         secret_key=list(key), dave_session=dave, can_encrypt=True)
    sender.sequence, sender.timestamp, sender._incr_nonce = 0, 0, 0
    receiver._creds = (key, dave, 1, False)                 # DAVE protocol 1 on the receive side

    with patch("hermes_cli.config.load_config", return_value={"discord": {
        "realtime_voice": RT,
        "realtime_voice_control": CONTROL,
    }}):
        provider = FakeProvider()
        adapter._realtime_transport_factory = lambda cfg: provider(model=cfg.model, api_key_env=cfg.api_key_env)
        ok, transport = await _start(adapter)
    turn = DiscordRealtimePlayback(adapter, GUILD).begin(100)
    tone = array.array("h", (int(6000 * math.sin(2 * math.pi * 300 * i / 24000)) for i in range(24000 // 2)))
    if sys.byteorder == "big":
        tone.byteswap()
    from plugins.platforms.discord.voice_stream import PCMFramer
    for frame in PCMFramer(24000, 1).feed(tone.tobytes()):
        assert turn.queue.offer(frame)
    turn.queue.close()
    assert vc.wait_idle(2.0)                                # the player pulled every frame
    enc = opus.Encoder()
    packets = []
    for frame in vc.sent:
        sender.sequence += 1
        sender.timestamp += 960
        packets.append(sender._get_voice_packet(enc.encode(frame, 960)))
    assert len(packets) >= 25
    try:
        await _feed(receiver, packets)
        await asyncio.sleep(0.15)
        assert dave.encrypted == len(packets) and dave.decrypted == len(packets)
        assert receiver._decode_failed == 0 and receiver._decode_ok == len(packets)
        samples = array.array("h", transport.appended_pcm())
        if sys.byteorder == "big":
            samples.byteswap()
        assert len(samples) >= 20 * 480
        voiced = samples[-20 * 480:]
        assert _goertzel(voiced, 300, 24000) > 50 * _goertzel(voiced, 1200, 24000)
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_a_request_hermes_silently_drops_frees_the_slot_and_is_reported(make_rt_adapter):
    """``_handle_voice_channel_input`` returns without a turn for a duplicate transcript, an
    unauthorized speaker or an unbound channel; the job must not hold the only slot forever."""
    from plugins.platforms.discord.adapter_realtime_voice import GatewayVoiceDelegate
    from plugins.platforms.discord.realtime_voice import DelegationBridge, RealtimeVoiceConfig

    adapter, vc = make_rt_adapter(RT)
    adapter._realtime_cfg = RealtimeVoiceConfig(enabled=True, delegate_admit_s=0.05)
    adapter._voice_input_callback = AsyncMock()              # accepted the call, started nothing
    done = []
    bridge = DelegationBridge(GatewayVoiceDelegate(adapter, GUILD), adapter._realtime_cfg,
                              on_complete=lambda job, result: done.append(result))
    assert bridge.submit(GUILD, ALLOWED_USER, "what is on my calendar")["status"] == "started"
    assert bridge.submit(GUILD, ALLOWED_USER, "and tomorrow")["reason"] == "busy"
    for _ in range(50):
        await asyncio.sleep(0.01)
        if done:
            break
    await asyncio.sleep(0)
    assert done[0].status == "not-admitted" and done[0].delivered_by == "voice"
    assert bridge.inflight == 0 and adapter._realtime_pending_jobs.get(GUILD, []) == []
    assert bridge.submit(GUILD, ALLOWED_USER, "and tomorrow")["status"] == "started"


@pytest.mark.asyncio
async def test_an_admitted_job_releases_its_slot_at_the_hard_cap(make_rt_adapter):
    from plugins.platforms.discord.adapter_realtime_voice import GatewayVoiceDelegate
    from plugins.platforms.discord.realtime_voice import RealtimeVoiceConfig

    adapter, vc = make_rt_adapter(RT)
    adapter._realtime_cfg = RealtimeVoiceConfig(enabled=True, delegate_admit_s=1, delegate_slot_max_s=0.05)
    adapter._voice_input_callback = AsyncMock()
    adapter._record_discord_processing_start = MagicMock()
    job = asyncio.ensure_future(GatewayVoiceDelegate(adapter, GUILD).run(
        DelegationRequest("h1", GUILD, ALLOWED_USER, "deploy the thing")))
    await asyncio.sleep(0)
    event = SimpleNamespace(user_id=str(ALLOWED_USER), raw_message=SimpleNamespace(guild_id=GUILD, add_reaction=None),
                            text="deploy the thing", message_type=MessageType.VOICE)
    with patch.object(type(adapter), "_reactions_enabled", return_value=False):
        await adapter.on_processing_start(event)            # Hermes admitted it ...
    result = await asyncio.wait_for(job, 1)                 # ... and never finished (e.g. approval wait)
    assert result.status == "timeout" and result.delivered_by == "hermes"
    assert adapter._realtime_pending_jobs.get(GUILD, []) == []


@pytest.mark.asyncio
async def test_the_production_sink_queues_allowed_frames_and_rejects_a_revoked_speaker_first(make_rt_adapter):
    from plugins.platforms.discord.realtime_voice import RealtimeVoiceSession

    adapter, vc = make_rt_adapter({**RT, "ingress_batch_ms": 1000})   # keep frames in the queue
    receiver = _receiver(adapter, vc, os.urandom(32))
    ok, _ = await _start(adapter)
    session = adapter._realtime_sessions[GUILD]
    try:
        await asyncio.sleep(0.15)                       # no fresh control recheck is needed per frame
        receiver.frame_sink(ALLOWED_USER, _tone(1))
        assert ok and [user for user, _pcm, _at in session._ingress] == [ALLOWED_USER]
        session._ingress.clear()
        adapter._allowed_user_ids = {str(STRANGER)}      # the gateway revokes the speaker
        await session.pump_once(time.monotonic() + RealtimeVoiceSession.LIVE_AUTH_TTL_S + 0.01)
        receiver.frame_sink(ALLOWED_USER, _tone(2))
        assert len(session._ingress) == 0
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_a_finished_streamed_hermes_reply_hands_the_floor_back_to_realtime(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    provider = FakeProvider()
    adapter._realtime_transport_factory = lambda cfg: provider(model=cfg.model, api_key_env=cfg.api_key_env)
    assert adapter.supports_streaming_tts(CHAT, AudioFormat(24000, 1, 2))
    assert vc.is_connected() and adapter._voice_text_channels[GUILD] == TEXT_CHANNEL
    with patch("hermes_cli.config.load_config", return_value={"discord": {
        "realtime_voice": RT,
        "realtime_voice_control": CONTROL,
    }}):
        ok, transport = await _start(adapter)
    session = adapter._realtime_sessions[GUILD]
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(24000)))
        handle = await adapter.begin_streaming_tts(CHAT, AudioFormat(24000, 1, 2))
        assert handle is not None and adapter._realtime_playback[GUILD].output_blocked()
        await adapter.finish_streaming_tts(handle)
        assert handle.released
        assert not adapter._realtime_playback[GUILD].output_blocked()
        assert not session._floor_blocked and GUILD not in adapter._realtime_hermes_playing
        assert "r1" in session._hermes_interrupted_responses
        busy = session.counters["player_busy"]
        await session.handle_event({"type": "response.created", "response": {"id": "r2"}})
        vc.stop()  # The streamed reply ended; Hermes no longer owns the Discord player.
        await session.handle_event(delta("r2", mono24(2400)))
        assert session.counters["player_busy"] == busy    # realtime may speak again
        assert session._out is not None and session._out.response_id == "r2"
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_overlapping_hermes_floors_keep_realtime_blocked_until_the_last_one_ends(make_rt_adapter):
    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    await _start(adapter)
    playback = adapter._realtime_playback[GUILD]
    try:
        await adapter._realtime_hermes_floor(GUILD, True)
        await adapter._realtime_hermes_floor(GUILD, True)
        await adapter._realtime_hermes_floor(GUILD, False)
        assert playback.output_blocked()
        await adapter._realtime_hermes_floor(GUILD, False)
        assert not playback.output_blocked()
    finally:
        await adapter.stop_realtime_voice(GUILD)


class _FileSource(discord.AudioSource):
    """Stands in for the FFmpeg-decoded reply file: a fixed number of 20 ms PCM frames."""

    def __init__(self, frames):
        self.left, self.reads = frames, 0

    def read(self):
        if self.left <= 0:
            return b""
        self.left -= 1
        self.reads += 1
        return b"\x01\x00" * 1920


async def _fixed_timeout(_path):
    return 5


@pytest.mark.asyncio
async def test_a_whole_file_hermes_reply_takes_the_direct_player_from_realtime_and_hands_it_back(make_rt_adapter):
    from plugins.platforms.discord import adapter as adapter_mod

    adapter, vc = make_rt_adapter(RT)
    receiver = _receiver(adapter, vc, os.urandom(32))
    ok, _ = await _start(adapter)
    session, playback = adapter._realtime_sessions[GUILD], adapter._realtime_playback[GUILD]
    adapter._playback_timeout_for_audio = _fixed_timeout
    reply = _FileSource(frames=150)
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(24000 * 3)))
        rt_queue, rt_source = playback.current, playback._source
        assert ok and vc.source is rt_source
        with patch.object(adapter_mod.discord, "FFmpegPCMAudio", return_value=reply), \
                patch.object(adapter_mod, "resolve_ffmpeg_executable", return_value="ffmpeg"):
            task = asyncio.ensure_future(adapter.play_in_voice_channel(GUILD, "reply.mp3"))
            await asyncio.sleep(0.05)
            assert rt_queue.cut_reason == "hermes-speaking"
            assert vc.source is not rt_source and vc.is_playing()            # the reply owns the player
            busy = session.counters["player_busy"]
            await session.handle_event({"type": "response.created", "response": {"id": "r2"}})
            vc.playing = False  # Hermes stream playback completed; receiver may own the player again
            await session.handle_event(delta("r2", mono24(2400)))
            assert session.counters["player_busy"] == busy + 1                # no talking over Hermes
            assert receiver._paused is False                                  # still full duplex
            assert await asyncio.wait_for(task, 3) is True
        assert reply.reads == 150
        assert not playback.output_blocked() and not session._floor_blocked
        assert GUILD not in adapter._realtime_hermes_playing
    finally:
        await adapter.stop_realtime_voice(GUILD)


@pytest.mark.asyncio
async def test_a_whole_file_hermes_reply_on_the_mixer_cuts_only_realtime_audio(make_rt_adapter):
    pytest.importorskip("numpy")
    from plugins.platforms.discord.adapter import _voice_mixer_module

    adapter, vc = make_rt_adapter(RT)
    _receiver(adapter, vc, os.urandom(32))
    mixer = _voice_mixer_module().VoiceMixer()
    vc.play(mixer)
    adapter._voice_mixers[GUILD] = mixer
    ok, _ = await _start(adapter)
    session, playback = adapter._realtime_sessions[GUILD], adapter._realtime_playback[GUILD]
    adapter._playback_timeout_for_audio = _fixed_timeout
    clip = b"\x01\x00" * 1920 * 10
    try:
        await session.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await session.handle_event(delta("r1", mono24(24000 * 3)))
        rt_queue = playback.current
        assert ok and rt_queue is not None
        with patch.object(_voice_mixer_module(), "decode_to_pcm", return_value=clip):
            assert await asyncio.wait_for(adapter.play_in_voice_channel(GUILD, "reply.mp3"), 3) is True
        assert rt_queue.cut_reason == "hermes-speaking"
        assert vc.source is mixer and vc.is_playing()                          # the mixer is never stopped
        assert not playback.output_blocked() and not session._floor_blocked
    finally:
        await adapter.stop_realtime_voice(GUILD)
