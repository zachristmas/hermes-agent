"""Board-owned executor linkage: verify the live pane/agent and record it on the card before the
agent gets any work (connect t_b956ba38: a live, tagged agent the card never named).
Temporary HERMES_HOME only; Herdr is replaced by the two read callables the bridge passes in."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_herdr as kbdh
from hermes_cli import kanban_direct_herdr_link as link

TAGS = {"kanban_task": "t_1", "kanban_board": "connect", "kanban_run": "run-7"}


def _agent(**over):
    info = {"pane_id": "w1T:p2", "workspace_id": "w1T", "tab_id": "w1T:t2", "terminal_id": "term_1",
            "tokens": dict(TAGS), "agent_session": {"value": "31c133ec"}}
    info.update(over)
    return info


def _pane(**over):
    pane = {"pane_id": "w1T:p2", "workspace_id": "w1T", "tab_id": "w1T:t2", "tokens": dict(TAGS)}
    pane.update(over)
    return pane


def _resolve(agent, panes, wait_s=0.0):
    return link.resolve_executor("connect-t_1", "w1T:p2", "t_1", "connect", "run-7",
                                 agent_info=lambda name: agent, list_panes=lambda: panes, wait_s=wait_s)


def test_a_verified_executor_carries_the_full_identity():
    ex = _resolve(_agent(), [_pane()])
    assert (ex.workspace_id, ex.tab_id, ex.pane_id, ex.native_session, ex.fence) == (
        "w1T", "w1T:t2", "w1T:p2", "31c133ec", "run-7")
    assert ex.ref.endswith("/default/w1T/w1T:t2/w1T:p2")


@pytest.mark.parametrize("agent, panes, why", [
    (_agent(tokens={**TAGS, "kanban_run": "run-6"}), [_pane()], "live tags"),
    (_agent(pane_id="w1T:p9"), [_pane()], "in pane"),
    (_agent(), [], "not listed"),
    (_agent(), [_pane(tokens={"kanban_task": "t_1"})], "listed with tags"),
    (_agent(agent_session=None), [_pane()], "native session"),
])
def test_an_unverifiable_executor_is_refused(agent, panes, why):
    with pytest.raises(link.LinkError, match=why):
        _resolve(agent, panes)


def test_a_missing_agent_is_refused():
    with pytest.raises(link.LinkError, match="not present"):
        _resolve(None, [_pane()])


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as c:
        yield c


def test_persisting_is_fenced_by_the_run_and_idempotent_per_pane(conn):
    tid = kb.create_task(conn, title="t", assignee="w")
    run_id = kb.claim_task(conn, tid).current_run_id
    ex = _resolve(_agent(), [_pane()])
    with pytest.raises(link.LinkError, match="no longer owns"):
        link.persist_executor(tid, None, ex, run_id=run_id + 1)
    link.persist_executor(tid, None, ex, run_id=run_id)
    link.persist_executor(tid, None, ex, run_id=run_id)  # an adopted/resumed run re-links the same pane
    refs = conn.execute("SELECT ref, note FROM task_refs WHERE task_id = ? AND ref_type = 'herdr'", (tid,)).fetchall()
    assert [r["ref"] for r in refs] == [ex.ref] and "31c133ec" in refs[0]["note"]
    linked = [e for e in kb.list_events(conn, tid) if e.kind == "executor_linked"]
    assert len(linked) == 2 and linked[0].payload["native_session"] == "31c133ec"


def test_a_missing_bridge_module_defers_as_infrastructure_not_a_card_failure(monkeypatch, tmp_path):
    from tools.process_registry import RestartSafeScopeUnavailable

    monkeypatch.setattr(kbdh, "__file__", str(tmp_path / "kanban_db_dispatch_herdr.py"))
    with pytest.raises(RestartSafeScopeUnavailable):
        kbdh._direct_herdr_worker_argv()
