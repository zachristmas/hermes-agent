"""The human-acceptance policy and verifier come from ONE shared source (the kanban root), never from the
caller's profile config. Temp roots only; two profiles (default and CIC) share one root and one board DB."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_settlement as ks
from hermes_cli import kanban_swarm


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Shared root + two profile homes. Starts as the default profile."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "cic").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    kb.init_db()
    kb.create_board("gsd")
    return root


def _as(monkeypatch, root, profile):
    monkeypatch.setenv("HERMES_HOME", str(root if profile == "default" else root / "profiles" / profile))


def _protect_as_default(root, monkeypatch):
    _as(monkeypatch, root, "default")
    return ks.make_verifier("correct horse"), ks.write_policy(["gsd"], ks.make_verifier("correct horse"))


def _empty_cic_profile_policy(root):
    (root / "profiles" / "cic" / "config.yaml").write_text("kanban:\n  human_acceptance:\n    boards: []\n    verifier: ''\n")


def _review_card():
    with kbc.connect(board="gsd") as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        assert kb.request_review(conn, tid, summary="ready")
    return tid


def _status(tid):
    with kbc.connect(board="gsd") as conn:
        return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def _refusals(tid):
    with kbc.connect(board="gsd") as conn:
        return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completion_refused_human_acceptance'",
                            (tid,)).fetchone()[0]


def test_every_profile_resolves_the_same_policy_file(root, monkeypatch):
    _protect_as_default(root, monkeypatch)
    paths, policies = set(), []
    for profile in ("default", "cic"):
        _as(monkeypatch, root, profile)
        paths.add(ks.policy_path())
        policies.append(ks.read_policy())
    assert len(paths) == 1 and paths.pop() == root / "kanban" / "human_acceptance.json"
    assert policies[0] == policies[1] and policies[0]["boards"] == ["gsd"]


def test_cic_context_cannot_complete_a_card_protected_by_the_default_profile(root, monkeypatch):
    _protect_as_default(root, monkeypatch)
    _empty_cic_profile_policy(root)  # an empty profile-level policy must not weaken the shared one
    tid = _review_card()
    _as(monkeypatch, root, "cic")
    with kbc.connect(board="gsd") as conn:
        assert kb.complete_task(conn, tid, summary="cic says done") is False
        assert kb.complete_task(conn, tid, summary="forced", force=True) is False
    assert _status(tid) == "review" and _refusals(tid) == 2


def test_tool_level_complete_from_cic_is_refused_and_valid_human_acceptance_still_works(root, monkeypatch):
    _protect_as_default(root, monkeypatch)
    _empty_cic_profile_policy(root)
    tid = _review_card()
    _as(monkeypatch, root, "cic")
    from tools import kanban_tools
    out = json.loads(kanban_tools._handle_complete({"task_id": tid, "board": "gsd", "summary": "orchestrator"}))
    assert out.get("ok") is not True and _status(tid) == "review" and _refusals(tid) == 1
    # verifier and policy are from the same canonical source, so the passphrase set once works everywhere
    token = ks.authenticate_passphrase("correct horse", ks._acceptance_config()["verifier"], principal="zach")
    assert token is not None
    with kbc.connect(board="gsd") as conn:
        assert kb.complete_task(conn, tid, summary="approved", human_acceptance=token)
    assert _status(tid) == "done"


@pytest.mark.parametrize("content", ["{not json", "[]", '{"boards": "gsd"}', '{"boards": [1], "verifier": ""}',
                                     '{"boards": ["gsd"], "verifier": 5}'])
def test_malformed_shared_policy_fails_closed_for_every_writer(root, monkeypatch, content):
    _as(monkeypatch, root, "default")
    tid = _review_card()
    ks.policy_path().write_text(content)
    with pytest.raises(ks.PolicyError):
        ks.read_policy()
    for profile in ("default", "cic"):
        _as(monkeypatch, root, profile)
        with kbc.connect(board="gsd") as conn:
            assert kb.complete_task(conn, tid, summary="x", force=True) is False
        assert ks.dashboard_acceptance(passphrase="correct horse") is None
    assert _status(tid) == "review"
    with kbc.connect(board="gsd") as conn, pytest.raises(kanban_swarm.SwarmRefusedError):
        kanban_swarm.create_swarm(conn, goal="g", workers=[kanban_swarm.SwarmWorkerSpec(profile="w", title="t", body="b")],
                                  verifier_assignee="v", synthesizer_assignee="s")


def test_unreadable_shared_policy_fails_closed(root, monkeypatch):
    _as(monkeypatch, root, "default")
    tid = _review_card()
    ks.policy_path().mkdir()  # exists but cannot be read as a file
    with kbc.connect(board="gsd") as conn:
        assert kb.complete_task(conn, tid, summary="x", force=True) is False
    assert _status(tid) == "review"


def test_missing_policy_leaves_unprotected_boards_unchanged(root, monkeypatch):
    _as(monkeypatch, root, "cic")
    assert not ks.policy_path().exists() and ks.read_policy() == {}
    with kbc.connect(board="gsd") as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        assert kb.complete_task(conn, tid, summary="fine")


def test_protected_swarm_refusal_uses_the_same_shared_policy_from_any_profile(root, monkeypatch):
    _protect_as_default(root, monkeypatch)
    _empty_cic_profile_policy(root)
    _as(monkeypatch, root, "cic")
    spec = dict(goal="g", workers=[kanban_swarm.SwarmWorkerSpec(profile="w", title="t", body="b")],
                verifier_assignee="v", synthesizer_assignee="s")
    with kbc.connect(board="gsd") as conn, pytest.raises(kanban_swarm.SwarmRefusedError):
        kanban_swarm.create_swarm(conn, **spec)
    with kbc.connect() as conn:  # the default board is not protected: unchanged
        assert kanban_swarm.create_swarm(conn, **spec).root_id


def test_operator_command_writes_the_shared_policy_and_is_refused_in_worker_context(root, monkeypatch):
    _as(monkeypatch, root, "cic")
    assert "Saved. Protected boards: gsd" in kc.run_slash("acceptance protect gsd")
    _as(monkeypatch, root, "default")
    shown = kc.run_slash("acceptance show")
    assert "protected boards: gsd" in shown and "verifier set: no" in shown
    assert "no such board" in kc.run_slash("acceptance protect nosuchboard")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    assert "operator-only" in kc.run_slash("acceptance unprotect gsd")
    assert ks.read_policy()["boards"] == ["gsd"]
    assert (root / "kanban" / "human_acceptance.json").stat().st_mode & 0o077 == 0
