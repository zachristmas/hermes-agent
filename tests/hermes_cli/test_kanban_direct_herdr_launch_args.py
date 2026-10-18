"""Harness-specific launch contracts for direct Herdr startup."""

import pytest

from hermes_cli import kanban_direct_herdr_worker as bridge


def _launch(monkeypatch, kind, model="claude-sonnet-5-5", effort="medium"):
    calls = []
    monkeypatch.setattr(bridge, "_run_herdr", lambda args, **kwargs: calls.append(args))
    bridge.start_agent("machineops-startup-check", "wB:pTest", model, effort, kind=kind)
    args = calls[0]
    return args[args.index("--") + 1:] if "--" in args else []


@pytest.mark.platforms("windows")
def test_windows_codex_does_not_inherit_claude_defaults(monkeypatch):
    assert _launch(monkeypatch, "codex") == [
        "--yolo", "--no-daemon", "-c", 'model_reasoning_effort="medium"',
    ]


def test_codex_uses_native_model_and_reasoning_options(monkeypatch):
    args = _launch(monkeypatch, "codex", model="gpt-6.1-sol", effort="high")
    assert "--effort" not in args
    assert args[args.index("--model") + 1] == "gpt-6.1-sol"
    assert args[args.index("-c") + 1] == 'model_reasoning_effort="high"'


def test_claude_keeps_its_own_options(monkeypatch):
    assert _launch(monkeypatch, "claude") == [
        "--model", "claude-sonnet-5-5", "--effort", "medium",
    ]


@pytest.mark.parametrize("kind", ["gemini", "hermes", "opencode"])
def test_other_harnesses_do_not_receive_claude_options(monkeypatch, kind):
    assert _launch(monkeypatch, kind) == []
