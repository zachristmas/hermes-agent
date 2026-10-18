"""Maintenance ticks must keep running while spawning is disabled (dispatch_in_gateway false)."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from gateway import kanban_watchers as watchers


class _Host(watchers.GatewayKanbanWatchersMixin):
    def __init__(self):
        self._running = True
        self.ticks = 0

    async def _sleep_between_ticks(self, _interval):
        self._running = False  # run exactly one iteration


def _patch(monkeypatch, kanban_cfg, seen):
    from hermes_cli import config

    monkeypatch.setattr(config, "load_config", lambda: {"kanban": kanban_cfg})

    class FakeDispatcher:
        def __init__(self, _kb, settings):
            seen["settings"] = settings

        def tick_once(self):
            seen["ticks"] = seen.get("ticks", 0) + 1
            return []

    monkeypatch.setattr(watchers, "_KanbanDispatcher", FakeDispatcher)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(watchers.asyncio, "sleep", no_sleep)


def test_paused_gateway_still_runs_spawn_disabled_maintenance(monkeypatch):
    seen: dict = {}
    _patch(monkeypatch, {"dispatch_in_gateway": False}, seen)
    asyncio.run(_Host()._kanban_maintenance_watcher())
    assert seen["ticks"] == 1
    assert dataclasses.asdict(seen["settings"])["max_spawn"] == 0  # nothing can be admitted


@pytest.mark.parametrize("cfg", [
    {"dispatch_in_gateway": True},
    {"dispatch_in_gateway": False, "maintenance_when_paused": False},
])
def test_maintenance_is_off_when_dispatch_is_on_or_explicitly_disabled(monkeypatch, cfg):
    seen: dict = {}
    _patch(monkeypatch, cfg, seen)
    asyncio.run(_Host()._kanban_maintenance_watcher())
    assert "ticks" not in seen
