"""Review-summary and artifact boundary regressions for direct Herdr dispatch."""
import tempfile
import unittest
from pathlib import Path

from hermes_cli.kanban_settlement import _review_summary, _verified_review_artifacts


class ReviewSummaryTests(unittest.TestCase):
    def test_extracts_final_summary_without_terminal_chrome(self):
        transcript = """• Inspected files
• Verified implementation

PR #42: https://github.com/example/repo/pull/42
Changes: analytics endpoint and setup documentation.

Worked for 4m

› Ask Codex to do anything
  GPT-6.1-Sol low
"""
        summary = _review_summary(transcript)
        self.assertEqual(summary, "PR #42: https://github.com/example/repo/pull/42 Changes: analytics endpoint and setup documentation.")

    def test_rejects_handoff_and_source_as_review_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CODEX_HANDOFF.md").write_text("transfer pending")
            (root / "transfer.py").write_text("print('transfer')")
            (root / "DELIVERABLE.md").write_text("actual review document")
            report = "Handoff: CODEX_HANDOFF.md; transfer.py; deliverable DELIVERABLE.md"
            self.assertEqual(_verified_review_artifacts(report, directory), [str((root / "DELIVERABLE.md").resolve())])

    def test_handoff_only_is_not_an_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CODEX_HANDOFF.md").write_text("transfer pending")
            transcript = """I wrote CODEX_HANDOFF.md and queued a transfer.

The requested implementation is still pending.
"""
            self.assertEqual(_verified_review_artifacts(transcript, directory), [])

    def test_no_handoff_is_review_summary_when_artifact_missing(self):
        transcript = """• I wrote a handoff (CODEX_HANDOFF.md) and queued a transfer.

The requested implementation is still pending.

Worked for 2m

› Ask Codex to do anything
"""
        self.assertEqual(_review_summary(transcript), "Worker handoff only. Requested deliverable remains incomplete.")
