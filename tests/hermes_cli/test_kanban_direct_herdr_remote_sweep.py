"""Cross-host existing-session sweep (kanban_direct_herdr_worker.sweep_other_hosts_for_task /
dispatch), added after the GSD incident on card t_a87265e1: a fast-dispatched board's bridge must
never spawn a brand-new agent on top of a session that already exists for the same kanban_task on
a DIFFERENT machine, reached through one of Herdr's own saved remote targets
(``herdr --machine <id> ...``).
"""
from __future__ import annotations

import sys

import pytest

from hermes_cli import kanban_direct_herdr_worker as bridge

_STUB = r'''
import json, sys

a = sys.argv[1:]
if a[:1] == ["--session"]:
    a = a[2:]

if a[:2] == ["machine", "list"]:
    print(json.dumps(MACHINES))
elif a[:1] == ["--machine"]:
    machine_id = a[1]
    rest = a[2:]
    if rest[:2] == ["agent", "list"]:
        print(json.dumps({"result": {"agents": AGENTS_BY_MACHINE.get(machine_id, [])}}))
    elif rest[:2] == ["agent", "wait"]:
        print(json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
    elif rest[:2] == ["agent", "read"]:
        print(json.dumps({"result": {"text": "already did the work here"}}))
    else:
        sys.stderr.write(json.dumps({"error": {"code": "unknown_command", "message": " ".join(rest)}}))
        sys.exit(1)
else:
    sys.stderr.write(json.dumps({"error": {"code": "unknown_command", "message": " ".join(a)}}))
    sys.exit(1)
'''


def _stub(tmp_path, monkeypatch, machines, agents_by_machine):
    script = tmp_path / "stub_herdr.py"
    script.write_text(
        f"MACHINES = {machines!r}\nAGENTS_BY_MACHINE = {agents_by_machine!r}\n" + _STUB,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")


def test_sweep_finds_nothing_when_no_machines_configured(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, [], {})
    matches, unreachable, checked = bridge.sweep_other_hosts_for_task("t_x")
    assert matches == []
    assert unreachable == []
    assert checked == []


def test_sweep_treats_unsupported_machine_list_as_zero_machines_not_unreachable(tmp_path, monkeypatch):
    """An old herdr without `machine list --json`, or a profile with none configured, is the
    ordinary single-machine case -- it must never block every dispatch on this install."""
    script = tmp_path / "stub_no_machine_cmd.py"
    script.write_text(
        "import sys, json\n"
        "sys.stderr.write(json.dumps({'error': {'code': 'unknown_command', 'message': 'machine'}}))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")
    matches, unreachable, checked = bridge.sweep_other_hosts_for_task("t_x")
    assert (matches, unreachable, checked) == ([], [], [])


def test_sweep_finds_a_tagged_match_on_a_remote_machine(tmp_path, monkeypatch):
    _stub(
        tmp_path, monkeypatch,
        machines=[{"id": "mac1", "label": "Personal MacBook", "enabled": True}],
        agents_by_machine={
            "mac1": [
                {"name": "gsd-t_a87265e1", "agent_status": "done", "pane_id": "w1:p2",
                 "tokens": {"kanban_board": "gsd", "kanban_task": "t_a87265e1"}},
                {"name": "other-t_zzz", "agent_status": "idle", "pane_id": "w1:p3",
                 "tokens": {"kanban_board": "gsd", "kanban_task": "t_zzz"}},
            ],
        },
    )
    matches, unreachable, checked = bridge.sweep_other_hosts_for_task("t_a87265e1")
    assert unreachable == []
    assert checked == ["Personal MacBook"]
    assert len(matches) == 1
    assert matches[0]["name"] == "gsd-t_a87265e1"
    assert matches[0]["_machine_label"] == "Personal MacBook"


def test_sweep_reports_a_machine_as_unreachable_not_as_no_match(tmp_path, monkeypatch):
    script = tmp_path / "stub_partial_outage.py"
    script.write_text(
        "MACHINES = [{'id': 'mac1', 'label': 'Personal MacBook', 'enabled': True}]\n"
        "AGENTS_BY_MACHINE = {}\n" + _STUB.replace(
            'print(json.dumps({"result": {"agents": AGENTS_BY_MACHINE.get(machine_id, [])}}))',
            'sys.stderr.write("connection refused"); sys.exit(1)',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")
    matches, unreachable, checked = bridge.sweep_other_hosts_for_task("t_a87265e1")
    assert matches == []
    assert checked == []
    assert unreachable == ["Personal MacBook"]


def test_dispatch_blocks_with_needs_input_when_a_machine_is_unreachable(tmp_path, monkeypatch):
    script = tmp_path / "stub_partial_outage2.py"
    script.write_text(
        "MACHINES = [{'id': 'mac1', 'label': 'Personal MacBook', 'enabled': True}]\n"
        "AGENTS_BY_MACHINE = {}\n" + _STUB.replace(
            'print(json.dumps({"result": {"agents": AGENTS_BY_MACHINE.get(machine_id, [])}}))',
            'sys.stderr.write("connection refused"); sys.exit(1)',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERDR_BIN", f"{sys.executable} {script}")
    success, report, block_kind = bridge.dispatch("t_a87265e1", "gsd", "/tmp/ws", "m", "medium")
    assert success is False
    assert block_kind == "needs_input"
    assert "Personal MacBook" in report


def test_dispatch_continues_an_existing_remote_match_instead_of_spawning_fresh(tmp_path, monkeypatch):
    _stub(
        tmp_path, monkeypatch,
        machines=[{"id": "mac1", "label": "Personal MacBook", "enabled": True}],
        agents_by_machine={
            "mac1": [
                {"name": "gsd-t_a87265e1", "agent_status": "done", "pane_id": "w1:p2",
                 "tokens": {"kanban_board": "gsd", "kanban_task": "t_a87265e1"}},
            ],
        },
    )
    success, report, block_kind = bridge.dispatch("t_a87265e1", "gsd", "/tmp/ws", "m", "medium")
    assert success is True
    assert block_kind == "transient"
    assert "Personal MacBook" in report
    assert "gsd-t_a87265e1" in report
    assert "already did the work here" in report


@pytest.mark.parametrize("status", ["blocked"])
def test_dispatch_surfaces_a_blocked_remote_match_as_not_settled(tmp_path, monkeypatch, status):
    _stub(
        tmp_path, monkeypatch,
        machines=[{"id": "mac1", "label": "Personal MacBook", "enabled": True}],
        agents_by_machine={
            "mac1": [
                {"name": "gsd-t_a87265e1", "agent_status": status, "pane_id": "w1:p2",
                 "tokens": {"kanban_board": "gsd", "kanban_task": "t_a87265e1"}},
            ],
        },
    )
    success, report, _block_kind = bridge.dispatch("t_a87265e1", "gsd", "/tmp/ws", "m", "medium")
    assert success is False
    assert "blocked on an approval" in report
