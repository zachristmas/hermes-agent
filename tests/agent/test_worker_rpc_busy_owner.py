"""A busy owner is waited out; an absent or mismatched one still fails fast.

Regression: an owner whose event loop stalled past the 5s discovery bound answered ``starting`` or
``control_timeout``, the worker raised ``owner_unavailable`` on its first persistence call, and
that sticky failure ended the whole managed run.
"""
import json
import queue
from types import SimpleNamespace

import pytest

from agent import runtime_session_store as store


READY = SimpleNamespace(state='ready', endpoint=SimpleNamespace(api_origin='http://127.0.0.1:1234'))


@pytest.fixture
def rpc_env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, '_OWNER_RETRY_INTERVAL_SECONDS', 0)
    monkeypatch.setattr('hermes_cli.gateway_runtime.control_home_for', lambda home, endpoint: home)
    monkeypatch.setattr('hermes_cli.gateway_client._session_ticket', lambda *a, **k: 'ticket')

    def connect(url, **kwargs):
        sent = queue.SimpleQueue()

        def recv():
            return json.dumps({'id': json.loads(sent.get())['id'], 'result': {'ok': True}})

        return SimpleNamespace(send=sent.put, recv=recv, close=lambda: None)

    monkeypatch.setattr('websockets.sync.client.connect', connect)
    monkeypatch.setattr('hermes_cli.gateway_runtime_discovery.query_identify', lambda *a, **k: {'pid': -1})

    def script(discoveries):
        seen = iter(discoveries)
        calls = []

        def discover(*a, **k):
            calls.append(1)
            return next(seen)

        monkeypatch.setattr('hermes_cli.gateway_runtime.discover_gateway_endpoint', discover)
        return store.WorkerRPC(tmp_path), calls

    return script


@pytest.mark.parametrize('busy', [
    SimpleNamespace(state='starting', endpoint=None),
    SimpleNamespace(state='inaccessible', endpoint=None, reason_code='control_timeout'),
])
def test_busy_owner_is_waited_out(rpc_env, busy):
    rpc, calls = rpc_env([busy, busy, READY])
    assert rpc('worker.persist') == {'ok': True}
    assert len(calls) == 3


def test_busy_owner_gives_up_after_bounded_patience(rpc_env, monkeypatch):
    monkeypatch.setattr(store, '_OWNER_PATIENCE_SECONDS', 0)
    rpc, calls = rpc_env([SimpleNamespace(state='starting', endpoint=None)] * 5)
    with pytest.raises(store.WorkerPersistenceError, match='owner_unavailable'):
        rpc('worker.persist')
    assert len(calls) == 1


@pytest.mark.parametrize('gone', [
    SimpleNamespace(state='absent', endpoint=None),
    SimpleNamespace(state='inaccessible', endpoint=None, reason_code='profile_mismatch'),
    SimpleNamespace(state='inaccessible', endpoint=None, reason_code='authorization'),
    SimpleNamespace(state='draining', endpoint=None),
    SimpleNamespace(state='incompatible', endpoint=None, reason_code='runtime_protocol'),
])
def test_absent_or_mismatched_owner_is_not_retried(rpc_env, gone):
    rpc, calls = rpc_env([gone] * 5)
    with pytest.raises(store.WorkerPersistenceError, match='owner_unavailable'):
        rpc('worker.persist')
    assert len(calls) == 1


def test_identify_timeout_is_waited_out(rpc_env, monkeypatch):
    answers = iter([TimeoutError(), TimeoutError(), {'pid': -1}])

    def identify(*a, **k):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr('hermes_cli.gateway_runtime_discovery.query_identify', identify)
    rpc, _ = rpc_env([READY])
    assert rpc('worker.persist') == {'ok': True}


def test_identify_timeout_surfaces_once_patience_is_spent(rpc_env, monkeypatch):
    monkeypatch.setattr(store, '_OWNER_PATIENCE_SECONDS', 0)

    def identify(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr('hermes_cli.gateway_runtime_discovery.query_identify', identify)
    rpc, _ = rpc_env([READY])
    with pytest.raises(TimeoutError):
        rpc('worker.persist')
