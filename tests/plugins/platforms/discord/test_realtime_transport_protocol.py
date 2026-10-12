"""Offline wire-protocol regressions for the Realtime transport and session."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from plugins.platforms.discord.realtime_voice import (
    OpenAIRealtimeTransport,
    RealtimeVoiceConfig,
    session_update_event,
)
from tests.plugins.platforms.discord.realtime_fakes import (
    ALLOWED,
    FakeDelegate,
    FakePlayback,
    make_session,
)


class FakeWS:
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    async def send_str(self, payload):
        self.sent.append(payload)

    async def receive(self):
        if not self.incoming:
            return SimpleNamespace(type=SimpleNamespace(name="CLOSED"), data=None)
        return self.incoming.pop(0)

    async def close(self):
        self.closed = True


class FakeHTTPSession:
    def __init__(self, ws=None):
        self.ws = ws or FakeWS()
        self.connect_args = None
        self.closed = False

    async def ws_connect(self, url, **kwargs):
        self.connect_args = (url, kwargs)
        return self.ws


@pytest.mark.asyncio
async def test_connect_sends_sentinel_only_in_authorization_header_and_serializes_session_update(caplog):
    sentinel = "sk-test"
    ws = FakeWS()
    http = FakeHTTPSession(ws)
    transport = OpenAIRealtimeTransport(
        model="gpt-realtime-test",
        session_factory=lambda: http,
        environ={"OPENAI_API_KEY": sentinel},
    )

    await transport.connect()
    cfg = RealtimeVoiceConfig(model="gpt-realtime-test", max_output_tokens=77)
    expected = session_update_event(cfg)
    await transport.send(expected)

    url, options = http.connect_args
    serialized = ws.sent[0]
    assert options["headers"] == {"Authorization": f"Bearer {sentinel}"}
    assert sentinel not in url
    assert json.loads(serialized) == expected
    assert sentinel not in serialized
    assert sentinel not in caplog.text

    await transport.close()
    assert ws.closed


@pytest.mark.asyncio
async def test_connect_uses_explicit_profile_key_without_secret_lookup(monkeypatch):
    import agent.secret_scope as secret_scope
    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: True)
    monkeypatch.setattr(secret_scope, "get_secret", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("transport must use the profile key resolved at session start")))
    ws = FakeWS()
    http = FakeHTTPSession(ws)
    transport = OpenAIRealtimeTransport(
        model="gpt-realtime-test", api_key="profile-specific-test-key",
        session_factory=lambda: http,
    )
    await transport.connect()
    assert http.connect_args is not None
    assert http.connect_args[1]["headers"] == {"Authorization": "Bearer profile-specific-test-key"}
    assert "profile-specific-test-key" not in http.connect_args[0]
    await transport.close()


@pytest.mark.asyncio
async def test_tool_call_output_correlates_call_id_through_session_transport():
    session, transport, _, delegate, _ = make_session(delegate=FakeDelegate())
    await session.start()
    assert transport.sent[0] == session_update_event(session.cfg)
    # Function calls are accepted only for the currently focused speaker.
    session._focus.user_id = ALLOWED

    await session.handle_event({
        "type": "response.function_call_arguments.done",
        "name": "ask_hermes",
        "call_id": "call-correlation-17",
        "arguments": json.dumps({"request": "check the forecast"}),
    })

    outputs = [event["item"] for event in transport.of_type("conversation.item.create")
               if event.get("item", {}).get("type") == "function_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "call-correlation-17"
    output = json.loads(outputs[0]["output"])
    assert {k: output[k] for k in ("status", "job_id")} == {"status": "started", "job_id": "h1"}
    assert "acknowledgement" in output["next"]
    assert len(delegate.requests) == 0  # delegated coroutine has not run until the loop yields

    await session.close("test-complete")


@pytest.mark.asyncio
async def test_receive_server_error_payload_does_not_expose_secret(caplog):
    sentinel = "sk-test"
    ws = FakeWS([SimpleNamespace(
        type=SimpleNamespace(name="TEXT"),
        data=json.dumps({"type": "error", "error": {"message": sentinel, "code": "bad_request"}}),
    )])
    http = FakeHTTPSession(ws)
    transport = OpenAIRealtimeTransport(
        model="gpt-realtime-test", session_factory=lambda: http,
        environ={"OPENAI_API_KEY": sentinel},
    )
    await transport.connect()

    event = await transport.recv()
    assert event["type"] == "error"
    assert sentinel not in http.connect_args[0]
    assert sentinel not in caplog.text
    await transport.close()


@pytest.mark.asyncio
async def test_connect_failure_closes_owned_transport_session_and_does_not_leak_secret(caplog):
    sentinel = "sk-test"

    class FailingSession(FakeHTTPSession):
        async def ws_connect(self, url, **kwargs):
            raise OSError(f"offline failure for {url}")

    http = FailingSession()
    transport = OpenAIRealtimeTransport(
        model="gpt-realtime-test", session_factory=lambda: http,
        environ={"OPENAI_API_KEY": sentinel},
    )
    with pytest.raises(OSError) as error:
        await transport.connect()

    assert sentinel not in str(error.value)
    assert sentinel not in caplog.text
    # Caller-provided sessions are intentionally not owned/closed by the transport.
    assert not http.closed
