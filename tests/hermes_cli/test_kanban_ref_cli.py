"""CLI surface for `hermes kanban ref add/list/rm/find` (hermes_cli.kanban._dispatch_ref)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_ref_add_list_rm_round_trip(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="pr7 candidate", created_by="test")

    out = kc.run_slash(
        f"ref add {task_id} --type branch --repo C:\\cic\\_SAVE\\globalshop-win-demo-pr7 "
        f"--ref demo/win-capture-20261009 --host DESKTOP-F8K8CPE"
    )
    assert "Added ref" in out
    assert task_id in out

    listed = kc.run_slash(f"ref list {task_id}")
    assert "demo/win-capture-20261009" in listed
    assert "DESKTOP-F8K8CPE" in listed

    ref_id = int(listed.split("[", 1)[1].split("]", 1)[0])
    removed = kc.run_slash(f"ref rm {ref_id}")
    assert "Removed ref" in removed
    assert "No refs" in kc.run_slash(f"ref list {task_id}")


def test_ref_add_requires_type_flag(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="card", created_by="test")

    out = kc.run_slash(f"ref add {task_id} --repo somewhere")
    assert "usage error" in out


def test_ref_find_scans_every_board(kanban_home):
    kb.create_board("gsd")
    kb.create_board("titan")
    with kbc.connect_closing(board="gsd") as conn:
        gsd_task = kb.create_task(conn, title="gsd pr7 card", created_by="test")
        kb.add_task_ref(
            conn, gsd_task, "branch", repo="C:\\cic\\_SAVE\\globalshop-win-demo-pr7",
            ref="demo/win-capture-20261009",
        )
    with kbc.connect_closing(board="titan") as conn:
        titan_task = kb.create_task(conn, title="unrelated titan card", created_by="test")
        kb.add_task_ref(conn, titan_task, "branch", repo="ECS Core", ref="main")

    found = kc.run_slash("ref find --repo C:\\cic\\_SAVE\\globalshop-win-demo-pr7")
    assert gsd_task in found
    assert "board=gsd" in found
    assert titan_task not in found


def test_ref_find_board_scoped_when_board_flag_given(kanban_home):
    kb.create_board("gsd")
    kb.create_board("titan")
    with kbc.connect_closing(board="gsd") as conn:
        gsd_task = kb.create_task(conn, title="gsd card", created_by="test")
        kb.add_task_ref(conn, gsd_task, "branch", repo="same-repo-name", ref="main")
    with kbc.connect_closing(board="titan") as conn:
        titan_task = kb.create_task(conn, title="titan card", created_by="test")
        kb.add_task_ref(conn, titan_task, "branch", repo="same-repo-name", ref="main")

    scoped = kc.run_slash("--board titan ref find --repo same-repo-name")
    assert titan_task in scoped
    assert gsd_task not in scoped


def test_ref_find_requires_repo_or_ref(kanban_home):
    out = kc.run_slash("ref find")
    assert "pass --repo and/or --ref" in out


def test_ref_find_no_match_reports_cleanly(kanban_home):
    assert "no matching refs" in kc.run_slash("ref find --repo nowhere")


def test_ref_add_is_fenced_for_delegated_children(kanban_home, monkeypatch):
    """Mirrors attach/attach-rm: a ref write is denied from a fenced delegated-child
    context, the same CLI-boundary fast-fail every other board mutation gets."""
    import argparse

    monkeypatch.setattr("agent.delegation_context.kanban_path_is_fenced", lambda _path: True)

    args = argparse.Namespace(kanban_action="ref", ref_action="add", board=None)
    assert kc._is_delegated_child_cli_mutation(args) is True

    args_rm = argparse.Namespace(kanban_action="ref", ref_action="rm", board=None)
    assert kc._is_delegated_child_cli_mutation(args_rm) is True

    args_find = argparse.Namespace(kanban_action="ref", ref_action="find", board=None)
    assert kc._is_delegated_child_cli_mutation(args_find) is False

    args_list = argparse.Namespace(kanban_action="ref", ref_action="list", board=None)
    assert kc._is_delegated_child_cli_mutation(args_list) is False
