"""Offline contract tests for the realtime voice control gate: exact authorization and
how ``execute`` routes an explicit slash start to the adapter boundary (tested end to end in
``test_realtime_voice_discord.py``)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.discord.adapter_realtime_voice import (
    RealtimeVoiceControl,
    RealtimeVoiceControlConfig,
    StartResult,
)

GUILD, USER, OTHER = 41001, 70001, 70002


class _Session:
    closed = False


class _Adapter:
    def __init__(self):
        self._realtime_sessions = {}
        self.start_realtime_voice = AsyncMock(return_value=StartResult(True, "started"))
        self.stop_realtime_voice = AsyncMock()


def _control(adapter=None, *, users=(USER,), clock=None):
    config = RealtimeVoiceControlConfig(enabled=True, guild_id=GUILD, user_ids=frozenset(users))
    return RealtimeVoiceControl(adapter or _Adapter(), config, **({"clock": clock} if clock else {}))


# --- config: exact IDs, anything else disables the control ---------------------------------------


def test_control_config_is_off_by_default():
    assert RealtimeVoiceControlConfig.from_mapping(None).enabled is False
    assert RealtimeVoiceControlConfig.from_mapping({}).enabled is False
    assert RealtimeVoiceControlConfig.from_mapping({"enabled": "true", "guild_id": GUILD, "user_ids": [USER]}).enabled is False


def test_control_config_accepts_exact_ids_as_ints_or_ascii_decimal_strings():
    config = RealtimeVoiceControlConfig.from_mapping(
        {"enabled": True, "guild_id": str(GUILD), "user_ids": [USER, str(OTHER)]})
    assert config.enabled and config.guild_id == GUILD and config.user_ids == {USER, OTHER}
    assert config.max_session_s == 600 and config.daily_audio_budget_s == 1800


@pytest.mark.parametrize("user_ids", [
    [],                       # empty is never "everyone"
    "70001",                  # not a list
    None,
    ["*"],
    ["alice"],
    ["<@70001>"],
    [70001.0],
    [True],
    [" 70001"],
    ["+70001"],
    ["-70001"],
    ["070001"],               # leading zero: not the canonical ID
    ["７０００１"],            # fullwidth digits: int() would accept them
    ["²"],
    [0],
    [1 << 64],
    [USER, "oops"],           # one bad entry voids the whole list
])
def test_control_config_rejects_anything_that_is_not_an_exact_numeric_user_id(user_ids):
    config = RealtimeVoiceControlConfig.from_mapping({"enabled": True, "guild_id": GUILD, "user_ids": user_ids})
    assert config.enabled is False and config.user_ids == frozenset()


@pytest.mark.parametrize("guild_id", [None, "*", "guild", 1.5, True, 0, "００１"])
def test_control_config_rejects_an_inexact_guild_id(guild_id):
    assert RealtimeVoiceControlConfig.from_mapping(
        {"enabled": True, "guild_id": guild_id, "user_ids": [USER]}).enabled is False


@pytest.mark.parametrize("limits", [
    {"max_session_seconds": 601}, {"daily_audio_budget_seconds": 1801},
    {"max_session_seconds": 0}, {"max_session_seconds": "soon"}, {"max_session_seconds": True},
    {"daily_audio_budget_seconds": float("nan")},
])
def test_control_config_rejects_limits_above_the_canary_caps_or_malformed(limits):
    assert RealtimeVoiceControlConfig.from_mapping(
        {"enabled": True, "guild_id": GUILD, "user_ids": [USER], **limits}).enabled is False


def test_control_config_limits_can_only_tighten_the_caps():
    config = RealtimeVoiceControlConfig.from_mapping({
        "enabled": True, "guild_id": GUILD, "user_ids": [USER],
        "max_session_seconds": 30, "daily_audio_budget_seconds": 120})
    assert (config.enabled, config.max_session_s, config.daily_audio_budget_s) == (True, 30.0, 120.0)


# --- authorization is exact -------------------------------------------------------------------


@pytest.mark.parametrize("guild,user,allowed", [
    (GUILD, USER, True), (GUILD + 1, USER, False), (GUILD, OTHER, False),
    (str(GUILD), USER, False), (GUILD, str(USER), False), (GUILD, True, False), (GUILD, None, False),
])
def test_authorized_needs_the_exact_guild_and_a_listed_user(guild, user, allowed):
    assert _control().authorized(guild, user) is allowed


def test_a_disabled_control_authorizes_nobody():
    control = RealtimeVoiceControl(_Adapter(), RealtimeVoiceControlConfig())
    assert control.authorized(GUILD, USER) is False


# --- explicit slash action routes only after exact authorization ----------------------------------


@pytest.mark.asyncio
async def test_execute_is_disabled_without_an_armed_config():
    adapter = _Adapter()
    control = RealtimeVoiceControl(adapter, RealtimeVoiceControlConfig.from_mapping({}))

    assert await control.execute(guild_id=GUILD, user_id=USER, action="start") == "Realtime canary control is disabled."
    adapter.start_realtime_voice.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "status", "stop"])
async def test_execute_refuses_every_action_for_an_unlisted_user_or_guild(action):
    adapter = _Adapter()
    control = _control(adapter)

    for guild, user in ((GUILD + 1, USER), (GUILD, OTHER)):
        result = await control.execute(guild_id=guild, user_id=user, action=action)
        assert result == "Realtime canary control is not authorized here."
    adapter.start_realtime_voice.assert_not_awaited()
    adapter.stop_realtime_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_start_routes_requester_to_session_boundary():
    adapter = _Adapter()

    result = await _control(adapter).execute(guild_id=GUILD, user_id=USER, action="start")

    assert result == "started"
    adapter.start_realtime_voice.assert_awaited_once_with(GUILD, requester_id=USER)


@pytest.mark.asyncio
async def test_status_reports_inactive_without_connecting():
    adapter = _Adapter()

    result = await _control(adapter).execute(guild_id=GUILD, user_id=USER, action="status")

    assert "inactive" in result
    adapter.start_realtime_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_reports_active_for_an_open_session():
    adapter = _Adapter()
    adapter._realtime_sessions[GUILD] = _Session()

    assert "is active" in await _control(adapter).execute(guild_id=GUILD, user_id=USER, action="status")


@pytest.mark.asyncio
async def test_stop_only_closes_the_authorized_guild_session():
    adapter = _Adapter()
    adapter._realtime_sessions[GUILD] = _Session()

    result = await _control(adapter).execute(guild_id=GUILD, user_id=USER, action="stop")

    assert "stopped" in result
    adapter.stop_realtime_voice.assert_awaited_once_with(GUILD, "stopped")


@pytest.mark.asyncio
async def test_an_unknown_action_does_nothing():
    adapter = _Adapter()

    assert "Unknown" in await _control(adapter).execute(guild_id=GUILD, user_id=USER, action="yolo")
    adapter.start_realtime_voice.assert_not_awaited()
    adapter.stop_realtime_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_without_a_guild_or_user_is_not_authorized():
    from plugins.platforms.discord.adapter_realtime_voice import DiscordRealtimeVoiceMixin

    mixin = DiscordRealtimeVoiceMixin()
    mixin._realtime_control = _control()
    for interaction in (SimpleNamespace(guild=None, user=SimpleNamespace(id=USER)),
                        SimpleNamespace(guild=SimpleNamespace(id=GUILD), user=None)):
        assert "not authorized" in await mixin.handle_realtime_voice_control(interaction, "start")
