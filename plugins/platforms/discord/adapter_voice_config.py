"""``discord.*`` voice settings read from config.yaml (not .env): voice FX, inactivity and playback timeouts."""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class DiscordVoiceConfigMixin:
    VOICE_TIMEOUT: int
    PLAYBACK_TIMEOUT: int

    def _load_voice_fx_config(self) -> Dict[str, Any]:
        """Read ``discord.voice_fx`` from config.yaml (not .env; off by default) with safe defaults."""
        defaults: Dict[str, Any] = {
            "enabled": False,        # master switch for the mixer subsystem
            "ambient_enabled": True, # idle "thinking" bed while tools run
            "ambient_path": "",      # optional custom loop file; "" = synthesised
            "ambient_gain": 0.18,    # idle bed loudness (0..1)
            "duck_gain": 0.06,       # ambient loudness while speech plays
            "speech_gain": 1.0,      # TTS / ack loudness
            "lead_silence_ms": 200,  # silence prepended to each clip so the
                                     # voice socket's warm-up doesn't clip the first word
            "ack_enabled": True,     # speak a short phrase before tool calls
            "ack_phrases": None,     # None = the localized defaults (``_default_voice_ack_phrases``)
        }
        try:
            from hermes_cli.config import read_raw_config
            cfg = read_raw_config() or {}
            fx = ((cfg.get("discord") or {}).get("voice_fx") or {})
            if isinstance(fx, dict):
                for k, v in fx.items():
                    if k in defaults and v is not None:
                        defaults[k] = v
        except Exception as e:
            logger.debug("Could not load discord.voice_fx config: %s", e)
        return defaults

    def _load_discord_int_config(self, key: str, default: int, *, minimum: int = 0) -> int:
        """Read a non-secret integer from the top-level ``discord`` config."""
        try:
            from hermes_cli.config import read_raw_config
            cfg = read_raw_config() or {}
            raw = (cfg.get("discord") or {}).get(key, default)
            value = int(raw)
            return max(minimum, value)
        except Exception as e:
            logger.debug("Could not load discord.%s config: %s", key, e)
            return default

    def _load_voice_timeout(self) -> int:
        """Return voice-channel inactivity timeout seconds; 0 disables it."""
        return self._load_discord_int_config(
            "voice_channel_inactivity_timeout_seconds", self.VOICE_TIMEOUT, minimum=0,
        )

    def _load_playback_timeout(self) -> int:
        """Return minimum playback wait seconds for Discord VC audio."""
        return self._load_discord_int_config(
            "voice_playback_timeout_seconds", self.PLAYBACK_TIMEOUT, minimum=1,
        )
