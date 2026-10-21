"""kanban.review_dispatch_boards: per-board opt-in for automatic review-lane dispatch.

The flat ``kanban.review_dispatch`` boolean used to default ``True`` globally, with zero board
scoping. On a board whose humans review every card by hand (titan, 2026-10-15), that caused a
24-spawn runaway loop: the dispatcher kept re-claiming a card already parked in ``review``,
spawning a fresh agent with the generic implement-lane prompt, which correctly found nothing new
to do and immediately re-requested review, forever. The fix mirrors the existing
``kanban.direct_herdr_dispatch_boards`` allowlist exactly: a board must be explicitly listed to get
automatic review dispatch, and the opt-in fails closed (same direction as the Herdr allowlist, the
opposite of ``kanban.dispatch_profiles``) on any config-read error.
"""
from __future__ import annotations

import pytest


def _write_config(root, text: str) -> None:
    root.joinpath("config.yaml").write_text(text, encoding="utf-8")


def test_board_uses_review_dispatch_helper(tmp_path, monkeypatch):
    """Unit-level check of the gate predicate itself."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_config(root, "kanban:\n  review_dispatch_boards:\n    - gsd\n    - GSD\n")

    from hermes_cli import kanban_db_dispatch as kbd

    assert kbd.review_dispatch_enabled("gsd") is True
    assert kbd.review_dispatch_enabled("GSD") is True
    assert kbd.review_dispatch_enabled("titan") is False


def test_absent_config_key_defaults_off(tmp_path, monkeypatch):
    """No config key at all (the new default) -> every board stays human-only review."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db_dispatch as kbd

    assert kbd.review_dispatch_enabled("gsd") is False
    assert kbd.review_dispatch_enabled(None) is False


@pytest.mark.parametrize("config_text", [
    "kanban:\n  review_dispatch_boards:\n",   # present but blank/null
    "kanban:\n  review_dispatch_boards: []\n",  # present but empty list
])
def test_present_but_empty_boards_key_stays_off(tmp_path, monkeypatch, config_text):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_config(root, config_text)

    from hermes_cli import kanban_db_dispatch as kbd

    assert kbd.review_dispatch_enabled("gsd") is False


def test_comma_string_form_is_normalized(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_config(root, "kanban:\n  review_dispatch_boards: gsd, titan\n")

    from hermes_cli import kanban_db_dispatch as kbd

    assert kbd.review_dispatch_enabled("gsd") is True
    assert kbd.review_dispatch_enabled("titan") is True
    assert kbd.review_dispatch_enabled("connect") is False


def test_unreadable_config_fails_closed_to_off(tmp_path, monkeypatch):
    """A broken config read must never silently turn automatic review dispatch on anywhere --
    the same fail direction as _direct_herdr_dispatch_boards, not dispatch_profiles."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_config(root, "kanban:\n  review_dispatch_boards:\n    - gsd\n")

    from hermes_cli import config_effective
    from hermes_cli import kanban_db_dispatch as kbd

    def raise_read_error(**_kwargs):
        raise OSError("config unavailable")

    monkeypatch.setattr(config_effective, "load_user_config_effective", raise_read_error)

    assert kbd.review_dispatch_enabled("gsd") is False


def test_board_defaults_to_current_board_when_none_passed(tmp_path, monkeypatch):
    """``review_dispatch_enabled(None)`` resolves against the active board, same as the
    direct-Herdr allowlist's own None-board fallback."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_config(root, "kanban:\n  review_dispatch_boards:\n    - default\n")

    from hermes_cli import kanban_db_dispatch as kbd

    assert kbd.review_dispatch_enabled(None) is True
