"""``hermes_cli.herdr_pane_lifecycle`` -- close a Herdr agent's pane while recording enough to
reopen the SAME session later, since closing a pane destroys Herdr's own record of it (verified
live, herdr 0.9.3: ``agent get`` on a just-closed agent returns ``agent_not_found``). Stubs
``herdr`` the same way ``tests/hermes_cli/test_kanban_direct_herdr_remote_sweep.py`` does: a fake
Python script on ``HERDR_BIN`` that answers the exact argv shapes this module sends.
"""
from __future__ import annotations

import sys

import pytest

from hermes_cli import herdr_pane_lifecycle as lifecycle
from hermes_cli import herdr_session_store as store


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


_STUB = r'''
import json, os, sys

a = sys.argv[1:]

# Mirror the real CLI's top-level mode: "herdr --machine <id> <command...>" targets a saved
# remote machine instead of the local session. Record which machine_id (or None for local) each
# call actually used, written to CALLS_LOG, so a test can assert the plumbing reached the right
# host -- not just that SOME call happened.
machine_id = None
if a[:1] == ["--machine"]:
    machine_id = a[1]
    a = a[2:]
with open(CALLS_LOG, "a", encoding="utf-8") as f:
    f.write(json.dumps({"machine_id": machine_id, "argv": a}) + "\n")

def ok(result):
    print(json.dumps({"result": result}))

if a[:2] == ["agent", "get"]:
    name = a[2]
    agent = AGENTS.get(name)
    if agent is None:
        sys.stderr.write(json.dumps({"error": {"code": "agent_not_found", "message": f"agent target {name} not found"}}))
        sys.exit(1)
    ok({"agent": agent})
elif a[:2] == ["pane", "close"]:
    ok({"type": "ok"})
elif a[:2] == ["workspace", "list"]:
    ok({"workspaces": WORKSPACES})
elif a[:2] == ["tab", "create"]:
    ok({"root_pane": {"pane_id": NEW_PANE_ID}, "tab": {}})
elif a[:2] == ["agent", "start"]:
    name = a[2]
    ok({"agent": {"name": name, "pane_id": NEW_PANE_ID, "workspace_id": "w1", "agent_status": "idle"}})
else:
    sys.stderr.write(json.dumps({"error": {"code": "unknown_command", "message": " ".join(a)}}))
    sys.exit(1)
'''


def _stub(tmp_path, monkeypatch, *, agents=None, workspaces=None, new_pane_id="w1:p9"):
    script = tmp_path / "stub_herdr.py"
    calls_log = tmp_path / "calls.jsonl"
    calls_log.write_text("", encoding="utf-8")
    script.write_text(
        f"AGENTS = {agents or {}!r}\nWORKSPACES = {workspaces or []!r}\n"
        f"NEW_PANE_ID = {new_pane_id!r}\nCALLS_LOG = {str(calls_log)!r}\n" + _STUB,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")
    return calls_log


def _machines_called(calls_log) -> list:
    import json as _json

    lines = calls_log.read_text(encoding="utf-8").splitlines()
    return [_json.loads(line)["machine_id"] for line in lines if line]


def test_close_records_session_then_closes_pane(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, agents={
        "reviewer": {
            "name": "reviewer", "agent": "claude", "pane_id": "w1:p2", "cwd": "/work",
            "workspace_id": "w1",
            "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "sess-123"},
        },
    })
    record = lifecycle.close("reviewer")
    assert record["session_id"] == "sess-123"
    assert record["kind"] == "claude"
    assert record["cwd"] == "/work"
    assert store.get_record("reviewer") == record


def test_close_refuses_an_agent_with_no_resumable_session(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, agents={
        "reviewer": {"name": "reviewer", "agent": "claude", "pane_id": "w1:p2", "agent_session": {}},
    })
    with pytest.raises(lifecycle.PaneLifecycleError, match="no resumable session id"):
        lifecycle.close("reviewer")
    # Never recorded, and the pane-close stub would have raised unknown_command if called --
    # the absence of a record is the proof close() never touched the pane either.
    assert store.get_record("reviewer") is None


def test_close_raises_for_an_agent_that_does_not_exist(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, agents={})
    with pytest.raises(lifecycle.PaneLifecycleError, match="no live Herdr agent"):
        lifecycle.close("ghost")


def test_reopen_resumes_a_recorded_claude_session_into_a_new_pane(tmp_path, monkeypatch):
    store.save_record({
        "name": "reviewer", "kind": "claude", "session_id": "sess-123", "cwd": "/work",
        "workspace_id": "w1", "machine_id": None, "tokens": {}, "closed_pane_id": None,
    })
    _stub(
        tmp_path, monkeypatch,
        workspaces=[{"workspace_id": "w1", "label": "titan"}],
        new_pane_id="w1:p9",
    )
    agent = lifecycle.reopen("reviewer")
    assert agent["pane_id"] == "w1:p9"
    updated = store.get_record("reviewer")
    assert updated["reopened_pane_id"] == "w1:p9"
    assert updated["session_id"] == "sess-123"


def test_reopen_builds_codex_resume_as_a_subcommand_not_a_flag(tmp_path, monkeypatch):
    """codex's resume syntax is `codex resume <id>` (a subcommand, the first positional arg),
    unlike claude's `--resume <id>` flag -- this must not be guessed as a mirror of claude's."""
    assert lifecycle._RESUME_ARG_BUILDERS["codex"]("sess-999") == ["resume", "sess-999"]
    assert lifecycle._RESUME_ARG_BUILDERS["claude"]("sess-999") == ["--resume", "sess-999"]


def test_reopen_accepts_a_raw_session_id_with_explicit_kind_when_nothing_was_recorded(tmp_path, monkeypatch):
    _stub(
        tmp_path, monkeypatch,
        workspaces=[{"workspace_id": "w1", "label": "titan"}],
        new_pane_id="w1:p9",
    )
    agent = lifecycle.reopen(
        "373452c2-7449-4782-83b9-4edb63f11537", kind="claude", workspace="titan", cwd="/work")
    assert agent["pane_id"] == "w1:p9"


def test_reopen_refuses_a_raw_session_id_without_kind(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch)
    with pytest.raises(lifecycle.PaneLifecycleError, match="--kind is required"):
        lifecycle.reopen("373452c2-7449-4782-83b9-4edb63f11537")


def test_reopen_refuses_an_unrecognized_target_with_no_record_and_not_a_uuid(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch)
    with pytest.raises(lifecycle.PaneLifecycleError, match="no recorded closed Herdr session"):
        lifecycle.reopen("not-a-uuid-and-never-closed")


def test_reopen_refuses_an_unsupported_agent_kind(tmp_path, monkeypatch):
    store.save_record({
        "name": "weirdo", "kind": "some-future-kind", "session_id": "sess-1", "cwd": "/work",
        "workspace_id": "w1", "machine_id": None,
    })
    _stub(tmp_path, monkeypatch, workspaces=[{"workspace_id": "w1", "label": "titan"}])
    with pytest.raises(lifecycle.PaneLifecycleError, match="no known resume syntax"):
        lifecycle.reopen("weirdo")


def test_reopen_reuses_an_already_available_pane_instead_of_creating_one(tmp_path, monkeypatch):
    store.save_record({
        "name": "reviewer", "kind": "claude", "session_id": "sess-123", "cwd": "/work",
        "workspace_id": "w1", "machine_id": None,
    })
    # No workspace/tab-create stub entries at all -- if reopen() tried to create a pane despite
    # --pane being given, the stub's `else` branch would raise unknown_command and fail this test.
    _stub(tmp_path, monkeypatch, new_pane_id="w1:p7")
    agent = lifecycle.reopen("reviewer", pane_id="w1:p7")
    assert agent["pane_id"] == "w1:p7"


def test_close_on_a_remote_machine_routes_every_call_through_that_machine(tmp_path, monkeypatch):
    """Zach's real setup: close an agent running on the Windows box while issuing the command
    from the Mac (or vice versa) via a saved Herdr machine. Every herdr call this makes --
    `agent get` and `pane close` alike -- must carry `--machine <id>`, never fall back to the
    local session."""
    calls_log = _stub(tmp_path, monkeypatch, agents={
        "reviewer": {
            "name": "reviewer", "agent": "claude", "pane_id": "w1:p2", "cwd": "/work",
            "workspace_id": "w1",
            "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "sess-123"},
        },
    })
    record = lifecycle.close("reviewer", machine_id="winbox")
    assert record["machine_id"] == "winbox"
    assert _machines_called(calls_log) == ["winbox", "winbox"]  # agent get, then pane close


def test_reopen_on_a_different_machine_than_the_one_it_was_closed_on(tmp_path, monkeypatch):
    """A session closed on one machine can be resumed on a DIFFERENT one by passing --machine
    explicitly at reopen time, regardless of where the original record says it lived -- the
    cross-host case this feature exists for (e.g. closed on Windows, reopened by a command issued
    from the Mac and targeted at a third/different saved machine, or at the Mac's own local
    session by passing no --machine at all)."""
    store.save_record({
        "name": "reviewer", "kind": "claude", "session_id": "sess-123", "cwd": "/work",
        "workspace_id": "w1", "machine_id": "winbox",
    })
    calls_log = _stub(
        tmp_path, monkeypatch,
        workspaces=[{"workspace_id": "w1", "label": "titan"}],
        new_pane_id="w2:p3",
    )
    agent = lifecycle.reopen("reviewer", machine_id="mac-local")
    assert agent["pane_id"] == "w2:p3"
    # Every call this made (the uniqueness check, workspace list, tab create, agent start) went
    # to the OVERRIDE machine, never "winbox" -- not one call silently fell back to local/stale.
    machines = _machines_called(calls_log)
    assert machines and set(machines) == {"mac-local"}
    assert store.get_record("reviewer")["machine_id"] == "mac-local"


def test_reopen_without_an_override_stays_on_the_machine_it_was_closed_on(tmp_path, monkeypatch):
    store.save_record({
        "name": "reviewer", "kind": "claude", "session_id": "sess-123", "cwd": "/work",
        "workspace_id": "w1", "machine_id": "winbox",
    })
    calls_log = _stub(
        tmp_path, monkeypatch,
        workspaces=[{"workspace_id": "w1", "label": "titan"}],
        new_pane_id="w1:p5",
    )
    lifecycle.reopen("reviewer")
    machines = _machines_called(calls_log)
    assert machines and set(machines) == {"winbox"}
