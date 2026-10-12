"""Focused gateway regression checks for the Realtime result-to-voice relay."""
from __future__ import annotations

from unittest.mock import MagicMock

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _event() -> MessageEvent:
    source = SessionSource(platform=Platform.DISCORD, chat_id="700", chat_type="group", user_id="42")
    event = MessageEvent(source=source, text="what is the weather", message_type=MessageType.VOICE)
    event.metadata["realtime_delegate_event_key"] = "request-1"
    return event


def _runner(relay: bool):
    from gateway.run_voice import GatewayVoiceMixin

    TestRunner = type("RelayTestRunner", (GatewayRunner, GatewayVoiceMixin), {})
    runner = object.__new__(TestRunner)
    runner._voice_mode = {}
    runner._adapter_profile_for_source = lambda source: "default"
    adapter = MagicMock()
    adapter._realtime_relays_event.side_effect = lambda event: bool(
        (getattr(event, "metadata", None) or {}).get("realtime_delegate_event_key")
    ) and relay
    adapter._realtime_capture_result.side_effect = lambda event, text: (
        adapter._realtime_relays_event(event) and bool(text))
    runner._delivery_adapter_for = lambda source: adapter
    return runner, adapter


def test_only_exact_pending_realtime_event_claims_voice_reply() -> None:
    event = _event()
    runner, adapter = _runner(True)
    assert runner._realtime_relays_voice_reply(event) is True
    assert runner._realtime_relays_voice_reply(event, "Forecast: sunny") is True
    adapter._realtime_capture_result.assert_called_once_with(event, "Forecast: sunny")

    runner, adapter = _runner(False)
    assert runner._realtime_relays_voice_reply(event) is False
    assert runner._realtime_relays_voice_reply(event, "Forecast: sunny") is False


def test_claimed_result_skips_whole_file_reply() -> None:
    event = _event()
    runner, _adapter = _runner(True)
    runner._should_send_voice_reply = MagicMock(return_value=True)
    runner._send_voice_reply = MagicMock()

    relayed = bool("Forecast: sunny") and runner._realtime_relays_voice_reply(event, "Forecast: sunny")
    if not relayed and runner._should_send_voice_reply(event, "Forecast: sunny", []):
        runner._send_voice_reply(event, "Forecast: sunny")
    assert not runner._should_send_voice_reply.called
    runner._send_voice_reply.assert_not_called()

def test_direct_streaming_tts_start_checks_the_exact_delegated_event() -> None:
    event = _event()
    runner, _adapter = _runner(True)
    runner._delivery_adapter_for = lambda source: MagicMock(
        _should_auto_tts_for_chat=MagicMock(return_value=True))
    runner._run_agent_start_streaming_tts(
        source=event.source, message_type=MessageType.VOICE, _status_thread_metadata=None,
        streaming_tts_consumer_holder=[], relay_event=event)


def test_non_realtime_turn_does_not_claim_hermes_voice_delivery() -> None:
    event = _event()
    event.metadata.clear()
    runner, adapter = _runner(False)
    assert runner._realtime_relays_voice_reply(event, "ordinary reply") is False
    event.message_type = MessageType.TEXT
    runner._voice_mode["discord:700"] = "all"
    runner._delivery_adapter_for = lambda source: None
    assert runner._should_send_voice_reply(event, "ordinary reply", []) is True