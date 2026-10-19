"""A person's comment on a card is answered by the card's CURRENT assignee, on that card.

Regression for cs t_6465d3ec (Zach's dashboard comments asked for a PR; retries re-ran the original
brief and never answered) and connect t_b956ba38 (a review card reopened to a stale handler).
Temporary HERMES_HOME only; no Herdr, no real worker processes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_comment_replies as replies
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_herdr as kbdh


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _in_review(conn, assignee="implementer"):
    tid = kb.create_task(conn, title="analytics", assignee=assignee)
    run_id = kb.claim_task(conn, tid).current_run_id
    assert kb.request_review(conn, tid, summary="done", expected_run_id=run_id)
    return tid


def _spawns(conn, *, eligible=lambda task: True, now=None):
    calls = []

    def spawn(task, workspace, board, env):
        calls.append((task.id, task.assignee, env))

    started = replies.reconcile_comment_replies(conn, board=None, spawn_fn=spawn, eligible=eligible, now=now)
    return started, calls


def _status(conn, tid):
    return kb.get_task(conn, tid).status


def test_a_dashboard_comment_on_a_review_card_gets_one_fenced_reply_from_the_current_assignee(conn):
    tid = _in_review(conn)
    kb.assign_task(conn, tid, "closer")  # the routing decision made after the handoff
    cid = kb.add_comment(conn, tid, "zach", "Why is the CSV export missing?", source="dashboard")

    started, calls = _spawns(conn)
    assert started == [tid]
    (_, assignee, env), = calls
    assert assignee == "closer"
    assert env["HERMES_KANBAN_REPLY_THROUGH"] == str(cid)
    assert replies.reply_in_flight(conn, tid)
    assert kbd.check_respawn_guard(conn, tid, lane="review") == "comment_reply_in_flight"
    assert _spawns(conn)[1] == []  # one reply run per lease, never a duplicate

    lock = env["HERMES_KANBAN_REPLY_LOCK"]
    assert replies.record_reply(conn, tid, through_id=cid, author="closer", body="It ships in PR #12.",
                                lock="not-the-lease") is None
    reply_id = replies.record_reply(conn, tid, through_id=cid, author="closer", body="It ships in PR #12.",
                                    lock=lock)
    assert reply_id is not None
    assert [c.author for c in kb.list_comments(conn, tid)] == ["zach", "closer"]
    assert _status(conn, tid) == "review"  # a conversational answer leaves Review alone
    assert not replies.pending_human_comments(conn, tid) and not replies.reply_in_flight(conn, tid)
    assert _spawns(conn)[1] == []


@pytest.mark.parametrize("source", ["cli", "agent", replies.REPLY_SOURCE, replies.SYSTEM_SOURCE, None])
def test_agent_cli_system_and_reply_comments_never_start_a_reply(conn, source):
    tid = _in_review(conn)
    kb.add_comment(conn, tid, "zach", "looks like a human wrote this", source=source)
    assert replies.tasks_with_pending_replies(conn) == []
    assert _spawns(conn)[1] == []


def test_a_running_card_queues_the_comment_for_its_own_run_and_a_stale_run_cannot_answer(conn):
    tid = kb.create_task(conn, title="busy", assignee="worker")
    run_id = kb.claim_task(conn, tid).current_run_id
    cid = kb.add_comment(conn, tid, "zach", "also export CSV", source="dashboard")
    assert _spawns(conn)[1] == []  # no second executor on a busy card
    assert replies.record_reply(conn, tid, through_id=cid, author="worker", body="on it", run_id=run_id + 1) is None
    assert replies.record_reply(conn, tid, through_id=cid, author="worker", body="on it", run_id=run_id)
    assert not replies.pending_human_comments(conn, tid)


def test_a_failing_reply_is_retried_once_then_leaves_one_truthful_note(conn):
    tid = _in_review(conn)
    kb.add_comment(conn, tid, "zach", "status?", source="dashboard")
    for expected in ("retry", "gave_up"):
        (_, _, env), = _spawns(conn)[1]
        assert replies.fail_reply(conn, tid, lock=env["HERMES_KANBAN_REPLY_LOCK"], reason="pane closed") == expected
    notes = [c for c in kb.list_comments(conn, tid) if c.author == "kanban"]
    assert len(notes) == 1 and "2 attempts" in notes[0].body and "pane closed" in notes[0].body
    assert _spawns(conn)[1] == [] and _status(conn, tid) == "review"


def test_an_expired_reply_lease_is_closed_and_retried_by_the_next_sweep(conn):
    tid = _in_review(conn)
    kb.add_comment(conn, tid, "zach", "status?", source="dashboard")
    (_, _, first), = _spawns(conn)[1]
    later = 10**10  # far past the lease expiry
    (_, _, second), = _spawns(conn, now=later)[1]
    assert second["HERMES_KANBAN_REPLY_LOCK"] != first["HERMES_KANBAN_REPLY_LOCK"]
    failed = [e for e in kb.list_events(conn, tid) if e.kind == "comment_reply_failed"]
    assert len(failed) == 1


def test_a_board_that_cannot_run_the_reply_spawns_nothing(conn):
    tid = _in_review(conn)
    kb.add_comment(conn, tid, "zach", "status?", source="dashboard")
    assert _spawns(conn, eligible=lambda task: False)[1] == []
    assert replies.pending_human_comments(conn, tid)  # still queued, not dropped


def test_an_explicit_request_on_review_reopens_to_the_current_assignee_not_the_stale_implementer(conn):
    tid = _in_review(conn, assignee="implementer")
    kb.assign_task(conn, tid, "closer")
    assert replies.route_requested_action(conn, tid, author="closer", action="open the PR") == "reopened"
    task = kb.get_task(conn, tid)
    assert task.status in ("ready", "todo") and task.assignee == "closer"


def test_an_explicit_request_on_a_done_card_is_noted_never_reopened(conn):
    tid = kb.create_task(conn, title="shipped", assignee="worker")
    kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, result="shipped")
    assert replies.route_requested_action(conn, tid, author="worker", action="redo it") == "needs_reopen"
    assert _status(conn, tid) == "done"


def test_review_reopen_without_a_later_reassignment_still_restores_the_implementer(conn):
    tid = _in_review(conn, assignee="implementer")
    assert kb.reopen_review_task(conn, tid)
    assert kb.get_task(conn, tid).assignee == "implementer"


def test_worker_context_leads_with_the_newest_instructions_and_the_requested_pr(conn):
    tid = kb.create_task(conn, title="analytics", body="Implement GA4 events and run the tests.", assignee="w")
    kb.add_comment(conn, tid, "zach", "The deliverable should be a pull request.", source="dashboard")
    kb.add_comment(conn, tid, "zach", "Do not run tests.", source="dashboard")
    ctx = kb.build_worker_context(conn, tid)
    latest = ctx.index("## Latest instructions")
    assert ctx.index("Do not run tests.") < ctx.index("pull request.", latest) < ctx.index("## Comment thread")
    assert "A pull request (requested in comment" in ctx
    assert "Awaiting your reply on the card" in ctx


@pytest.mark.parametrize("body, wanted", [
    ("The deliverable should be a pull request.", True),
    ("Please open a PR when done.", True),
    ("Do not open a PR, just push the branch.", False),
    ("Can you confirm the PR builds?", False),
])
def test_requested_pull_request_detection(conn, body, wanted):
    tid = kb.create_task(conn, title="t", assignee="w")
    kb.add_comment(conn, tid, "zach", body, source="dashboard")
    assert bool(replies.requested_deliverables(kb.list_comments(conn, tid))) is wanted


def test_the_dispatcher_tick_starts_the_reply_through_the_normal_spawn_path(conn, monkeypatch):
    tid = _in_review(conn)
    task = kb.get_task(conn, tid)
    Path(task.workspace_path or kb.workspaces_root() / tid).mkdir(parents=True, exist_ok=True)
    kb.add_comment(conn, tid, "zach", "status?", source="dashboard")
    spawned = []
    monkeypatch.setattr(kbdh, "_board_uses_direct_herdr_dispatch", lambda board, task: True)
    monkeypatch.setattr(kbd, "_default_spawn",
                        lambda task, workspace, board=None, extra_env=None: spawned.append((task.id, extra_env)))
    kbd.dispatch_once(conn)
    assert [t for t, _ in spawned] == [tid]
    assert set(spawned[0][1]) == {"HERMES_KANBAN_REPLY_LOCK", "HERMES_KANBAN_REPLY_THROUGH"}
