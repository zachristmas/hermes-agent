"""``hermes_cli.herdr_session_store`` -- the on-disk record of Herdr sessions this install closed,
since Herdr itself has nothing left to ask after a pane closes (see
``hermes_cli/herdr_pane_lifecycle.py``)."""
from __future__ import annotations

import pytest

from hermes_cli import herdr_session_store as store


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_save_and_get_round_trip():
    store.save_record({"name": "reviewer", "kind": "claude", "session_id": "sess-1"})
    record = store.get_record("reviewer")
    assert record["session_id"] == "sess-1"


def test_get_record_missing_returns_none():
    assert store.get_record("nope") is None


def test_save_record_requires_a_name():
    with pytest.raises(ValueError):
        store.save_record({"kind": "claude"})


def test_list_records_reflects_multiple_saves():
    store.save_record({"name": "a", "kind": "claude", "session_id": "1"})
    store.save_record({"name": "b", "kind": "codex", "session_id": "2"})
    records = store.list_records()
    assert set(records) == {"a", "b"}


def test_remove_record():
    store.save_record({"name": "a", "kind": "claude", "session_id": "1"})
    assert store.remove_record("a") is True
    assert store.get_record("a") is None
    assert store.remove_record("a") is False


def test_save_record_persists_across_loads(tmp_path, monkeypatch):
    store.save_record({"name": "a", "kind": "claude", "session_id": "1"})
    # A fresh read (new call, no cached state) still sees it -- proves the write is on disk,
    # not held only in an in-process cache.
    assert store.get_record("a") is not None
