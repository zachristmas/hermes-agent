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
elif a[:2] == ["agent", "prompt"]:
    print(json.dumps({"result": {}}))
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
    print(json.dumps({"result": {"agent": {"agent_status": status}}}))
elif a[:2] == ["agent", "list"]:
    print(json.dumps({"result": {"agents": state.get("agents", [])}}))
elif a[:2] == ["agent", "read"]:
    assert "--source" in a and "recent-unwrapped" in a, "bridge must use recent-unwrapped as the read source"
    print(json.dumps({"result": {"text": "Card done: fixed the thing."}}))
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
    print(json.dumps({"result": {"agent": {}}}))
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


def test_find_existing_agent_returns_none_when_no_agent_matches(happy_herdr):
    assert bridge.find_existing_agent("titan-t_cb0795f9") is None


def test_run_reuses_a_surviving_agent_instead_of_spawning_a_second_one(happy_herdr):
    """A dispatcher retry after this bridge's own process crashed mid-run must not create a
    second pane/agent onto the same name -- it re-tags and waits on the one already live."""
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
    assert ("agent", "prompt") not in kinds
    assert ("agent", "wait") in kinds


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
    assert "tag did not verify" in report.lower(), report


def test_agent_not_ready_is_not_fatal_when_agent_is_present(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _AGENT_NOT_READY_STUB)
    bridge.start_agent("titan-t_x", "w1:p2", "claude-sonnet-5-5", "medium")  # must not raise


def test_agent_not_ready_is_fatal_when_agent_is_truly_absent(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, _AGENT_START_TRULY_MISSING_STUB)
    with pytest.raises(bridge.BridgeError):
        bridge.start_agent("titan-t_x", "w1:p2", "claude-sonnet-5-5", "medium")


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


def test_main_happy_path_requests_review_and_exits_zero(happy_herdr, kanban_home, monkeypatch):
    """Per the herdr-dispatch skill ("finish agent work in Review, never Done"): a settled Herdr
    status is not proof the work was actually done correctly, so success lands on `review`, not
    `done`, for a human to judge."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="direct herdr card", assignee="default")
        claimed = kb.claim_task(conn, tid)
        workspace = kanban_home.parent / "workspace"
        workspace.mkdir()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

        exit_code = bridge.main()

        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert exit_code == 0
    assert task.status == "review"
    summaries = " ".join(str((e.payload or {}).get("summary", "")) for e in events)
    assert "Card done: fixed the thing." in summaries


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


def test_main_failure_path_blocks_card_and_exits_nonzero(unreachable_herdr, kanban_home, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="direct herdr card (down)", assignee="default")
        claimed = kb.claim_task(conn, tid)
        workspace = kanban_home.parent / "workspace2"
        workspace.mkdir()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

        exit_code = bridge.main()

        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert exit_code == 1
    assert task.status == "blocked"
    reasons = " ".join(str((e.payload or {}).get("reason", "")) for e in events).lower()
    assert "herdr" in reasons or "connection refused" in reasons


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
