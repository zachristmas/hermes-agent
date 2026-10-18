"""Dashboard Done on a review-required board: acceptance comes only from server-side request context.

A loopback session token, the same-user native principal, a token-auth caller, a payload boolean and the
old global ``dashboard_session_trusted`` option are NOT human proof. A verified OAuth-gated session or the
configured acceptance passphrase is. Temporary HERMES_HOME only.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_settlement as ks

API = "/api/plugins/kanban"
HEADER = "X-Hermes-Kanban-Acceptance"


def _load_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_acceptance_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    verifier = ks.make_verifier("correct horse")
    cfg = {"boards": [kb.get_current_board()], "verifier": verifier, "dashboard_session_trusted": True}
    monkeypatch.setattr(ks, "_acceptance_config", lambda: cfg)

    app = FastAPI()
    app.include_router(_load_plugin().router, prefix=API)

    @app.middleware("http")
    async def fake_dashboard_auth(request: Request, call_next):  # stands in for the real auth middlewares
        mode = request.headers.get("x-test-auth", "")
        if mode in ("oauth", "oauth+token"):
            request.state.session = object()
        if mode == "oauth+token":
            request.state.token_authenticated = True
        if mode == "native":
            request.state.native_http_principal = {"profile_id": "p"}
        return await call_next(request)

    app.state.auth_required = False
    return app, TestClient(app)


def _review_task():
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        assert kb.request_review(conn, tid, summary="ready")
    return tid


def _status(tid):
    with kbc.connect() as conn:
        return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def _done(client, tid, headers=None, **extra):
    return client.patch(f"{API}/tasks/{tid}", json={"status": "done", "summary": "ok", "result": "ok", **extra},
                        headers=headers or {})


def test_loopback_dashboard_session_is_not_human_proof(env):
    _app, client = env
    tid = _review_task()
    for kw in ({}, {"human_acceptance": True, "accepted": True, "dashboard_session_trusted": True}):
        assert _done(client, tid, **kw).status_code >= 400
    assert _done(client, tid, headers={HEADER: "wrong"}).status_code >= 400
    assert _status(tid) == "review"


def test_same_user_principals_are_not_human_proof_even_when_the_oauth_gate_is_on(env):
    app, client = env
    app.state.auth_required = True
    tid = _review_task()
    assert _done(client, tid, headers={"x-test-auth": "native"}).status_code >= 400
    assert _done(client, tid, headers={"x-test-auth": "oauth+token"}).status_code >= 400
    assert _done(client, tid).status_code >= 400
    assert _status(tid) == "review"


def test_a_session_object_without_the_oauth_gate_is_not_trusted(env):
    _app, client = env  # auth_required is False: request.state.session cannot be a verified provider session
    tid = _review_task()
    assert _done(client, tid, headers={"x-test-auth": "oauth"}).status_code >= 400
    assert _status(tid) == "review"


def test_correct_passphrase_header_completes_review_to_done(env):
    _app, client = env
    tid = _review_task()
    assert _done(client, tid, headers={HEADER: "correct horse"}).status_code == 200
    assert _status(tid) == "done"


def test_verified_oauth_gated_session_completes_review_to_done(env):
    app, client = env
    app.state.auth_required = True
    tid = _review_task()
    assert _done(client, tid, headers={"x-test-auth": "oauth"}).status_code == 200
    assert _status(tid) == "done"


def test_bulk_route_is_gated_the_same_way(env):
    _app, client = env
    a, b = _review_task(), _review_task()
    body = {"ids": [a, b], "status": "done", "summary": "ok", "result": "ok"}
    results = client.post(f"{API}/tasks/bulk", json=body).json()["results"]
    assert not any(r["ok"] for r in results) and _status(a) == _status(b) == "review"
    results = client.post(f"{API}/tasks/bulk", json=body, headers={HEADER: "correct horse"}).json()["results"]
    assert all(r["ok"] for r in results) and _status(a) == _status(b) == "done"


def test_dragging_a_card_to_done_or_skipping_review_is_refused_even_with_acceptance(env):
    _app, client = env
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="never reviewed", assignee="worker")  # not in review
    assert _done(client, tid, headers={HEADER: "correct horse"}).status_code >= 400
    assert _status(tid) != "done"


def test_acceptance_context_does_not_leak_between_requests(env):
    _app, client = env
    a, b = _review_task(), _review_task()
    assert _done(client, a, headers={HEADER: "correct horse"}).status_code == 200
    assert _done(client, b).status_code >= 400 and _status(b) == "review"
