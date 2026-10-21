"""False Blocked settlements (connect t_00c0e992, STORE-1191, runs 75/76).

The worker's only unchecked item was "Fix and scoped tests - not requested as a deliverable"; the
bridge counted it as unmet twice, the failure breaker gave up and the card sat in Blocked with no
prerequisite anyone could act on. Earlier sync runs had written "waiting on a screen-share" as if it
were required. These tests pin the settlement policy that replaces that: optional work is recorded
not counted, an exact-resolution card is not satisfied by a source-only hypothesis, a business
Blocked needs a typed prerequisite after the authorized reads, and retry exhaustion queues a durable
coordinator reconciliation instead of stranding the card.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kdiag
from hermes_cli import kanban_direct_herdr_report as run_report
from hermes_cli import kanban_direct_herdr_worker as bridge
from hermes_cli import kanban_report_policy as policy
from hermes_cli import kanban_settlement as ks

INCIDENT_BODY = """**Goal** - Determine why the Beta kit image is missing from the BOGO popup on Titan store MIP and provide evidence-backed findings.

**Approach**
- Investigate the relevant Titan Stores/Portal code and data using read-only QAS/dev access to identify the cause.
- Draft a copy-ready message for Zach to ping Jesse and arrange the agreed screen-share; do not post it.

**Acceptance criteria**
- Findings explain the most likely cause of the missing image and include supporting evidence, such as relevant code/data references or reproduction observations.
- The findings distinguish confirmed facts from anything that remains unverified.
- A copy-ready screen-share ping for Jesse is included in a card comment.
- No changes are made to the Connect ticket; any reply Zach needs to send is provided as copy-ready text in a card comment.
- Any repo changes, if needed, are limited to a feature branch or worktree, with scoped tests run; no PR is created.
- The task ends in request-review and is not marked Done or archived.

**Out of scope**
- Connect ticket writes or posting messages on Zach's behalf.
"""

# Run 76's report, trimmed to the parts settlement reads (status, checklist, summary).
RUN_76_REPORT = """## Status
partial

## Reply
none

## Deliverables
- [x] Findings explaining the most likely cause, with code references - productDetailCtrl.js:2023, Product.cshtml:935
- [x] Confirmed vs unverified split - No QAS/dev data or live-store check was done.
- [x] Copy-ready screen-share ping for Jesse - "Hi Jesse, when are you available for a short screen-share?"
- [ ] Fix and scoped tests - not requested as a deliverable and not done. Likely fix: set `$scope.bogoProduct.mainImage = product.Images[0]` in `setUpProductData`.
- [x] No Connect ticket changes - none made.
- [x] No production writes - none made.

## Summary
I re-verified the earlier run's code claims by reading productDetailCtrl.js and Product.cshtml. I did not call the API or the live store.
"""

INK_CORRECTION = (
    "Zach directed that this investigation should dig into production data and determine an exact "
    "resolution. Runs 75 and 76 explicitly skipped live-store/API and product-data checks. A Jesse "
    "screen-share is not an established prerequisite. Do not repeat source-only runs.")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return hermes_home


def _card(body: str = INCIDENT_BODY, *, comments: tuple = ()) -> str:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="STORE-1191 Beta kit image missing from BOGO popup", body=body,
                             assignee="cic")
        for author, text, source in comments:
            kb.add_comment(conn, tid, author=author, body=text, source=source)
    return tid


def _claim(tid: str) -> int:
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        claimed = kb.claim_task(conn, tid, claimer="cic")
        assert claimed is not None
        return int(claimed.current_run_id)


def _settle_report(tid: str, run_id: int, text: str) -> str:
    """Write the run's report where the bridge reads it, mark the card through the bridge, settle."""
    report_path = run_report.fence_paths(tid, None, f"run-{run_id}")[1]
    report_path.write_text(text, encoding="utf-8")
    saved, _ = bridge._mark_card(tid, None, run_id, True, "settled", settlement_owner=True,
                                 agent_name="connect-t_x", report_path=report_path)
    assert saved
    with kbc.connect_closing() as conn:
        return ks.settle_from_receipt(conn, tid)


def _events(tid: str, kind: str) -> list:
    with kbc.connect_closing() as conn:
        return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _task(tid: str):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid)


# --- the incident -------------------------------------------------------------------------------

def test_optional_fix_and_tests_is_a_follow_up_not_an_unmet_deliverable(home):
    """Run 76 under the card as it stood: the findings were the deliverable, the fix was not asked
    for. The card goes to Review with the fix recorded as follow-up, never a failure."""
    tid = _card()
    run_id = _claim(tid)

    assert _settle_report(tid, run_id, RUN_76_REPORT) == "review"

    task = _task(tid)
    assert task.status == "review"
    assert task.consecutive_failures == 0
    assert not _events(tid, "gave_up") and not _events(tid, "partial_result")
    receipt = _events(tid, "worker_result_receipt")[-1].payload
    assert receipt["unmet"] == []
    assert receipt["follow_ups"] and receipt["follow_ups"][0].startswith("Fix and scoped tests")
    assert "Optional follow-up" in receipt["report"]


def test_exact_resolution_card_keeps_a_source_only_hypothesis_off_review(home):
    """After the correction the card asks for an exact resolution. The same source-only report is a
    bounded partial (not Review, not Blocked); the optional fix is still not what keeps it open."""
    tid = _card(comments=(("Ink", INK_CORRECTION, "cli"),))
    run_id = _claim(tid)

    assert _settle_report(tid, run_id, RUN_76_REPORT) == "partial"

    task = _task(tid)
    assert task.status == "ready" and task.consecutive_failures == 1
    unmet = _events(tid, "partial_result_detail")[-1].payload["unmet"]
    assert any("exact resolution" in u for u in unmet)
    assert not any(u.startswith("Fix and scoped tests") for u in unmet)


def test_exact_resolution_needs_every_ungated_read_done(home):
    tid = _card(comments=(("Ink", INK_CORRECTION, "cli"),))
    run_id = _claim(tid)
    report = RUN_76_REPORT.replace("## Summary", (
        "## Investigation\n"
        "- [x] productDetailCtrl.js modal controller - mainImage never set\n"
        "- [ ] live product API response for MIC011456 - not called\n"
        "## Resolution\nexact\n## Summary"))

    assert _settle_report(tid, run_id, report) == "partial"
    unmet = _events(tid, "partial_result_detail")[-1].payload["unmet"]
    assert unmet == ["authorized read not performed: live product API response for MIC011456 - not called"]


def test_exact_resolution_with_reads_done_reaches_review(home):
    tid = _card(comments=(("Ink", INK_CORRECTION, "cli"),))
    run_id = _claim(tid)
    report = RUN_76_REPORT.replace("## Summary", (
        "## Investigation\n"
        "- [x] live product API response for MIC011456 - BOGO option has Images[0]\n"
        "- [ ] production image table write - prohibited (production write)\n"
        "## Resolution\nexact\n## Summary"))

    assert _settle_report(tid, run_id, report) == "review"


# --- business Blocked ---------------------------------------------------------------------------

SCREEN_SHARE_BLOCK = """## Status
blocked
## Deliverables
- [x] Findings - productDetailCtrl.js:2023
## Investigation
- [x] productDetailCtrl.js - mainImage never set
- [ ] live product API response - not called
## Blocker
Waiting for Zach to arrange the agreed Jesse screen-share.
## Summary
Titan investigation needs Windows.
"""


def test_a_screen_share_wish_with_reads_undone_is_not_a_business_block(home):
    tid = _card()
    run_id = _claim(tid)

    assert _settle_report(tid, run_id, SCREEN_SHARE_BLOCK) == "partial"

    task = _task(tid)
    assert task.status == "ready" and task.block_kind is None
    unmet = _events(tid, "partial_result_detail")[-1].payload["unmet"]
    assert unmet[0].startswith("blocked claim not accepted: no typed blocker")
    assert "authorized reads remain undone" in unmet[0]


@pytest.mark.parametrize("kind,needs,gated", [
    ("access", "grant read access to the QAS product image table", "QAS image table - access denied"),
    ("human_decision", "choose between restoring setUpImages and a data fix", "none"),
    ("production_write", "approve the production image record correction", "image record update - production write"),
])
def test_a_typed_prerequisite_after_the_reads_is_a_business_block(home, kind, needs, gated):
    tid = _card()
    run_id = _claim(tid)
    report = ("## Status\nblocked\n## Deliverables\n- [x] Findings - API response captured\n"
              "## Investigation\n- [x] live product API response - Images empty for the BOGO option\n"
              + (f"- [ ] {gated}\n" if gated != "none" else "")
              + f"## Blocker\nkind: {kind} | needs: {needs} | owner: zach\n## Summary\nData gap found.\n")

    assert _settle_report(tid, run_id, report) == "blocked"

    task = _task(tid)
    assert (task.status, task.block_kind) == ("blocked", "needs_input")
    settled = _events(tid, "settled_from_receipt")[-1].payload
    assert settled["prerequisite"] == {"kind": kind, "needs": needs, "owner": "zach"}
    assert not _events(tid, "reconciliation_queued")


def test_closeout_with_untyped_remaining_work_is_a_bounded_partial_not_blocked(home):
    """1257eeb's closeout blocked any executor problem with remaining work as needs_input."""
    tid = _card()
    run_id = _claim(tid)
    with kbc.connect_closing() as conn:
        assert ks.record_result_receipt(conn, tid, run_id, success=False, report="no report", closeout={
            "progress": {"finished": ["findings"], "remaining": ["live API read"], "wait_kind": "prerequisite"},
            "executor_problem": "agent settled without a report"})
        assert ks.settle_from_receipt(conn, tid) == "partial"
    task = _task(tid)
    assert task.status == "ready" and task.consecutive_failures == 1
    assert _events(tid, "progress_recorded")[-1].payload["finished"] == ["findings"]


def test_approval_hold_closeout_still_blocks(home):
    tid = _card()
    run_id = _claim(tid)
    with kbc.connect_closing() as conn:
        assert ks.record_result_receipt(conn, tid, run_id, success=False, report="push approval", closeout={
            "progress": {"finished": ["commit abc"], "remaining": ["push"], "wait_kind": "approval"},
            "executor_problem": "agent blocked on an approval dialog"})
        assert ks.settle_from_receipt(conn, tid) == "blocked"
    assert _task(tid).status == "blocked"


# --- repeated partials: reconciliation, durable across restart ----------------------------------

def test_repeated_partials_queue_a_durable_reconciliation(home):
    tid = _card(comments=(("Ink", INK_CORRECTION, "cli"),))
    for _ in range(2):
        run_id = _claim(tid)
        _settle_report(tid, run_id, RUN_76_REPORT)

    task = _task(tid)
    assert task.status == "blocked" and task.block_kind is None
    queued = _events(tid, "reconciliation_queued")
    assert len(queued) == 1
    payload = queued[0].payload
    assert payload["owner"] == "cic" and payload["trigger"] == "partial_result"
    assert "acceptance criteria" in payload["next_action"]
    assert [a["outcome"] for a in payload["attempts"]][:2] == ["gave_up", "partial_result"]
    assert any("exact resolution" in u for u in payload["unmet"])
    assert _events(tid, "gave_up")[-1].payload["reconciliation"] == payload["next_action"]

    # "Restart": a fresh connection and the sweep see the same open item; nothing re-queues the card.
    with kbc.connect_closing() as conn:
        open_items = ks.open_reconciliations(conn)
        assert [i["task_id"] for i in open_items] == [tid]
        assert ks.reconcile_settlements(conn) == []
        assert kb.get_task(conn, tid).status == "blocked"
        diags = kdiag.compute_task_diagnostics(kb.get_task(conn, tid), kb.list_events(conn, tid), [])
    assert "reconciliation_pending" in [d.kind for d in diags]

    later = int(time.time()) + ks.RECONCILE_ESCALATE_SECONDS + 1
    with kbc.connect_closing() as conn:
        assert ks.reconcile_reconciliations(conn, now=later) == [tid]
        assert ks.reconcile_reconciliations(conn, now=later + 60) == []
    assert len(_events(tid, "reconciliation_escalated")) == 1

    with kbc.connect_closing() as conn:
        assert kb.unblock_task(conn, tid)
        assert ks.open_reconciliations(conn) == []


def test_reconciliation_names_an_open_hold_and_offers_no_blind_unblock(home):
    tid = _card()
    run_id = _claim(tid)
    with kbc.connect_closing() as conn:
        ks.record_recovery_hold(conn, tid, reason="owner unknown after restart")
        with kb.write_txn(conn):
            payload = ks.queue_reconciliation(conn, tid, run_id, trigger="crashed", error="worker died",
                                              assignee="cic")
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        diags = kdiag.compute_task_diagnostics(kb.get_task(conn, tid), kb.list_events(conn, tid), [])
    assert payload["holds"] == [f"recovery hold on run {run_id} (uncertain effect)"]
    assert payload["next_action"].startswith("Resolve the open hold first")
    diag = next(d for d in diags if d.kind == "reconciliation_pending")
    assert "unblock" not in [a.kind for a in diag.actions]


# --- the kanban_block tool ----------------------------------------------------------------------

@pytest.fixture
def worker(home, monkeypatch):
    tid = _card()
    run_id = _claim(tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def test_kanban_block_refuses_a_business_block_without_a_typed_prerequisite(worker):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_block({"reason": "Waiting on Zach to arrange the Jesse screen-share",
                                       "kind": "needs_input"}))
    assert "kanban_block refused" in out["error"]
    assert _task(worker).status == "running"


def test_kanban_block_accepts_a_typed_prerequisite(worker):
    from tools import kanban_tools as kt

    prerequisite = {"kind": "access", "needs": "grant read access to the QAS product image table",
                    "owner": "zach", "investigated": ["live product API response for MIC011456"]}
    out = json.loads(kt._handle_block({"reason": "QAS image table is unreadable", "kind": "needs_input",
                                       "prerequisite": prerequisite}))
    assert "error" not in out
    task = _task(worker)
    assert task.status == "blocked"
    assert _events(worker, "block_prerequisite")[-1].payload["needs"] == prerequisite["needs"]


def test_kanban_block_transient_needs_no_prerequisite(worker):
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_block({"reason": "provider 502", "kind": "transient"}))
    assert "error" not in out


# --- policy units -------------------------------------------------------------------------------

def test_an_optional_marker_does_not_excuse_an_unconditional_acceptance_item():
    report = run_report.FinalReport({"status": "complete"}, unmet=[
        "Copy-ready screen-share ping for Jesse in a card comment - optional, skipped"])
    judged = policy.assess(report, criteria=policy.acceptance_criteria(INCIDENT_BODY))
    assert judged.follow_ups == []
    assert judged.required_unmet[0].startswith("Copy-ready screen-share ping")
