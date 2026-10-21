"""``inject_message`` answers True when a turn is *scheduled*. The real outcome reaches the plugin as the
``on_plugin_injection_result`` hook, exactly once, and a ``dedupe_key`` makes a retry of an admitted wake
a no-op. Real GatewayRunner, DiscordAdapter and SessionAuthority (fixtures only; no live gateway)."""
import asyncio

import pytest
import pytest_asyncio
import hermes_yaml as yaml

from gateway.run import GatewayRunner
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tests.waiting import async_wait_until

PLUGIN = "desk"


def _context(home):
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"entries": {PLUGIN: {"allow_gateway_injection": True}}},
    }), encoding="utf-8")
    manager = PluginManager(scope_key=str(home))
    return PluginContext(PluginManifest(name=PLUGIN, key=PLUGIN, source="user"), manager), manager


def _rows(authority, session_id):
    from hermes_state_runtime import list_session_admissions
    return [row for row in list_session_admissions(authority.db, session_id=session_id, pending_only=False)
            if row["principal_id"].startswith("automation:")]


@pytest_asyncio.fixture
async def world(monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from hermes_constants import get_hermes_home
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fixture-token", typing_indicator=False))

    async def send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="sent")
    monkeypatch.setattr(adapter, "send", send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    runner._wire_adapter_handlers(adapter)
    executed = []

    async def turn(event, source, quick_key, run_generation):
        executed.append(event.text)
        captured = execution_result.get()
        if captured is not None:
            captured["result"] = {"final_response": "reply", "messages": []}
        return "reply"
    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)

    ctx, manager = _context(get_hermes_home())
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)

    calls = []

    async def record(name, **kwargs):
        if name == "on_plugin_injection_result":
            calls.append(kwargs)
        return []
    monkeypatch.setattr("hermes_cli.plugins.ainvoke_hook", record)

    source = adapter.build_source(chat_id="42", chat_type="dm", user_id="42")
    await adapter.handle_message(MessageEvent(text="A", source=source, message_id="A"))
    await async_wait_until(lambda: "A" in executed, timeout=10, what="the human turn")
    key = runner.session_store._generate_session_key(source)
    owner = authority.logical_owner(runner.session_store.lookup_by_session_key(key).session_id)
    return type("World", (), dict(runner=runner, authority=authority, ctx=ctx, calls=calls, key=key,
                                  owner=owner))


async def _inject(world, text, **kwargs):
    return await asyncio.to_thread(world.ctx.inject_message, text, session_key=world.key, **kwargs)


@pytest.mark.asyncio
async def test_admitted_wake_is_acknowledged_once_with_its_key(world):
    assert await _inject(world, "wake 1", dedupe_key="occ-1") is True
    await async_wait_until(lambda: world.calls, timeout=10, what="the outcome hook")
    assert [(c["outcome"], c["dedupe_key"], c["plugin_id"]) for c in world.calls] == [("admitted", "occ-1", PLUGIN)]
    assert len(_rows(world.authority, world.owner)) == 1


@pytest.mark.asyncio
async def test_retry_of_an_admitted_wake_commits_nothing_twice(world):
    """The first admission's acknowledgement was lost: the plugin re-injects the same key, now with
    different text (a restarted scheduler words the wake differently). No second turn, still acknowledged."""
    assert await _inject(world, "wake 1", dedupe_key="occ-1") is True
    await async_wait_until(lambda: len(world.calls) == 1, timeout=10, what="first outcome")
    assert await _inject(world, "wake 1 (after restart)", dedupe_key="occ-1") is True
    await async_wait_until(lambda: len(world.calls) == 2, timeout=10, what="second outcome")
    assert [(c["outcome"], c["reason"]) for c in world.calls] == [("admitted", "admitted"),
                                                                  ("admitted", "already_admitted")]
    assert len(_rows(world.authority, world.owner)) == 1


@pytest.mark.asyncio
async def test_concurrent_handlers_with_one_key_admit_one_turn(world):
    results = await asyncio.gather(*[_inject(world, f"wake {i}", dedupe_key="occ-1") for i in range(3)])
    assert results == [True, True, True]
    await async_wait_until(lambda: len(world.calls) == 3, timeout=10, what="every outcome")
    assert len(_rows(world.authority, world.owner)) == 1


@pytest.mark.asyncio
async def test_without_a_key_every_injection_is_its_own_turn(world):
    assert await _inject(world, "same text") is True
    assert await _inject(world, "same text") is True
    await async_wait_until(lambda: len(world.calls) == 2, timeout=10, what="both outcomes")
    assert len(_rows(world.authority, world.owner)) == 2
    assert all(c["dedupe_key"] is None for c in world.calls)


@pytest.mark.asyncio
async def test_permanent_refusal_is_reported_with_its_reason_and_never_retried_by_core(world, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    attempts = []

    async def refuse(adapter, event, identity, replaces=None):
        attempts.append(identity)
        raise RuntimeStoreError("admission_conflict")
    monkeypatch.setattr(world.authority, "admit_automation", refuse)
    assert await _inject(world, "wake", dedupe_key="occ-2") is True
    await async_wait_until(lambda: world.calls, timeout=10, what="the refusal")
    assert [(c["outcome"], c["reason"]) for c in world.calls] == [("refused", "admission_conflict")]
    assert len(attempts) == 1
    assert not _rows(world.authority, world.owner)


@pytest.mark.asyncio
async def test_draining_runtime_is_retryable_and_the_same_key_admits_once_after_release(world, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    real = world.authority.admit_automation
    draining = {"on": True}

    async def gate(adapter, event, identity, replaces=None):
        if draining["on"]:
            raise RuntimeStoreError("runtime_draining")
        return await real(adapter, event, identity, replaces)
    monkeypatch.setattr(world.authority, "admit_automation", gate)
    assert await _inject(world, "wake", dedupe_key="occ-3") is True
    await async_wait_until(lambda: world.calls, timeout=10, what="the retryable outcome")
    assert world.calls[0]["outcome"] == "retryable" and world.calls[0]["reason"] == "runtime_draining"
    draining["on"] = False
    assert await _inject(world, "wake", dedupe_key="occ-3") is True
    await async_wait_until(lambda: len(world.calls) == 2, timeout=10, what="the admitted outcome")
    assert world.calls[1]["outcome"] == "admitted"
    assert len(_rows(world.authority, world.owner)) == 1


@pytest.mark.asyncio
async def test_unrouted_injection_settles_as_refused_exactly_once(world):
    world.runner._draining = True
    world.runner.session_store  # the scheduler accepts; dispatch finds the runner draining
    assert await _inject(world, "wake", dedupe_key="occ-4") is True
    await async_wait_until(lambda: world.calls, timeout=10, what="the unrouted outcome")
    assert [(c["outcome"], c["reason"]) for c in world.calls] == [("refused", "not_routed")]
    assert not _rows(world.authority, world.owner)
