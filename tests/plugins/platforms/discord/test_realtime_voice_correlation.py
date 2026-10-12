"""A delegated job resolves only from its own dispatched event once that event carries a key.

``_handle_voice_channel_input`` stamps every voice event with ``realtime_delegate_event_key`` and
hands it to the per-call hook before ``handle_message``. The job must take that key in the hook
itself: lifecycle hooks can fire while the callback is still awaiting dispatch, and an unrelated
keyed utterance with the same words from the same speaker must never resolve someone else's job.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from discord_voice_fakes import ALLOWED_USER, GUILD

from gateway.platforms.event import MessageType, ProcessingOutcome
from plugins.platforms.discord.realtime_voice import DelegationRequest

KEY = "realtime_delegate_event_key"


def _adapter():
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    with patch("hermes_cli.config.read_raw_config", return_value={"discord": {}}):
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
    adapter._record_discord_processing_complete = MagicMock()
    return adapter


def _event(text, key):
    return SimpleNamespace(text=text, user_id=str(ALLOWED_USER), raw_message=SimpleNamespace(guild_id=GUILD),
                           message_type=MessageType.VOICE, metadata={KEY: key})


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_an_unrelated_keyed_event_during_dispatch_does_not_resolve_the_job():
    from plugins.platforms.discord.adapter_realtime_voice import GatewayVoiceDelegate

    adapter = _adapter()
    mine = {}

    async def gateway_entry(guild_id, user_id, transcript, *, on_realtime_event_created=None):
        mine["event"] = _event(transcript, "mine")
        on_realtime_event_created(mine["event"])
        # still inside dispatch: another utterance with the same words finishes first
        await adapter.on_processing_complete(_event(transcript, "other"), ProcessingOutcome.SUCCESS)

    adapter._voice_input_callback = gateway_entry
    job = asyncio.ensure_future(GatewayVoiceDelegate(adapter, GUILD).run(
        DelegationRequest("h1", GUILD, ALLOWED_USER, "check my calendar")))
    await _settle()
    assert not job.done()
    await adapter.on_processing_complete(mine["event"], ProcessingOutcome.FAILURE)
    assert (await asyncio.wait_for(job, 1)).status == "failed"


@pytest.mark.asyncio
async def test_a_keyed_event_before_the_hook_fires_does_not_resolve_the_job():
    from plugins.platforms.discord.adapter_realtime_voice import GatewayVoiceDelegate

    adapter = _adapter()
    entered, release, mine = asyncio.Event(), asyncio.Event(), {}

    async def gateway_entry(guild_id, user_id, transcript, *, on_realtime_event_created=None):
        entered.set()
        await release.wait()
        mine["event"] = _event(transcript, "mine")
        on_realtime_event_created(mine["event"])

    adapter._voice_input_callback = gateway_entry
    job = asyncio.ensure_future(GatewayVoiceDelegate(adapter, GUILD).run(
        DelegationRequest("h1", GUILD, ALLOWED_USER, "check my calendar")))
    await entered.wait()
    await adapter.on_processing_complete(_event("check my calendar", "other"), ProcessingOutcome.SUCCESS)
    release.set()
    await _settle()
    assert not job.done()
    await adapter.on_processing_complete(mine["event"], ProcessingOutcome.SUCCESS)
    assert (await asyncio.wait_for(job, 1)).status == "done"
