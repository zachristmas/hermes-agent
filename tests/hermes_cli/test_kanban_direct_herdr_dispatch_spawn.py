"""kanban.direct_herdr_dispatch_boards: titan-only opt-in for the direct-Herdr bridge worker
(card t_cb0795f9).

``_default_spawn`` normally builds a full interactive ``hermes -p <profile> chat -q ...`` worker
argv. A board listed in ``kanban.direct_herdr_dispatch_boards`` instead gets
``hermes_cli.kanban_direct_herdr_worker`` — a small synchronous bridge that drives a Herdr pane +
coding agent itself. Every other board must be completely unaffected, and the opt-in must default
off and fail closed on an unreadable config.
"""
from __future__ import annotations

import subprocess

import pytest


def _make_task(kb, *, assignee: str = "elias", task_id: str = "t_direct_herdr"):
    return kb.Task(
        id=task_id,
        title="direct herdr dispatch",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


@pytest.fixture
def spawn_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a spawnable profile, mocked Popen, and mocked hermes resolution."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db_dispatch as kbd
    from tools.environments import local as env_local

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    # Pre-existing, unrelated environment quirk when tests run from inside a nested
    # `.claude/worktrees/<name>` checkout (true for every _default_spawn test in this worktree,
    # including the pre-existing test_kanban_worker_spawn_toolsets.py): PM's payload-manifest probe
    # walks up from this file's path and finds a real `manifest.json` next to the worktree root,
    # which the test suite's home_io_guard correctly refuses as live-filesystem I/O. Short-circuit
    # just that probe; it is unrelated to the direct-Herdr dispatch logic under test.
    monkeypatch.setattr(env_local, "_resolve_hermes_bin_dir", lambda: None)

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {"root": root, "workspace": workspace, "captured": captured}


def _write_config(root, text: str) -> None:
    root.joinpath("config.yaml").write_text(text, encoding="utf-8")


def test_non_titan_board_always_builds_existing_worker_argv(spawn_env):
    """Even with titan opted in, an unrelated board (connect) must build the unchanged
    worker argv (``-m hermes_cli.kanban_worker_client``) — the opt-in is strictly per-board."""
    _write_config(spawn_env["root"], "kanban:\n  direct_herdr_dispatch_boards:\n    - titan\n")

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    kbd._default_spawn(_make_task(kb), str(spawn_env["workspace"]), board="connect")

    cmd = spawn_env["captured"]["cmd"]
    env = spawn_env["captured"]["env"]
    assert cmd[-2:] == ["-m", "hermes_cli.kanban_worker_client"]
    assert "kanban_direct_herdr_worker" not in " ".join(cmd)
    assert "HERMES_KANBAN_HERDR_MODEL" not in env
    assert "HERMES_KANBAN_HERDR_EFFORT" not in env


def test_titan_without_opt_in_behaves_like_today(spawn_env):
    """No config key at all (default off) — titan must still get the existing worker argv."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    kbd._default_spawn(_make_task(kb), str(spawn_env["workspace"]), board="titan")

    cmd = spawn_env["captured"]["cmd"]
    assert cmd[-2:] == ["-m", "hermes_cli.kanban_worker_client"]
    assert "kanban_direct_herdr_worker" not in " ".join(cmd)


def test_titan_with_opt_in_builds_direct_herdr_bridge_argv_and_env(spawn_env, monkeypatch):
    """Titan opted in: the bridge module argv replaces the chat worker, and the model/effort
    env vars the bridge reads are populated from the task (or sane defaults)."""
    _write_config(spawn_env["root"], "kanban:\n  direct_herdr_dispatch_boards:\n    - titan\n")
    monkeypatch.setattr("sys.executable", "C:\\python\\python.exe")

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    task = _make_task(kb)
    task.model_override = "claude-opus-9"
    task.reasoning_effort = "high"
    kbd._default_spawn(task, str(spawn_env["workspace"]), board="titan")

    cmd = spawn_env["captured"]["cmd"]
    env = spawn_env["captured"]["env"]
    assert cmd == ["C:\\python\\python.exe", "-m", "hermes_cli.kanban_direct_herdr_worker"]
    assert env["HERMES_KANBAN_HERDR_MODEL"] == "claude-opus-9"
    assert env["HERMES_KANBAN_HERDR_EFFORT"] == "high"
    assert env["HERMES_KANBAN_TASK"] == "t_direct_herdr"
    assert env["HERMES_KANBAN_BOARD"] == "titan"


def test_titan_with_opt_in_defaults_model_and_effort_when_task_has_none(spawn_env):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    _write_config(spawn_env["root"], "kanban:\n  direct_herdr_dispatch_boards: titan\n")  # comma-string form

    kbd._default_spawn(_make_task(kb), str(spawn_env["workspace"]), board="titan")

    env = spawn_env["captured"]["env"]
    assert env["HERMES_KANBAN_HERDR_MODEL"] == "claude-sonnet-5-5"
    assert env["HERMES_KANBAN_HERDR_EFFORT"] == "medium"


@pytest.mark.parametrize("config_text", [
    "kanban:\n  direct_herdr_dispatch_boards:\n",   # present but blank/null
    "kanban:\n  direct_herdr_dispatch_boards: []\n",  # present but empty list
])
def test_titan_present_but_empty_boards_key_stays_off(spawn_env, config_text):
    """A present-but-empty key must resolve to 'off', the same safe direction as absent — this
    is the opposite fail direction from kanban.dispatch_profiles (which fails closed = deny)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    _write_config(spawn_env["root"], config_text)

    kbd._default_spawn(_make_task(kb), str(spawn_env["workspace"]), board="titan")

    cmd = spawn_env["captured"]["cmd"]
    assert "kanban_direct_herdr_worker" not in " ".join(cmd)


def test_unreadable_config_fails_closed_to_legacy_worker(spawn_env, monkeypatch):
    """A broken config read must never silently switch titan onto the Herdr bridge."""
    from hermes_cli import config_effective
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    def raise_read_error(**_kwargs):
        raise OSError("config unavailable")

    monkeypatch.setattr(config_effective, "load_user_config_effective", raise_read_error)

    kbd._default_spawn(_make_task(kb), str(spawn_env["workspace"]), board="titan")

    cmd = spawn_env["captured"]["cmd"]
    assert "kanban_direct_herdr_worker" not in " ".join(cmd)


def test_board_uses_direct_herdr_dispatch_helper(tmp_path, monkeypatch):
    """Unit-level check of the gate predicate itself, independent of the full spawn path."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_config(root, "kanban:\n  direct_herdr_dispatch_boards:\n    - titan\n    - Titan\n")

    from hermes_cli import kanban_db_dispatch as kbd

    assert kbd._board_uses_direct_herdr_dispatch("titan") is True
    assert kbd._board_uses_direct_herdr_dispatch("TITAN") is True
    assert kbd._board_uses_direct_herdr_dispatch("connect") is False
    assert kbd._board_uses_direct_herdr_dispatch(None) is False
