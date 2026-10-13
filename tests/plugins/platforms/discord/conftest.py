"""Fixtures for the Discord streaming-voice tests (fakes live in discord_voice_fakes.py)."""

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("discord")

from discord_voice_fakes import ALLOWED_USER, GUILD, TEXT_CHANNEL, FakeVoiceClient  # noqa: E402


@pytest.fixture
def make_adapter():
    """A real DiscordAdapter built through ``__init__`` with ``discord.voice_streaming`` config,
    bound to a fake voice client in GUILD whose transcripts route to TEXT_CHANNEL."""
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    built = []

    def _make(streaming=None, *, voice_fx=None, vc=None):
        discord_cfg = {"voice_streaming": {"enabled": True, **(streaming or {})}}
        if voice_fx is not None:
            discord_cfg["voice_fx"] = voice_fx
        with patch("hermes_cli.config.read_raw_config", return_value={"discord": discord_cfg}):
            adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
        adapter._client = MagicMock()
        adapter._allowed_user_ids = {str(ALLOWED_USER)}
        adapter._voice_timeout_seconds = 0  # no auto-leave timers in tests
        vc = vc or FakeVoiceClient()
        adapter._voice_clients[GUILD] = vc
        adapter._voice_text_channels[GUILD] = TEXT_CHANNEL
        built.append(vc)
        return adapter, vc

    yield _make
    for vc in built:
        vc.stop()
