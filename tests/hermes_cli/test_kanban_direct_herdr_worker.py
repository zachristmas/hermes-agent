"""hermes_cli.kanban_direct_herdr_worker — the direct-Herdr dispatch bridge (card t_cb0795f9).

Exercises the bridge against a stubbed ``herdr`` CLI (a small fake script, not the real binary,
resolved through ``HERDR_BIN`` exactly as a real deployment would resolve it) and a synthetic
on-disk kanban DB fixture. Covers the full workspace -> pane create -> link/tag -> agent start ->
prompt -> wait -> read -> cleanup happy path, the real CLI's structured non-fatal codes
(``agent_not_ready``, ``agent_blocked``, ``agent_prompt_stalled``), the exit-1-vs-exit-2
distinction, and a hard-failure path (herdr unreachable) that must surface a clean non-zero exit
rather than hang. The result/error shapes here match the real ``herdr`` CLI's own ``--skill``
reference (0.9.3), relayed by the coordinator from the installed binary — not guessed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_direct_herdr_worker as bridge

_STUB_PRELUDE = r'''
import json, os, sys

STATE = os.environ["STUB_STATE"]

def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"tokens": {}, "calls": [], "workspaces": []}

def _save(state):
    with open(STATE, "w") as f:
        json.dump(state, f)

a = sys.argv[1:]
if a[:1] == ["--session"]:
    a = a[2:]  # herdr_command() pins --session <name> ahead of every real subcommand
state = _load()
state["calls"].append(a)
_save(state)
'''

_HAPPY_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["workspace", "list"]:
    print(json.dumps({"result": {"workspaces": state["workspaces"]}}))
elif a[:2] == ["workspace", "create"]:
    label = a[a.index("--label") + 1]
    ws = {"label": label, "workspace_id": "w" + label}
    state["workspaces"].append(ws)
    _save(state)
    print(json.dumps({"result": {"workspace": ws, "tab": {"tab_id": "t1"}, "root_pane": {"pane_id": "w1:p1"}}}))
elif a[:2] == ["tab", "create"]:
    print(json.dumps({"result": {"tab": {"tab_id": "t2"}, "root_pane": {"pane_id": "w1:p2"}}}))
elif a[:2] == ["pane", "report-metadata"]:
    pane_id = a[2]
    tokens = {}
    i = 3
    while i < len(a):
        if a[i] == "--token":
            k, _, v = a[i + 1].partition("=")
            tokens[k] = v
        i += 2
    state["tokens"].setdefault(pane_id, {}).update(tokens)
    _save(state)
    # the real CLI prints nothing on a successful metadata update
elif a[:2] == ["pane", "get"]:
    pane_id = a[2]
    print(json.dumps({"result": {"pane": {"id": pane_id, "tokens": state["tokens"].get(pane_id, {})}}}))
elif a[:2] == ["agent", "start"]:
    print(json.dumps({"result": {"agent": {"name": a[2]}}}))
    state.setdefault("agents", []).append({"name": a[2], "pane_id": a[a.index("--pane") + 1], "agent_status": "idle"})
    _save(state)
elif a[:2] == ["agent", "prompt"]:
    # A cooperative agent: it writes the run-scoped final report the assignment names.
    marker = "your report to "
    if marker in a[3] and state.get("report_file", "") is not None:
        path = a[3].split(marker, 1)[1].split(" in the format", 1)[0].strip()
        with open(path, "w") as f:
            f.write(state.get("report_file") or (
                "## Status\ncomplete\n## Reply\nnone\n## Deliverables\n- [x] the fix\n"
                "## Summary\nCard done: fixed the thing.\n"))
    print(json.dumps({"result": {}}))
elif a[:2] == ["pane", "list"]:
    panes = [{"pane_id": p, "workspace_id": p.split(":")[0], "tab_id": p.split(":")[0] + ":t1",
              "terminal_id": "term_" + p, "tokens": tok} for p, tok in state["tokens"].items()]
    print(json.dumps({"result": {"panes": panes}}))
elif a[:2] == ["agent", "wait"]:
    # Settling is what "wait" means -- any tracked agent (the reuse-path fixtures) moves to idle
    # here, so a later `agent get` re-check (the pre-close freshness recheck) sees it consistently,
    # not whatever stale status it was seeded with.
    name = a[2]
    for ag in state.get("agents", []):
        if ag.get("name") == name:
            ag["agent_status"] = "idle"
    _save(state)
    print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
elif a[:2] == ["agent", "get"]:
    name = a[2]
    known = next((ag for ag in state.get("agents", []) if ag.get("name") == name), None)
    status = known.get("agent_status", "idle") if known else "idle"
    agent = {"agent_status": status}
    if known:
        pane = known.get("pane_id")
        agent.update(pane_id=pane, tokens=state["tokens"].get(pane, {}), workspace_id=pane.split(":")[0],
                     tab_id=pane.split(":")[0] + ":t1", terminal_id="term_" + pane,
                     agent_session={"value": "native-" + name})
    print(json.dumps({"result": {"agent": agent}}))
elif a[:2] == ["agent", "list"]:
    print(json.dumps({"result": {"agents": state.get("agents", [])}}))
elif a[:2] == ["agent", "read"]:
    assert "--source" in a and "recent-unwrapped" in a, "bridge must use recent-unwrapped as the read source"
    report = state.get("report", "Card done: fixed the thing.")
    print(json.dumps({"result": {"text": report}}))
elif a[:2] == ["pane", "close"]:
    print(json.dumps({"result": {}}))
else:
    sys.stderr.write(json.dumps({"error": {"code": "unknown_command", "message": " ".join(a)}}))
    sys.exit(1)
'''

_UNREACHABLE_STUB = r'''
import sys
sys.stderr.write("connection refused: herdr server is not running\n")
sys.exit(1)
'''

_SYNTAX_ERROR_STUB = r'''
import sys
sys.stderr.write("unrecognized arguments: --bogus-flag\n")
sys.exit(2)
'''

_AGENT_NOT_READY_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "start"]:
    sys.stderr.write(json.dumps({"error": {"code": "agent_not_ready", "message": "input not confirmed"}}))
    sys.exit(1)
elif a[:2] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": {"agent_status": "starting"}}}))
'''

_AGENT_START_TRULY_MISSING_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "start"]:
    sys.stderr.write(json.dumps({"error": {"code": "agent_not_ready", "message": "input not confirmed"}}))
    sys.exit(1)
elif a[:2] == ["agent", "get"]:
    # Verified live (herdr 0.9.3): a truly missing agent is this structured error, not an empty
    # agent object -- see test_agent_status_reports_empty_for_a_truly_missing_agent_rather_than_raising.
    sys.stderr.write(json.dumps({"error": {"code": "agent_not_found", "message": "agent target titan-t_x not found"}}))
    sys.exit(1)
'''

_AGENT_BLOCKED_ON_PROMPT_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "prompt"]:
    sys.stderr.write(json.dumps({"error": {"code": "agent_blocked", "message": "awaiting approval"}}))
    sys.exit(1)
'''

_AGENT_PROMPT_STALLED_BUT_DELIVERED_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "prompt"]:
    sys.stderr.write(json.dumps({"error": {"code": "agent_prompt_stalled", "message": "no activity in 5s"}}))
    sys.exit(1)
elif a[:2] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": {"agent_status": "working"}}}))
'''

_AGENT_PROMPT_STALLED_NOT_DELIVERED_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "prompt"]:
    sys.stderr.write(json.dumps({"error": {"code": "agent_prompt_stalled", "message": "no activity in 5s"}}))
    sys.exit(1)
elif a[:2] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
'''

_WRONG_BOARD_TAG_STUB = _STUB_PRELUDE + r'''
# Simulates a server that silently ignores the metadata write (or a pane that was tagged for a
# different board earlier and never actually got re-tagged): `pane get` always reads back a
# `gsd`-board tag no matter what `report-metadata` was just asked to set.
if a[:2] == ["pane", "report-metadata"]:
    pass
elif a[:2] == ["pane", "get"]:
    pane_id = a[2]
    print(json.dumps({"result": {"pane": {"id": pane_id, "tokens": {"kanban_task": "t_cb0795f9", "kanban_board": "gsd"}}}}))
'''


def _stub(tmp_path, monkeypatch, script_text, *, name="stub_herdr.py"):
    script = tmp_path / name
    script.write_text(script_text, encoding="utf-8")
    state = tmp_path / "stub_state.json"
    monkeypatch.setenv("STUB_STATE", str(state))
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")
    return state


def _set_stub_report(state_path, text):
    """The stub creates its state file on first call; seed it so a test can pre-set the report."""
    path = Path(state_path)
    state = json.loads(path.read_text()) if path.exists() else {"tokens": {}, "calls": [], "workspaces": []}
    state["report"] = text
    path.write_text(json.dumps(state))


def _set_stub_report_file(state_path, summary, *, status="complete", deliverables="- [x] the fix"):
    """What the cooperative stub agent writes to this run's report file when prompted."""
    path = Path(state_path)
    state = json.loads(path.read_text()) if path.exists() else {"tokens": {}, "calls": [], "workspaces": []}
    state["report_file"] = (f"## Status\n{status}\n## Reply\nnone\n## Deliverables\n{deliverables}\n"
                            f"## Summary\n{summary}\n")
    path.write_text(json.dumps(state))


@pytest.fixture
def happy_herdr(tmp_path, monkeypatch):
    return _stub(tmp_path, monkeypatch, _HAPPY_STUB)


@pytest.fixture
def unreachable_herdr(tmp_path, monkeypatch):
    script = tmp_path / "stub_herdr_down.py"
    script.write_text(_UNREACHABLE_STUB, encoding="utf-8")
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_pane_name_follows_board_task_id_convention():
    assert bridge.pane_name("titan", "t_cb0795f9") == "titan-t_cb0795f9"


def test_pane_name_sanitizes_names_that_would_violate_herdrs_pattern():
    # A board/task combo long enough to exceed Herdr's 32-char name pattern must not raise;
    # it is sanitized to something that still matches [a-z][a-z0-9_-]{0,31}.
    name = bridge.pane_name("titan", "t_" + "f" * 40)
    assert bridge._NAME_RE.match(name)


def test_happy_path_drives_full_lifecycle_and_returns_report(happy_herdr):
    success, report = bridge.run(
        "t_cb0795f9", "titan", "C:\\workspace\\t_cb0795f9", "claude-sonnet-5-5", "medium",
    )
    assert success is True
    assert report == "Card done: fixed the thing."

    calls = json.loads(happy_herdr.read_text())["calls"]
    kinds = [tuple(c[:2]) for c in calls]
    assert ("workspace", "list") in kinds
    assert ("workspace", "create") in kinds  # no "titan"-labelled workspace existed yet
    assert ("tab", "create") in kinds
    assert ("pane", "report-metadata") in kinds
    assert ("agent", "list") in kinds  # checked for a surviving agent from a prior crashed attempt
    assert ("agent", "start") in kinds
    assert ("agent", "prompt") in kinds
    assert ("agent", "wait") in kinds
    assert ("agent", "read") in kinds
    assert ("pane", "close") in kinds  # a genuinely settled agent's pane is closed
    assert kinds.count(("pane", "get")) >= 1  # the link must have been verified by reading it back
    # Closing a pane tears down the agent living in it (verified live) -- the report must be read
    # BEFORE the work pane closes, or the read-back gets agent_not_found instead of the
    # transcript. (The *first* "pane close" here is the unrelated stale-root-pane cleanup that
    # happens before the agent even starts; the work pane's close is the last one.)
    assert kinds.index(("agent", "read")) < len(kinds) - 1 - kinds[::-1].index(("pane", "close"))


def test_ensure_board_workspace_reuses_an_existing_label_without_creating(happy_herdr):
    """Herdr workspaces are per-board already (the live install's real one is labelled "cic", not
    a generic "default") -- a board whose workspace already exists must not call `workspace
    create` again, and no stale root pane needs closing since nothing new was created."""
    state = {"tokens": {}, "calls": [], "workspaces": [{"label": "titan", "workspace_id": "wTitan"}]}
    happy_herdr.write_text(json.dumps(state), encoding="utf-8")

    workspace_id, stale_root_pane_id = bridge.ensure_board_workspace("titan")

    # The real `tab create --workspace` call requires the id, not the label (verified live
    # against herdr 0.9.3: passing the label there fails workspace_not_found).
    assert workspace_id == "wTitan"
    assert stale_root_pane_id is None
    calls = json.loads(happy_herdr.read_text())["calls"]
    assert ("workspace", "create") not in [tuple(c[:2]) for c in calls]


def test_ensure_board_workspace_creates_when_missing(happy_herdr):
    """A just-created workspace's id (not its label) is returned, along with the id of the
    throwaway root pane `workspace create` mints -- which the caller must close only after a real
    pane exists in that workspace (closing it any earlier deletes the whole empty workspace)."""
    workspace_id, stale_root_pane_id = bridge.ensure_board_workspace("titan")
    assert workspace_id == "wtitan"
    assert stale_root_pane_id == "w1:p1"
    calls = json.loads(happy_herdr.read_text())["calls"]
    create_call = next(c for c in calls if tuple(c[:2]) == ("workspace", "create"))
    assert create_call[create_call.index("--label") + 1] == "titan"


def test_resolve_workspace_id_accepts_either_label_or_id(happy_herdr):
    """HERMES_KANBAN_HERDR_WORKSPACE may be set to either a label or an id -- both resolve to
    the real id `tab create --workspace` requires."""
    state = {"tokens": {}, "calls": [], "workspaces": [{"label": "cic", "workspace_id": "wB"}]}
    happy_herdr.write_text(json.dumps(state), encoding="utf-8")

    assert bridge.resolve_workspace_id("cic") == "wB"
    assert bridge.resolve_workspace_id("wB") == "wB"
    with pytest.raises(bridge.BridgeError):
        bridge.resolve_workspace_id("no-such-workspace")


def test_link_pane_verifies_the_tag_was_actually_set(happy_herdr):
    bridge.link_pane("w1:p2", "t_cb0795f9", "titan")  # must not raise
    state = json.loads(happy_herdr.read_text())
    assert state["tokens"]["w1:p2"]["kanban_task"] == "t_cb0795f9"
    assert state["tokens"]["w1:p2"]["kanban_board"] == "titan"


def test_link_pane_rejects_a_tag_from_the_wrong_board(tmp_path, monkeypatch):
    """A pane that reads back with the right task id but a different board's tag (the metadata
    write didn't actually take, or the pane was tagged for another board earlier) must not verify
    -- both tokens are checked, not just the task."""
    _stub(tmp_path, monkeypatch, _WRONG_BOARD_TAG_STUB)
    with pytest.raises(bridge.BridgeError):
        bridge.link_pane("w1:p2", "t_cb0795f9", "titan")


def test_create_pane_reads_pane_id_from_root_pane(happy_herdr):
    assert bridge.create_pane("titan-t_x", "C:\\ws", herdr_workspace="titan") == "w1:p2"


def test_start_agent_passes_through_the_given_kind(happy_herdr):
    bridge.start_agent("titan-t_x", "w1:p2", "gpt-6.1-sol", "medium", kind="codex")
    calls = json.loads(happy_herdr.read_text())["calls"]
    start_call = next(c for c in calls if tuple(c[:2]) == ("agent", "start"))
    assert start_call[start_call.index("--kind") + 1] == "codex"


def test_run_passes_the_given_kind_through_to_agent_start(happy_herdr):
    """``run()`` has no board->kind table of its own: whatever ``kind`` its caller (the
    dispatcher, resolved from kanban.herdr_board_agents) passes in reaches ``agent start``
    unchanged, for any installed herdr agent kind, not just claude/codex."""
    success, _report = bridge.run(
        "t_x", "p5", "C:\\workspace\\t_x", "gpt-6.1-sol", "medium", kind="codex",
    )
    assert success is True
    calls = json.loads(happy_herdr.read_text())["calls"]
    start_call = next(c for c in calls if tuple(c[:2]) == ("agent", "start"))
    assert start_call[start_call.index("--kind") + 1] == "codex"
    assert start_call[start_call.index("--model") + 1] == "gpt-6.1-sol"


def test_run_defaults_to_claude_kind_when_caller_does_not_specify_one(happy_herdr):
    success, _report = bridge.run("t_x", "titan", "C:\\workspace\\t_x", "claude-sonnet-5-5", "medium")
    assert success is True
    calls = json.loads(happy_herdr.read_text())["calls"]
    start_call = next(c for c in calls if tuple(c[:2]) == ("agent", "start"))
    assert start_call[start_call.index("--kind") + 1] == "claude"


def test_find_existing_agent_returns_none_when_no_agent_matches(happy_herdr):
    assert bridge.find_existing_agent("titan-t_cb0795f9") is None


def test_run_reuses_a_surviving_agent_instead_of_spawning_a_second_one(happy_herdr):
    """A dispatcher retry after this bridge's own process crashed mid-run must not create a
    second pane/agent onto the same name. It re-tags the live one, lets its earlier work settle,
    then sends THIS run's fenced assignment: the earlier run's output is never this run's result
    (t_6465d3ec run 114 adopted run 113's scrollback as success)."""
    state = {"tokens": {}, "calls": [], "workspaces": [],
              "agents": [{"name": "titan-t_cb0795f9", "pane_id": "w1:p9", "agent_status": "working"}]}
    happy_herdr.write_text(json.dumps(state), encoding="utf-8")

    success, report = bridge.run(
        "t_cb0795f9", "titan", "C:\\workspace\\t_cb0795f9", "claude-sonnet-5-5", "medium",
    )
    assert success is True
    assert report == "Card done: fixed the thing."

    kinds = [tuple(c[:2]) for c in json.loads(happy_herdr.read_text())["calls"]]
    assert ("workspace", "create") not in kinds
    assert ("tab", "create") not in kinds
    assert ("agent", "start") not in kinds
    assert kinds.index(("agent", "wait")) < kinds.index(("agent", "prompt"))


def test_run_reprompts_a_reused_agent_that_is_idle_rather_than_assuming_it_already_ran(happy_herdr):
    """A crashed prior attempt could have started this agent and never actually prompted it (the
    crash window is between those two calls) -- an `idle` status on reuse can't be trusted to mean
    "already handled this", or a never-prompted agent's irrelevant transcript gets reported as a
    false success. Unlike a genuinely mid-flight agent (working/busy/blocked), idle gets the task
    (re-)sent before waiting."""
    state = {"tokens": {}, "calls": [], "workspaces": [],
              "agents": [{"name": "titan-t_cb0795f9", "pane_id": "w1:p9", "agent_status": "idle"}]}
    happy_herdr.write_text(json.dumps(state), encoding="utf-8")

    success, report = bridge.run(
        "t_cb0795f9", "titan", "C:\\workspace\\t_cb0795f9", "claude-sonnet-5-5", "medium",
    )
    assert success is True, report

    kinds = [tuple(c[:2]) for c in json.loads(happy_herdr.read_text())["calls"]]
    assert ("workspace", "create") not in kinds
    assert ("tab", "create") not in kinds
    assert ("agent", "start") not in kinds
    assert ("agent", "prompt") in kinds


_REUSE_WRONG_TAG_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "list"]:
    print(json.dumps({"result": {"agents": [{"name": "titan-t_cb0795f9", "pane_id": "w1:p9", "agent_status": "working"}]}}))
elif a[:2] == ["pane", "report-metadata"]:
    pass
elif a[:2] == ["pane", "get"]:
    print(json.dumps({"result": {"pane": {"id": a[2], "tokens": {"kanban_task": "t_cb0795f9", "kanban_board": "gsd"}}}}))
'''


def test_run_refuses_to_reuse_an_agent_whose_pane_tag_wont_verify(tmp_path, monkeypatch):
    """A name match alone isn't enough to treat a live agent as this card's own -- if its pane's
    tag won't verify (wrong board here), that must fail closed, not silently proceed to wait on
    and report an unverifiable agent's transcript as this card's result."""
    _stub(tmp_path, monkeypatch, _REUSE_WRONG_TAG_STUB)
    success, report = bridge.run(
        "t_cb0795f9", "titan", "C:\\workspace\\t_cb0795f9", "claude-sonnet-5-5", "medium",
    )
    assert success is False
    assert "already tagged for a different card" in report.lower(), report


_REUSE_NO_PANE_ID_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "list"]:
    print(json.dumps({"result": {"agents": [{"name": "titan-t_cb0795f9", "agent_status": "working"}]}}))
'''


def test_run_refuses_to_reuse_an_agent_with_no_reported_pane_id(tmp_path, monkeypatch):
    """A bare name match in `agent list` with no pane_id can't be ownership-checked at all -- that
    must fail closed rather than silently trust the name match alone."""
    _stub(tmp_path, monkeypatch, _REUSE_NO_PANE_ID_STUB)
    success, report = bridge.run(
        "t_cb0795f9", "titan", "C:\\workspace\\t_cb0795f9", "claude-sonnet-5-5", "medium",
    )
    assert success is False
    assert "no pane_id" in report.lower(), report


def test_agent_not_ready_is_not_fatal_when_agent_is_present(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _AGENT_NOT_READY_STUB)
    bridge.start_agent("titan-t_x", "w1:p2", "claude-sonnet-5-5", "medium")  # must not raise


def test_agent_not_ready_is_fatal_when_agent_is_truly_absent(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _AGENT_START_TRULY_MISSING_STUB)
    with pytest.raises(bridge.BridgeError):
        bridge.start_agent("titan-t_x", "w1:p2", "claude-sonnet-5-5", "medium")


_AGENT_NOT_READY_EMPTY_STATUS_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "start"]:
    sys.stderr.write(json.dumps({"error": {"code": "agent_not_ready", "message": "input not confirmed"}}))
    sys.exit(1)
elif a[:2] == ["agent", "get"]:
    # The agent genuinely exists (pane_id present, matching) but its status field is empty --
    # agent_status() alone can't tell this apart from "doesn't exist" (both read as falsy).
    print(json.dumps({"result": {"agent": {"agent_status": "", "pane_id": "w1:p2"}}}))
'''


def test_agent_not_ready_is_not_fatal_when_agent_exists_with_an_empty_status(tmp_path, monkeypatch):
    """An existing agent with an empty/unrecognized status field is not the same thing as no agent
    at all -- agent_status()'s single falsy return conflates the two, so this must go through the
    existence-aware _agent_info check instead, not agent_status()."""
    _stub(tmp_path, monkeypatch, _AGENT_NOT_READY_EMPTY_STATUS_STUB)
    bridge.start_agent("titan-t_x", "w1:p2", "claude-sonnet-5-5", "medium")  # must not raise


_AGENT_NOT_READY_WRONG_PANE_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "start"]:
    sys.stderr.write(json.dumps({"error": {"code": "agent_not_ready", "message": "input not confirmed"}}))
    sys.exit(1)
elif a[:2] == ["agent", "get"]:
    # A same-named agent exists, but in a DIFFERENT pane than the one just created here.
    print(json.dumps({"result": {"agent": {"agent_status": "idle", "pane_id": "w9:p9"}}}))
'''


def test_agent_not_ready_is_fatal_when_the_same_named_agent_is_in_a_different_pane(tmp_path, monkeypatch):
    """A name match isn't a pane match -- this bridge tracks state by pane_id, so a same-named
    agent reported in some other pane isn't this card's agent and must not be silently adopted."""
    _stub(tmp_path, monkeypatch, _AGENT_NOT_READY_WRONG_PANE_STUB)
    with pytest.raises(bridge.BridgeError, match="not the pane just created"):
        bridge.start_agent("titan-t_x", "w1:p2", "claude-sonnet-5-5", "medium")


_START_TIMEOUT_BUT_AGENT_EXISTS_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["workspace", "list"]:
    print(json.dumps({"result": {"workspaces": [{"label": "titan", "workspace_id": "wTitan"}]}}))
elif a[:2] == ["tab", "create"]:
    print(json.dumps({"result": {"tab": {"tab_id": "t2"}, "root_pane": {"pane_id": "w1:p2"}}}))
elif a[:2] == ["agent", "list"]:
    print(json.dumps({"result": {"agents": []}}))
elif a[:2] == ["pane", "report-metadata"]:
    pass
elif a[:2] == ["pane", "get"]:
    print(json.dumps({"result": {"pane": {"tokens": {"kanban_task": "t_cb0795f9", "kanban_board": "titan"}}}}))
elif a[:2] == ["agent", "start"]:
    # A bare CLI timeout -- doesn't prove the server never created the agent.
    sys.stderr.write(json.dumps({"error": {"code": "timeout", "message": "start response lost"}}))
    sys.exit(1)
elif a[:2] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": {"agent_status": "working"}}}))
elif a[:2] == ["pane", "close"]:
    sys.stderr.write(json.dumps({"error": {"code": "bug", "message": "must not be called"}}))
    sys.exit(1)
'''


def test_run_preserves_the_pane_when_start_agent_times_out_but_the_agent_exists(tmp_path, monkeypatch):
    """A `start` timeout doesn't prove the server never created the agent -- it may have started
    despite a lost/slow response. Closing the pane in that case would discard a live executor, the
    exact P1 this whole fix round exists to prevent. Must check before discarding."""
    _stub(tmp_path, monkeypatch, _START_TIMEOUT_BUT_AGENT_EXISTS_STUB)
    success, report = bridge.run(
        "t_cb0795f9", "titan", "C:\\workspace\\t_cb0795f9", "claude-sonnet-5-5", "medium",
    )
    assert success is False
    assert "pane is preserved" in report.lower(), report


_AGENT_NOT_FOUND_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "get"]:
    sys.stderr.write(json.dumps({"error": {"code": "agent_not_found", "message": "agent target x not found"}}))
    sys.exit(1)
'''


def test_agent_status_reports_empty_for_a_truly_missing_agent_rather_than_raising(tmp_path, monkeypatch):
    """Verified live (herdr 0.9.3): `agent get` on a name with no agent returns the structured
    error `agent_not_found`, not an empty/absent result. `start_agent`'s own `agent_not_ready`
    fallback calls `agent_status` expecting a falsy return for "not present yet", not a crash."""
    _stub(tmp_path, monkeypatch, _AGENT_NOT_FOUND_STUB)
    assert bridge.agent_status("titan-t_x") == ""


def test_agent_blocked_on_prompt_is_surfaced_not_auto_answered(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _AGENT_BLOCKED_ON_PROMPT_STUB)
    with pytest.raises(bridge.AgentBlockedError):
        bridge.prompt_agent("titan-t_x", "work kanban task t_x")


def test_prompt_stalled_but_actually_delivered_is_not_treated_as_failure(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _AGENT_PROMPT_STALLED_BUT_DELIVERED_STUB)
    bridge.prompt_agent("titan-t_x", "work kanban task t_x")  # must not raise -- agent is busy


def test_prompt_stalled_and_not_delivered_is_a_real_failure(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _AGENT_PROMPT_STALLED_NOT_DELIVERED_STUB)
    with pytest.raises(bridge.BridgeError):
        bridge.prompt_agent("titan-t_x", "work kanban task t_x")


def test_cli_syntax_error_exit_2_is_a_plain_bridge_error_not_a_herdr_cli_error(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _SYNTAX_ERROR_STUB, name="stub_syntax_error.py")
    with pytest.raises(bridge.BridgeError) as exc_info:
        bridge._run_herdr(["agent", "start", "x"])
    assert not isinstance(exc_info.value, bridge.HerdrCliError)


def test_herdr_unreachable_fails_cleanly_without_hanging(unreachable_herdr):
    success, report = bridge.run(
        "t_cb0795f9", "titan", "C:\\workspace\\t_cb0795f9", "claude-sonnet-5-5", "medium",
    )
    assert success is False
    assert "herdr" in report.lower() or "connection refused" in report.lower()


_SETTLE_BLOCKED_ON_RECHECK_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "wait"]:
    print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
elif a[:2] == ["agent", "get"]:
    # Flips to blocked between the wait returning and the pre-close recheck.
    print(json.dumps({"result": {"agent": {"agent_status": "blocked"}}}))
elif a[:2] == ["agent", "read"]:
    print(json.dumps({"result": {"text": "needs approval to continue"}}))
elif a[:2] == ["pane", "close"]:
    sys.stderr.write(json.dumps({"error": {"code": "bug", "message": "must not be called"}}))
    sys.exit(1)
'''


def test_settle_and_close_preserves_the_pane_when_still_blocked_on_wait(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _SETTLE_BLOCKED_ON_RECHECK_STUB)
    success, report, pane_preserved = bridge._settle_and_close("titan-t_x", "w1:p2")
    assert success is False
    assert pane_preserved is True
    assert "blocked" in report.lower()


def test_settle_and_close_rechecks_status_right_before_closing(tmp_path, monkeypatch):
    """`wait` can itself time out and return a stale settled-looking read, or the agent can flip
    back to working between the wait returning and the close -- the pane must not be closed on a
    stale status read."""
    _stub(tmp_path, monkeypatch, _SETTLE_BLOCKED_ON_RECHECK_STUB)
    # The "must not be called" pane-close stub above proves close_pane was never invoked: if it
    # had been, `_run_herdr` would surface that stub's error as a BridgeError and fail this test.
    bridge._settle_and_close("titan-t_x", "w1:p2")


_SETTLE_UNKNOWN_STATUS_ON_RECHECK_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "wait"]:
    print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
elif a[:2] == ["agent", "get"]:
    # An unrecognized status (not idle/done/finished, and not one of the obviously-unsafe ones
    # either) on the pre-close recheck -- a future herdr status this file doesn't know about yet,
    # or the agent genuinely gone.
    print(json.dumps({"result": {"agent": {"agent_status": "paused"}}}))
elif a[:2] == ["agent", "read"]:
    print(json.dumps({"result": {"text": "unclear"}}))
elif a[:2] == ["pane", "close"]:
    sys.stderr.write(json.dumps({"error": {"code": "bug", "message": "must not be called"}}))
    sys.exit(1)
'''


def test_settle_and_close_preserves_the_pane_on_an_unrecognized_status_not_just_known_unsafe_ones(
    tmp_path, monkeypatch,
):
    """The pre-close recheck must require the live status to be positively confirmed settled
    (idle/done/finished), not merely absent from a hardcoded list of known-unsafe statuses -- an
    empty, "unknown", or any future status this file doesn't recognize must fail closed, not fall
    through to "safe to close" by default."""
    _stub(tmp_path, monkeypatch, _SETTLE_UNKNOWN_STATUS_ON_RECHECK_STUB)
    success, report, pane_preserved = bridge._settle_and_close("titan-t_x", "w1:p2")
    assert success is False
    assert pane_preserved is True
    assert "not confirmed settled" in report.lower()


_SETTLE_RESUMED_DURING_READ_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "wait"]:
    print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
elif a[:2] == ["agent", "get"]:
    # First recheck (right after wait): still idle. Second recheck (right before close, after the
    # read): resumed to working -- simulating the agent picking work back up during the read call.
    prior_gets = sum(1 for c in state["calls"][:-1] if c[:2] == ["agent", "get"])
    status = "idle" if prior_gets == 0 else "working"
    print(json.dumps({"result": {"agent": {"agent_status": status}}}))
elif a[:2] == ["agent", "read"]:
    print(json.dumps({"result": {"text": "finished"}}))
elif a[:2] == ["pane", "close"]:
    sys.stderr.write(json.dumps({"error": {"code": "bug", "message": "must not be called"}}))
    sys.exit(1)
'''


def test_settle_and_close_preserves_the_pane_if_the_agent_resumes_during_the_report_read(
    tmp_path, monkeypatch,
):
    """`read_agent` is itself a real call that can take time -- an agent that resumes work during
    that read must not still get its pane closed out from under it. The read already succeeded, so
    this is still a reported success; only the pane's fate changes."""
    _stub(tmp_path, monkeypatch, _SETTLE_RESUMED_DURING_READ_STUB)
    success, report, pane_preserved = bridge._settle_and_close("titan-t_x", "w1:p2")
    assert success is True
    assert pane_preserved is True
    assert report == "finished"


_SETTLE_CLOSE_FAILS_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "wait"]:
    print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
elif a[:2] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
elif a[:2] == ["agent", "read"]:
    print(json.dumps({"result": {"text": "done"}}))
elif a[:2] == ["pane", "close"]:
    sys.stderr.write(json.dumps({"error": {"code": "pane_close_failed", "message": "herdr could not close the pane"}}))
    sys.exit(1)
'''


def test_settle_and_close_reports_pane_preserved_when_close_itself_fails(tmp_path, monkeypatch):
    """A real close failure (not just "it was already gone") must not be silently treated as
    cleaned up -- the caller needs to know the pane may still be live."""
    _stub(tmp_path, monkeypatch, _SETTLE_CLOSE_FAILS_STUB)
    success, report, pane_preserved = bridge._settle_and_close("titan-t_x", "w1:p2")
    assert success is True
    assert pane_preserved is True
    assert report == "done"


def test_annotate_preserved_pane_surfaces_the_signal_only_on_a_preserved_success():
    """Computing pane_preserved and then dropping it on the floor would make all of
    _settle_and_close's TOCTOU/close-failure handling pointless -- nothing downstream (the card
    summary a human reads) would ever learn the pane needs a look."""
    assert bridge._annotate_preserved_pane(True, "report body", False) == "report body"
    assert bridge._annotate_preserved_pane(False, "report body", True) == "report body"
    annotated = bridge._annotate_preserved_pane(True, "report body", True)
    assert "report body" in annotated
    assert "manual follow-up" in annotated.lower()


_REUSE_CLOSE_FAILS_STUB = _STUB_PRELUDE + r'''
if a[:2] == ["agent", "list"]:
    print(json.dumps({"result": {"agents": [{"name": "titan-t_cb0795f9", "pane_id": "w1:p9", "agent_status": "working"}]}}))
elif a[:2] == ["pane", "report-metadata"]:
    pass
elif a[:2] == ["pane", "get"]:
    print(json.dumps({"result": {"pane": {"tokens": {"kanban_task": "t_cb0795f9", "kanban_board": "titan"}}}}))
elif a[:2] == ["agent", "wait"]:
    print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
elif a[:2] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
elif a[:2] == ["agent", "read"]:
    print(json.dumps({"result": {"text": "Card done: fixed the thing."}}))
elif a[:2] == ["pane", "close"]:
    sys.stderr.write(json.dumps({"error": {"code": "pane_close_failed", "message": "herdr could not close the pane"}}))
    sys.exit(1)
'''


def test_run_surfaces_a_preserved_pane_in_its_returned_report(tmp_path, monkeypatch):
    """End-to-end: a reused agent whose pane fails to close on a genuine success still reports
    that the pane needs a look, instead of a clean-looking report that hides it."""
    _stub(tmp_path, monkeypatch, _REUSE_CLOSE_FAILS_STUB)
    success, report = bridge.run(
        "t_cb0795f9", "titan", "C:\\workspace\\t_cb0795f9", "claude-sonnet-5-5", "medium",
    )
    assert success is True
    assert "Card done: fixed the thing." in report
    assert "manual follow-up" in report.lower()


def test_compose_prompt_falls_back_to_the_bare_instruction_when_the_board_is_unreachable():
    # No kanban DB fixture is set up here -- kbc.connect() fails, and the fallback must still work.
    assert bridge._compose_prompt("t_cb0795f9", "no-such-board") == "work kanban task t_cb0795f9"


def test_compose_prompt_enriches_with_the_cards_title_and_body(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Fix the thing", body="Do the specific repro steps.")
    prompt = bridge._compose_prompt(tid, kb.DEFAULT_BOARD)
    assert prompt.startswith(f"work kanban task {tid}")
    assert "Fix the thing" in prompt
    assert "Do the specific repro steps." in prompt


def test_herdr_bin_not_found_raises_bridge_error(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # a PATH with no herdr on it
    # This machine has a real Herdr install under the real user's home (the whole premise of the
    # card); also redirect the bridge's "~/.herdr/packages/standalone/releases/*" fallback glob
    # away from it so this unit test exercises "truly not found", not this machine's real install.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(bridge.BridgeError):
        bridge.herdr_command()


def test_main_happy_path_records_receipt_and_the_dispatcher_sweep_enters_review(happy_herdr, kanban_home, monkeypatch):
    """Per the herdr-dispatch skill ("finish agent work in Review, never Done"): the bridge only
    records a durable run-scoped receipt for the exact settled native agent; the dispatcher
    settlement sweep (not the bridge) moves the card, and success lands on `review`, never `done`."""
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="direct herdr card", assignee="default")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        workspace = kanban_home.parent / "workspace"
        workspace.mkdir()
        (workspace / "DELIVERABLE.md").write_text("reviewable artifact")
        _set_stub_report_file(happy_herdr, f"Final deliverable: {workspace / 'DELIVERABLE.md'}")
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

        exit_code = bridge.main()
        assert exit_code == 0
        assert kb.get_task(conn, tid).status == "running"  # the bridge itself never transitions the card
        receipts = [e for e in kb.list_events(conn, tid) if e.kind == "worker_result_receipt"]
        assert receipts and receipts[0].payload["artifacts"], receipts and receipts[0].payload

        kbd.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None)  # admission disabled
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.status == "review"
    # Attachment copying (kernel _stage_completion_artifacts) applies to scratch-workspace artifacts
    # only; this test's workspace is external, so assert the durable Review reference instead.
    reviewed = [e for e in events if e.kind == "review_requested"]
    assert reviewed and any("DELIVERABLE.md" in p for p in reviewed[0].payload["artifacts"])
    summaries = " ".join(str((e.payload or {}).get("summary", "")) for e in events)
    assert "DELIVERABLE.md" in summaries
    # The executor was verified and recorded on the card before the agent got its assignment.
    linked = [e for e in events if e.kind == "executor_linked"]
    assert linked and linked[0].payload["native_session"] and linked[0].payload["fence"].startswith("run-")
    with kbc.connect() as conn:
        refs = conn.execute("SELECT ref FROM task_refs WHERE task_id = ? AND ref_type = 'herdr'", (tid,)).fetchall()
    assert [r["ref"] for r in refs] == [linked[0].payload["ref"]]


def test_success_receipt_requires_the_exact_settled_native_agent(happy_herdr, kanban_home, tmp_path):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fence", assignee="default")
        claimed = kb.claim_task(conn, tid)
        run_id = claimed.current_run_id
    ws = tmp_path / "ws-fence"
    ws.mkdir()
    (ws / "D.md").write_text("artifact")
    report = f"done {ws / 'D.md'}"
    # No native agent named: refused, nothing recorded.
    with pytest.raises(bridge.BridgeError):
        bridge._mark_card(tid, None, run_id, True, report, workspace_dir=str(ws), settlement_owner=True)
    # Not owned by the dispatcher settlement: refused.
    with pytest.raises(bridge.BridgeError):
        bridge._mark_card(tid, None, run_id, True, report, workspace_dir=str(ws), agent_name="x")
    with kbc.connect() as conn:
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "worker_result_receipt"]


def test_main_withholds_review_without_a_real_artifact(happy_herdr, kanban_home, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="handoff only", assignee="default")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        workspace = kanban_home.parent / "workspace-handoff-only"
        workspace.mkdir()
        _set_stub_report_file(happy_herdr, "Handoff written; ready for Review.")
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

        exit_code = bridge.main()
        assert exit_code == 0  # receipt recorded; the DISPATCHER decides the card outcome
        assert kb.get_task(conn, tid).status == "running"
        kbd.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        runs = kb.list_runs(conn, tid)
    assert task is not None
    # no verified deliverable: settled as a truthful partial (owner kept), never Review
    assert task.status == "ready"
    assert task.block_kind is None
    assert runs[-1].outcome == "partial_result"
    assert any(e.kind == "partial_result_detail" for e in events)
    assert not any(e.kind == "review_requested" for e in events)
    assert not kb.list_attachments(conn, tid)


def test_main_exits_nonzero_when_the_card_transition_is_rejected(happy_herdr, kanban_home, monkeypatch):
    """A stale/duplicate `HERMES_KANBAN_RUN_ID` (the claim moved on since this run started) must
    not be swallowed as a successful exit -- `request_review`'s boolean return is checked, not
    just whether it raised."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="direct herdr card", assignee="default")
        kb.claim_task(conn, tid)  # a real run exists; HERMES_KANBAN_RUN_ID below deliberately isn't it
        workspace = kanban_home.parent / "workspace3"
        workspace.mkdir()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999999")  # does not match the real current run

        exit_code = bridge.main()

        task = kb.get_task(conn, tid)
    assert exit_code == 1
    assert task.status == "running"  # the rejected transition left the card exactly where it was


def test_main_failure_before_any_agent_records_a_truthful_failure_receipt(unreachable_herdr, kanban_home, monkeypatch):
    """Herdr unreachable: no agent ever ran. The bridge records its own failure reason as the run's
    receipt (it used to require a settled agent identity even to fail, so every such run crashed
    unrecorded and was retried blind: t_6465d3ec runs 97-113). Nothing reaches Review or Done."""
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="direct herdr card (down)", assignee="default")
        claimed = kb.claim_task(conn, tid)
        workspace = kanban_home.parent / "workspace2"
        workspace.mkdir()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

        assert bridge.main() == 1
        receipts = [e for e in kb.list_events(conn, tid) if e.kind == "worker_result_receipt"]
        assert receipts and receipts[0].payload["success"] is False
        assert "herdr" in receipts[0].payload["report"].lower()
        kbd.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.status == "blocked" and task.block_kind == "transient"
    assert not any(e.kind in {"completed", "review_requested"} for e in events)


def test_main_exits_nonzero_without_required_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    assert bridge.main() == 1


def test_main_exits_nonzero_without_a_run_id(monkeypatch, tmp_path):
    # The dispatcher always sets this; a worker started any other way must refuse rather than
    # silently drop the run-ownership fence that protects a stale/duplicate run from overwriting a
    # card a newer run already owns.
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "titan")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert bridge.main() == 1


def test_main_exits_nonzero_with_a_malformed_run_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "titan")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "not-a-number")
    assert bridge.main() == 1


# --- fresh pane is not a usable shell yet (CS board agent_pane_busy blocks) -----------------

_FRESH_PANE_STUB = _HAPPY_STUB.replace(
    '''elif a[:2] == ["pane", "get"]:
    pane_id = a[2]
    print(json.dumps({"result": {"pane": {"id": pane_id, "tokens": state["tokens"].get(pane_id, {})}}}))''',
    '''elif a[:2] == ["pane", "get"]:
    pane_id = a[2]
    n = state.get("pane_gets", 0) + 1
    state["pane_gets"] = n
    _save(state)
    pane = {"id": pane_id, "tokens": state["tokens"].get(pane_id, {}), "revision": 1 if n > 3 else 0}
    print(json.dumps({"result": {"pane": pane}}))''',
).replace(
    '''elif a[:2] == ["agent", "start"]:
    print(json.dumps({"result": {"agent": {"name": a[2]}}}))''',
    '''elif a[:2] == ["agent", "start"]:
    starts = state.get("starts", 0) + 1
    state["starts"] = starts
    state["start_args"] = a
    _save(state)
    if starts <= int(os.environ.get("STUB_BUSY_STARTS", "0")):
        sys.stderr.write(json.dumps({"error": {"code": "agent_pane_busy",
                         "message": "agent target pane " + a[a.index("--pane") + 1] + " is not an available shell"}}))
        sys.exit(1)
    print(json.dumps({"result": {"agent": {"name": a[2]}}}))''',
)


def test_herdr_agent_profile_uses_dispatch_profile_home_for_board_defaults(monkeypatch, tmp_path):
    config = tmp_path / "profiles" / "cs-orchestrator" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("kanban:\n  herdr_board_agents:\n    cs:\n      kind: codex\n      model: gpt-6.1-sol\n      effort: low\n")
    monkeypatch.setenv("HERMES_PROFILE", "cs-orchestrator")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # custom root: profiles live under <root>/profiles
    assert bridge._herdr_agent_profile_for_worker(
        "cs", "claude", "claude-sonnet-5-5", "medium",
    ) == ("codex", "gpt-6.1-sol", "low")


def test_herdr_agent_profile_respects_per_task_overrides(monkeypatch, tmp_path):
    config = tmp_path / "profiles" / "cs-orchestrator" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("kanban:\n  herdr_board_agents:\n    cs:\n      kind: codex\n      model: gpt-6.1-sol\n      effort: low\n")
    monkeypatch.setenv("HERMES_PROFILE", "cs-orchestrator")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # custom root: profiles live under <root>/profiles
    assert bridge._herdr_agent_profile_for_worker(
        "cs", "claude", "task-model", "high", has_model_override=True, has_effort_override=True,
    ) == ("codex", "task-model", "high")


def test_herdr_agent_args_use_harness_specific_permission_bypass():
    assert bridge._herdr_agent_args("codex", "gpt-6.1-sol", "low") == [
        "--model", "gpt-6.1-sol", "-c", 'model_reasoning_effort="low"', "--yolo",
    ]
    assert bridge._herdr_agent_args("claude", "claude-sonnet-5-5", "medium") == [
        "--model", "claude-sonnet-5-5", "--effort", "medium", "--dangerously-skip-permissions",
    ]


def test_start_agent_uses_codex_yolo_without_unsupported_effort_flag(tmp_path, monkeypatch):
    state = _stub(tmp_path, monkeypatch, _HAPPY_STUB)
    bridge.start_agent("cs-t_x", "w1:p2", "gpt-6.1-sol", "low", kind="codex")
    args = json.loads(state.read_text())["calls"]
    start = next(call for call in args if call[:2] == ["agent", "start"])
    assert start[start.index("--") + 1:] == [
        "--model", "gpt-6.1-sol", "-c", 'model_reasoning_effort="low"', "--yolo",
    ]


def test_start_agent_uses_claude_skip_permissions(tmp_path, monkeypatch):
    state = _stub(tmp_path, monkeypatch, _HAPPY_STUB)
    bridge.start_agent("cs-t_x", "w1:p2", "claude-sonnet-5-5", "medium", kind="claude")
    args = json.loads(state.read_text())["calls"]
    start = next(call for call in args if call[:2] == ["agent", "start"])
    assert start[start.index("--") + 1:] == [
        "--model", "claude-sonnet-5-5", "--effort", "medium", "--dangerously-skip-permissions",
    ]


def test_wait_pane_shell_ready_polls_until_the_shell_has_drawn(tmp_path, monkeypatch):
    state = _stub(tmp_path, monkeypatch, _FRESH_PANE_STUB)
    monkeypatch.setattr(bridge, "_PANE_SHELL_POLL_S", 0)
    assert bridge.wait_pane_shell_ready("w1:p2", timeout_s=5) is True
    assert json.loads(state.read_text())["pane_gets"] == 4


def test_wait_pane_shell_ready_times_out_without_raising(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _FRESH_PANE_STUB)
    monkeypatch.setattr(bridge, "_PANE_SHELL_POLL_S", 0)
    assert bridge.wait_pane_shell_ready("w1:p2", timeout_s=0) is False


def test_start_agent_retries_agent_pane_busy_then_succeeds(tmp_path, monkeypatch):
    state = _stub(tmp_path, monkeypatch, _FRESH_PANE_STUB)
    monkeypatch.setenv("STUB_BUSY_STARTS", "2")
    monkeypatch.setattr(bridge, "_START_BUSY_BACKOFF_S", 0)
    bridge.start_agent("cs-t_x", "w1:p2", "claude-sonnet-5-5", "medium")
    assert json.loads(state.read_text())["starts"] == 3


def test_start_agent_gives_up_after_bounded_agent_pane_busy_retries(tmp_path, monkeypatch):
    state = _stub(tmp_path, monkeypatch, _FRESH_PANE_STUB)
    monkeypatch.setenv("STUB_BUSY_STARTS", "99")
    monkeypatch.setattr(bridge, "_START_BUSY_BACKOFF_S", 0)
    with pytest.raises(bridge.HerdrCliError) as exc:
        bridge.start_agent("cs-t_x", "w1:p2", "claude-sonnet-5-5", "medium")
    assert exc.value.code == "agent_pane_busy"
    assert json.loads(state.read_text())["starts"] == bridge._START_BUSY_RETRIES + 1


def test_run_waits_for_the_new_pane_shell_before_starting_the_agent(tmp_path, monkeypatch):
    state = _stub(tmp_path, monkeypatch, _FRESH_PANE_STUB)
    monkeypatch.setattr(bridge, "_PANE_SHELL_POLL_S", 0)
    success, _report = bridge.run("t_x", "cs", "/ws/t_x", "claude-sonnet-5-5", "medium")
    assert success is True
    calls = json.loads(state.read_text())["calls"]
    start_idx = next(i for i, c in enumerate(calls) if tuple(c[:2]) == ("agent", "start"))
    assert sum(1 for c in calls[:start_idx] if tuple(c[:2]) == ("pane", "get")) >= 4


def test_run_sends_no_work_to_an_executor_it_could_not_link(happy_herdr):
    """Startup invariant: tags verified, identity recorded on the card, THEN the prompt. A linkage
    failure leaves the agent idle and unprompted in its tagged pane for the next run to adopt."""
    from hermes_cli import kanban_direct_herdr_link as herdr_link

    def refuse(name, pane_id):
        raise herdr_link.LinkError("pane is not listed")

    success, report = bridge.run(
        "t_cb0795f9", "titan", "C:\\workspace\\t_cb0795f9", "claude-sonnet-5-5", "medium", link_fn=refuse,
    )
    assert success is False and "executor linkage failed before the assignment was sent" in report
    calls = json.loads(happy_herdr.read_text())["calls"]
    start = next(c for c in calls if c[:2] == ["agent", "start"])
    agent_pane = start[start.index("--pane") + 1]
    assert not [c for c in calls if c[:2] == ["agent", "prompt"]]
    assert ["pane", "close", agent_pane] not in [c[:3] for c in calls]
