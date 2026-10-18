"""/open-pr handoff contract for the direct Herdr bridge, and review-summary correction.

Regression for the Titan review loop (cards t_adc72060 / t_e119b0f7, 2026-10-16): the bridge saved
the raw terminal capture (Claude banner, echoed prompt, usage-limit error) as the Review summary.
The contract is opt-in per board via ``kanban.direct_herdr_open_pr_handoff_boards``.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_review_summary import edit_review_summary
from hermes_cli import kanban_direct_herdr_handoff as handoff
from hermes_cli import kanban_direct_herdr_worker as bridge

COMMAND = (
    "/open-pr ECS Core, ADO Task 11278 (also link 11279, 11281), worktree C:/cic/_wt/t_x/ECS Core, "
    "branch feature/ado-10644-product-designer-schema, target QAS, kanban t_x (board titan)"
)
BANNER = (
    "▐▛███▛█   Claude Code v2.1.301\n▝▜██████▀  Sonnet 5.5 with medium effort · Claude Team\n"
    " ▝▝   ▝▝   C:\\cic · /rc\n\n"
)
USAGE_LIMIT = (
    "  ⎿  You've hit your weekly limit · resets 11am (America/New_York)\n"
    "     Use your limit reset to reset it now: clau.de/reset\n"
    "     /usage-credits to finish what you’re working on.\n"
)


def _block(run_id: int, task_id: str, body: str) -> str:
    return f"● HANDOFF-BEGIN run={run_id} task={task_id}\n{body}\nHANDOFF-END\n"


GOOD_BODY = f"{COMMAND}\nSchema script and sqlproj reviewed.\nLocalDB result predates the index fix."


# ---- pure contract -------------------------------------------------------------------------


def test_valid_current_run_block_becomes_a_command_first_summary():
    transcript = BANNER + "❯ work kanban task t_x\n" + _block(7, "t_x", GOOD_BODY)
    got = handoff.resolve_handoff(transcript, "work kanban task t_x", "t_x", 7, None)
    assert got.ok and got.source == "transcript"
    assert got.summary.splitlines()[0] == COMMAND
    assert "Claude Code" not in got.summary and "limit" not in got.summary.lower().split("predates")[0]


def test_banner_plus_usage_limit_is_a_failure_not_a_review():
    got = handoff.resolve_handoff(BANNER + USAGE_LIMIT, "work kanban task t_x", "t_x", 7, None)
    assert not got.ok
    assert "usage limit" in got.reason
    assert "Transcript tail" in got.reason and "weekly limit" in got.reason  # evidence kept


def test_blank_or_banner_only_turn_is_a_failure():
    assert not handoff.resolve_handoff("", "p", "t_x", 7, None).ok
    got = handoff.resolve_handoff(BANNER, "p", "t_x", 7, None)
    assert not got.ok and "without a handoff" in got.reason


def test_echoed_instruction_placeholder_is_not_a_handoff():
    prompt = "work kanban task t_x\n" + handoff.contract_instructions("t_x", 7)
    # The agent's pane echoes the prompt back, wrapped and indented.
    echoed = "\n".join("  " + line for line in prompt.splitlines())
    got = handoff.resolve_handoff(BANNER + "❯ " + echoed, prompt, "t_x", 7, None)
    assert not got.ok


def test_placeholder_command_in_a_real_block_is_rejected():
    body = "/open-pr <the real, copy-ready command for this delivery>\nstuff"
    assert not handoff.resolve_handoff(_block(7, "t_x", body), "p", "t_x", 7, None).ok


def test_block_tied_to_another_run_or_task_is_rejected():
    assert not handoff.resolve_handoff(_block(6, "t_x", GOOD_BODY), "p", "t_x", 7, None).ok
    assert not handoff.resolve_handoff(_block(7, "t_y", GOOD_BODY), "p", "t_x", 7, None).ok


def test_unterminated_block_is_rejected():
    text = f"HANDOFF-BEGIN run=7 task=t_x\n{GOOD_BODY}\n"
    assert not handoff.resolve_handoff(text, "p", "t_x", 7, None).ok


def test_command_must_lead_the_block():
    body = "Done, here is the command:\n" + COMMAND
    assert not handoff.resolve_handoff(_block(7, "t_x", body), "p", "t_x", 7, None).ok


def test_last_card_comment_with_a_real_command_is_reused_even_under_a_usage_limit_banner():
    comment = f"```text\n{COMMAND}\n```\n\nRun from a harness rooted at C:/cic."
    got = handoff.resolve_handoff(BANNER + USAGE_LIMIT, "p", "t_x", 7, comment)
    assert got.ok and got.source == "card_comment"
    assert got.summary.splitlines()[0] == COMMAND
    assert "```" not in got.summary


def test_last_comment_without_a_real_command_is_not_a_handoff():
    for body in ("Looks good, thanks", "/open-pr", "/open-pr <ticket>", "see /open-pr docs for the command"):
        assert not handoff.resolve_handoff(BANNER, "p", "t_x", 7, body).ok


def test_prompt_carries_the_contract_only_on_an_opted_in_board(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
    assert "HANDOFF-BEGIN" not in bridge._compose_prompt(tid, kb.DEFAULT_BOARD, 3)
    _enable(kanban_home, kb.DEFAULT_BOARD)
    prompt = bridge._compose_prompt(tid, kb.DEFAULT_BOARD, 3)
    assert f"HANDOFF-BEGIN run=3 task={tid}" in prompt
    assert "HANDOFF-BEGIN" not in bridge._compose_prompt(tid, kb.DEFAULT_BOARD)  # no run id, no contract


# ---- worker integration (real kanban DB, scripted agent transcript) ---------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _enable(home: Path, *boards: str) -> None:
    items = "".join(f"\n    - {b}" for b in boards)
    (home / "config.yaml").write_text(
        f"kanban:\n  direct_herdr_open_pr_handoff_boards:{items}\n", encoding="utf-8",
    )


def _run_main(kanban_home, monkeypatch, transcript_for, *, comment=None, enable=True):
    """Claim a card, script the agent's settled transcript, run ``bridge.main``."""
    if enable:
        _enable(kanban_home, kb.DEFAULT_BOARD)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="schema card", assignee="default")
        claimed = kb.claim_task(conn, tid)
        if comment:
            kb.add_comment(conn, tid, "axiom", comment)
    workspace = kanban_home.parent / "ws"
    workspace.mkdir(exist_ok=True)
    run_id = claimed.current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    transcript = transcript_for(tid, run_id)
    monkeypatch.setattr(bridge, "dispatch", lambda *a, **k: (True, transcript, "transient"))
    code = bridge.main()
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
        atts = kb.list_attachments(conn, tid)
        events = kb.list_events(conn, tid)
    return code, task, runs, atts, events


def test_usage_limited_banner_blocks_the_card_instead_of_reviewing(kanban_home, monkeypatch):
    code, task, runs, atts, events = _run_main(
        kanban_home, monkeypatch, lambda tid, rid: BANNER + USAGE_LIMIT,
    )
    assert code == 1
    assert task.status == "blocked" and task.block_kind == "needs_input"
    assert not [e for e in events if e.kind == "review_requested"]
    assert "usage limit" in (runs[-1].summary or "")
    assert any(a.filename == f"herdr-transcript-run{runs[-1].id}.txt" for a in atts)  # raw audit kept


def test_valid_handoff_reaches_review_with_a_clean_summary_and_raw_audit(kanban_home, monkeypatch):
    code, task, runs, atts, _ = _run_main(
        kanban_home, monkeypatch,
        lambda tid, rid: BANNER + "❯ prompt\n" + _block(rid, tid, GOOD_BODY),
    )
    assert code == 0 and task.status == "review"
    summary = runs[-1].summary
    assert summary.splitlines()[0] == COMMAND
    assert "Claude Code" not in summary
    assert len(atts) == 1 and "Claude Code" in Path(atts[0].stored_path).read_text(encoding="utf-8")


def test_existing_last_comment_handoff_is_reused_despite_a_banner(kanban_home, monkeypatch):
    code, task, runs, _, _ = _run_main(
        kanban_home, monkeypatch, lambda tid, rid: BANNER + USAGE_LIMIT,
        comment=f"```text\n{COMMAND}\n```\n\nSame combined schema PR.",
    )
    assert code == 0 and task.status == "review"
    assert runs[-1].summary.splitlines()[0] == COMMAND


def test_a_board_that_did_not_opt_in_keeps_the_plain_bridge(kanban_home, monkeypatch):
    _enable(kanban_home, "titan")  # default board is not listed
    code, task, runs, atts, _ = _run_main(
        kanban_home, monkeypatch, lambda tid, rid: "Card done: fixed the thing.", enable=False,
    )
    assert code == 0 and task.status == "review"
    assert runs[-1].summary.startswith("Card done: fixed the thing.")
    assert not atts


def test_stale_run_id_still_cannot_move_a_gated_card(kanban_home, monkeypatch):
    _enable(kanban_home, kb.DEFAULT_BOARD)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owned elsewhere", assignee="default")
        kb.claim_task(conn, tid)
    workspace = kanban_home.parent / "ws2"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999999")
    monkeypatch.setattr(bridge, "dispatch", lambda *a, **k: (True, _block(999999, tid, GOOD_BODY), "transient"))
    assert bridge.main() == 1
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"


# ---- review-summary correction ---------------------------------------------------------------


def _review_card(conn, raw="▐▛███▛█   Claude Code v2.1.301"):
    tid = kb.create_task(conn, title="in review", assignee="default")
    claimed = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary=raw, expected_run_id=claimed.current_run_id)
    return tid


def _edit(tid, **kw):
    ns = dict(task_id=tid, title=None, body=None, priority=None, result=None, summary=None, metadata=None)
    ns.update(kw)
    return kanban_cli._cmd_edit(argparse.Namespace(**ns))


def test_review_summary_edit_preserves_status_assignee_runs_and_old_text(kanban_home):
    with kbc.connect() as conn:
        tid = _review_card(conn)
        before = kb.get_task(conn, tid)
        runs_before = [r.id for r in kb.list_runs(conn, tid)]
        events_before = [e.id for e in kb.list_events(conn, tid)]

        ok, why = edit_review_summary(conn, tid, summary=COMMAND + "\n\nclean prose")
        after = kb.get_task(conn, tid)
        events_after = kb.list_events(conn, tid)

    assert (ok, why) == (True, None)
    assert (after.status, after.assignee, after.claim_lock, after.current_run_id) == (
        before.status, before.assignee, None, None)
    assert [r.id for r in kb.list_runs(kbc.connect(), tid)] == runs_before  # no new run
    assert [e.id for e in events_after][: len(events_before)] == events_before  # history intact
    new = events_after[-1]
    assert new.kind == "review_summary_edited"
    assert new.payload["previous_summary"].startswith("▐▛███▛█")
    with kbc.connect() as conn:
        assert kb.latest_run(conn, tid).summary.startswith(COMMAND)


def test_review_summary_edit_is_refused_for_a_live_claim_and_for_non_review_cards(kanban_home):
    with kbc.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="default")
        kb.claim_task(conn, running)
        ready = kb.create_task(conn, title="ready", assignee="default")
        done = kb.create_task(conn, title="done", assignee="default")
        claimed = kb.claim_task(conn, done)
        kb.complete_task(conn, done, result="x", expected_run_id=claimed.current_run_id)
        reviewing = _review_card(conn)
        # a reviewer has claimed the review card: it is no longer idle
        conn.execute("UPDATE tasks SET claim_lock = 'reviewer:1' WHERE id = ?", (reviewing,))
        conn.commit()

        for tid in (running, ready, done, reviewing):
            ok, why = edit_review_summary(conn, tid, summary=COMMAND)
            assert not ok and why, tid
        assert kb.get_task(conn, running).status == "running"
        assert kb.get_task(conn, ready).status == "ready"
        assert kb.get_task(conn, done).status == "done"
        assert kb.get_task(conn, reviewing).status == "review"
        assert not edit_review_summary(conn, "t_missing", summary=COMMAND)[0]
        assert not edit_review_summary(conn, ready, summary="  ")[0]


def test_cli_edit_summary_alone_corrects_a_review_card_and_rejects_other_cards(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = _review_card(conn)
        other = kb.create_task(conn, title="ready", assignee="default")
    assert _edit(tid, summary=COMMAND) == 0
    assert _edit(other, summary=COMMAND) == 1
    assert "not review" in capsys.readouterr().err
    assert _edit(tid, metadata="{}") == 2  # metadata still needs --result
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "review"
        assert kb.latest_run(conn, tid).summary == COMMAND
