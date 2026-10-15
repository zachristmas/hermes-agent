"""``hermes_cli.herdr_slash`` -- the shared ``/herdr`` body both the CLI REPL
(``cli_commands_mixin.py``) and the messaging gateway (``gateway/slash_commands.py``) call into."""
from __future__ import annotations

import sys

import pytest

from hermes_cli import herdr_session_store as store
from hermes_cli.herdr_slash import dispatch_herdr_slash, handle_cli_command


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_bare_herdr_lists_records():
    assert "No closed Herdr sessions recorded." in dispatch_herdr_slash("")
    assert "No closed Herdr sessions recorded." in dispatch_herdr_slash("list")


def test_list_shows_a_recorded_session():
    store.save_record({"name": "reviewer", "kind": "claude", "session_id": "sess-1", "cwd": "/w"})
    out = dispatch_herdr_slash("list")
    assert "reviewer" in out and "claude" in out


def test_unknown_action_shows_usage():
    out = dispatch_herdr_slash("frobnicate reviewer")
    assert "Usage: /herdr" in out


def test_close_with_no_target_shows_usage():
    out = dispatch_herdr_slash("close")
    assert "Usage: /herdr close" in out


def test_close_surfaces_a_lifecycle_error_as_text_not_an_exception(tmp_path, monkeypatch):
    script = tmp_path / "stub.py"
    script.write_text(
        'import json, sys\n'
        'sys.stderr.write(json.dumps({"error": {"code": "agent_not_found", "message": "x"}}))\n'
        'sys.exit(1)\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")
    out = dispatch_herdr_slash("close ghost")
    assert "/herdr close:" in out and "no live Herdr agent" in out


def test_handle_cli_command_prints_the_dispatch_result(capsys):
    class _Fake:
        pass

    handle_cli_command(_Fake(), "herdr list")
    captured = capsys.readouterr()
    assert "No closed Herdr sessions recorded." in captured.out
