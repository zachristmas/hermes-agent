"""``hermes_cli.web_routers.herdr`` -- the dashboard's minimal list+reopen panel for the Herdr
close/reopen feature. Calls the route coroutines directly (same style as
``test_web_routers_endpoint_probe.py``), stubbing ``herdr`` the way
``test_herdr_pane_lifecycle.py`` does.
"""
from __future__ import annotations

import asyncio
import sys

import pytest
from fastapi import HTTPException

from hermes_cli import herdr_session_store as store
from hermes_cli.web_models import HerdrReopenBody
from hermes_cli.web_routers.herdr import herdr_closed_sessions_route, herdr_reopen_route


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_closed_sessions_route_reflects_the_store(tmp_path, monkeypatch):
    store.save_record({"name": "reviewer", "kind": "claude", "session_id": "sess-1", "cwd": "/w"})
    result = asyncio.run(herdr_closed_sessions_route())
    assert result["records"]["reviewer"]["session_id"] == "sess-1"


def test_closed_sessions_route_empty_when_nothing_recorded():
    result = asyncio.run(herdr_closed_sessions_route())
    assert result["records"] == {}


def test_reopen_route_resumes_a_recorded_session(tmp_path, monkeypatch):
    store.save_record({
        "name": "reviewer", "kind": "claude", "session_id": "sess-123", "cwd": "/work",
        "workspace_id": "w1", "machine_id": None,
    })
    script = tmp_path / "stub_herdr.py"
    script.write_text(
        r'''
import json, sys
a = sys.argv[1:]
if a[:2] == ["agent", "get"]:
    sys.stderr.write(json.dumps({"error": {"code": "agent_not_found", "message": "x"}}))
    sys.exit(1)
elif a[:2] == ["workspace", "list"]:
    print(json.dumps({"result": {"workspaces": [{"workspace_id": "w1", "label": "titan"}]}}))
elif a[:2] == ["tab", "create"]:
    print(json.dumps({"result": {"root_pane": {"pane_id": "w1:p9"}}}))
elif a[:2] == ["agent", "start"]:
    print(json.dumps({"result": {"agent": {"name": a[2], "pane_id": "w1:p9", "agent_status": "idle"}}}))
else:
    sys.stderr.write(json.dumps({"error": {"code": "unknown_command", "message": " ".join(a)}}))
    sys.exit(1)
''',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")
    result = asyncio.run(herdr_reopen_route(HerdrReopenBody(target="reviewer")))
    assert result["pane_id"] == "w1:p9"


def test_reopen_route_maps_lifecycle_error_to_400(tmp_path, monkeypatch):
    script = tmp_path / "stub_not_found.py"
    script.write_text(
        'import json, sys\n'
        'sys.stderr.write(json.dumps({"error": {"code": "agent_not_found", "message": "x"}}))\n'
        'sys.exit(1)\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(herdr_reopen_route(HerdrReopenBody(target="no-such-record")))
    assert exc_info.value.status_code == 400
