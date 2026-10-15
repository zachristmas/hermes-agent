"""Generic multi-valued task refs (kanban_db_refs.py): a task can point at any number of typed
references (branch, ADO work item, Connect ticket, PR URL, ...), the generalization that replaced
a rejected scalar ``set-branch`` design once a GSD card turned out to span multiple repos."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


def test_add_task_ref_round_trips(conn):
    task_id = kb.create_task(conn, title="pr7 candidate", created_by="test")
    ref_id = kb.add_task_ref(
        conn, task_id, "branch", repo="C:\\cic\\_SAVE\\globalshop-win-demo-pr7",
        ref="demo/win-capture-20261009", host="DESKTOP-F8K8CPE",
    )
    assert ref_id > 0

    refs = kb.list_task_refs(conn, task_id)
    assert len(refs) == 1
    assert refs[0]["ref_type"] == "branch"
    assert refs[0]["repo"] == "C:\\cic\\_SAVE\\globalshop-win-demo-pr7"
    assert refs[0]["ref"] == "demo/win-capture-20261009"
    assert refs[0]["host"] == "DESKTOP-F8K8CPE"
    assert refs[0]["note"] is None


def test_task_can_hold_refs_across_multiple_repos(conn):
    """The whole reason a scalar set-branch was rejected: one task, several repos."""
    task_id = kb.create_task(conn, title="gsd multi-repo card", created_by="test")
    kb.add_task_ref(conn, task_id, "branch", repo="backoffice", ref="feature/d30")
    kb.add_task_ref(conn, task_id, "branch", repo="order-service", ref="feature/d30-order")
    kb.add_task_ref(conn, task_id, "connect-ticket", note="ITSD-1234")

    refs = kb.list_task_refs(conn, task_id)
    assert len(refs) == 3
    assert {r["ref_type"] for r in refs} == {"branch", "connect-ticket"}
    assert any(r["note"] == "ITSD-1234" and r["repo"] is None for r in refs)


def test_add_task_ref_requires_a_type(conn):
    task_id = kb.create_task(conn, title="card", created_by="test")
    with pytest.raises(ValueError):
        kb.add_task_ref(conn, task_id, "   ")


def test_add_task_ref_unknown_task_raises(conn):
    with pytest.raises(ValueError):
        kb.add_task_ref(conn, "t_does_not_exist", "branch", ref="main")


def test_remove_task_ref(conn):
    task_id = kb.create_task(conn, title="card", created_by="test")
    ref_id = kb.add_task_ref(conn, task_id, "pr-url", note="https://example/pr/1")

    removed = kb.remove_task_ref(conn, ref_id)
    assert removed["ref_type"] == "pr-url"
    assert removed["task_id"] == task_id
    assert kb.list_task_refs(conn, task_id) == []


def test_remove_task_ref_unknown_id_returns_none(conn):
    assert kb.remove_task_ref(conn, 999999) is None


def test_find_task_refs_by_repo_and_ref(conn):
    task_id = kb.create_task(conn, title="pr7 candidate", created_by="test")
    kb.add_task_ref(
        conn, task_id, "branch", repo="C:\\cic\\_SAVE\\globalshop-win-demo-pr7",
        ref="demo/win-capture-20261009",
    )
    other_id = kb.create_task(conn, title="unrelated card", created_by="test")
    kb.add_task_ref(conn, other_id, "branch", repo="C:\\cic\\_SAVE\\other-checkout", ref="main")

    found = kb.find_task_refs(conn, repo="C:\\cic\\_SAVE\\globalshop-win-demo-pr7")
    assert len(found) == 1
    assert found[0]["task_id"] == task_id
    assert found[0]["task_title"] == "pr7 candidate"

    found_by_ref = kb.find_task_refs(conn, ref="demo/win-capture-20261009")
    assert [r["task_id"] for r in found_by_ref] == [task_id]


def test_find_task_refs_requires_repo_or_ref(conn):
    with pytest.raises(ValueError):
        kb.find_task_refs(conn)


def test_find_task_refs_no_match_returns_empty(conn):
    task_id = kb.create_task(conn, title="card", created_by="test")
    kb.add_task_ref(conn, task_id, "branch", repo="somewhere", ref="main")

    assert kb.find_task_refs(conn, repo="nowhere") == []
