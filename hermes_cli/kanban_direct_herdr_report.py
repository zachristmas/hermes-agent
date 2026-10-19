"""Assignment in, final report out, for one direct-Herdr run (or reply run).

The bridge used to prompt a coding agent with the card's original title and body only, then took a
120-line terminal capture as the run's result. On a card that had been redirected by later comments
(t_6465d3ec: "the deliverable should be a pull request", "do not run tests") the agent re-ran the
original brief, and a retry that adopted the agent read old test claims plus the echoed prompt out
of scrollback and settled that to Review.

Now each run is fenced by its own name (``run-<run_id>`` or ``reply-<comment>-<lease>``):

- the full assignment (:func:`hermes_cli.kanban_db.build_worker_context`, which leads with the
  latest instructions) is written to ``<board>/runs/<task>/<fence>.assignment.md``, outside the task
  workspace so it can never be committed into the deliverable, and the agent is prompted with a
  short pointer to it plus the newest instructions inline;
- the agent writes its final report to ``<fence>.report.md``. That file is the run's only result
  evidence: no file means no success, whatever the terminal shows. A fresh fence name per run means
  a stale agent from an earlier run can never satisfy this run's report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_INLINE_INSTRUCTIONS = 3
_INLINE_CHARS = 400
_SECTION_RE = re.compile(r"^#{1,3}\s*([A-Za-z][A-Za-z ]*?)\s*:?\s*$")
_CHECK_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*(.+?)\s*$")

RUN_REPORT_FORMAT = """\
## Status
complete | partial | blocked
## Reply
<a direct answer to each comment awaiting your reply, or "none">
## Deliverables
- [x] <each deliverable the body and later instructions ask for> - <evidence: path, URL, commit>
- [ ] <a requested deliverable you did not deliver> - <why>
## Summary
<what you did in THIS run and how you verified it; do not repeat earlier runs' claims you did not re-check>"""

REPLY_REPORT_FORMAT = """\
## Disposition
answer | action
## Reply
<your answer to the comment(s), written to the person who asked>
## Action
<only when Disposition is action: the one-line action the person explicitly asked for>"""


@dataclass
class FinalReport:
    sections: dict[str, str]
    delivered: list[str] = field(default_factory=list)
    unmet: list[str] = field(default_factory=list)

    def get(self, name: str) -> str:
        return self.sections.get(name, "").strip()

    @property
    def status(self) -> str:
        words = self.get("status").lower().split()
        return words[0].strip(".`*") if words else ""

    @property
    def disposition(self) -> str:
        words = self.get("disposition").lower().split()
        return words[0].strip(".`*") if words else ""

    @property
    def reply(self) -> str:
        text = self.get("reply")
        return "" if text.lower().strip(". ") in ("", "none", "n/a") else text


def run_dir(task_id: str, board: Optional[str]) -> Path:
    from hermes_cli import kanban_db as kb
    path = kb.workspaces_root(board=board).parent / "runs" / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def fence_paths(task_id: str, board: Optional[str], fence: str) -> tuple[Path, Path]:
    root = run_dir(task_id, board)
    return root / f"{fence}.assignment.md", root / f"{fence}.report.md"


def _inline_instructions(conn, task_id: str) -> list[str]:
    from hermes_cli import kanban_comment_replies as replies
    from hermes_cli import kanban_db as kb

    comments = kb.list_comments(conn, task_id)[-_INLINE_INSTRUCTIONS:]
    human = set(replies.human_comment_ids(conn, task_id))
    lines = []
    for c in reversed(comments):
        body = " ".join((c.body or "").split())
        if len(body) > _INLINE_CHARS:
            body = body[:_INLINE_CHARS] + "..."
        lines.append(f"- #{c.id} {c.author}{' (human)' if c.id in human else ''}: {body}")
    return lines


def write_assignment(
    task_id: str, board: Optional[str], fence: str, *, reply_through: Optional[int] = None,
) -> tuple[str, Path, Optional[int]]:
    """Write ``<fence>.assignment.md`` and return ``(short_prompt, report_path, answered_through)``.

    ``answered_through`` is the newest pending human comment the agent is asked to answer (``None``
    when nothing awaits a reply); the bridge advances the reply watermark only that far. A reply run
    (``reply_through`` set) is asked to answer and route, never to do the work itself: it holds no
    run claim, so work done there would bypass the run fences and gates.
    """
    from hermes_cli import kanban_comment_replies as replies
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    assignment_path, report_path = fence_paths(task_id, board, fence)
    report_path.unlink(missing_ok=True)
    conn = kbc.connect(board=board)
    try:
        task = kb.get_task(conn, task_id)
        context = kb.build_worker_context(conn, task_id)
        pending = [c for c in replies.pending_human_comments(conn, task_id)
                   if reply_through is None or c.id <= reply_through]
        inline = _inline_instructions(conn, task_id)
    finally:
        conn.close()
    answered_through = pending[-1].id if pending else None
    if reply_through is not None:
        asked = "\n".join(f"- #{c.id} {c.author}: {c.body.strip()}" for c in pending)
        instructions = (
            "# Reply on the card\n\n"
            f"A person commented on kanban task {task_id}, which is assigned to you. Answer them.\n\n"
            f"{asked}\n\n"
            "Answer from what you know and what you can check read-only (the card, the workspace, git "
            "state, the deliverables). Do NOT change files, run builds or tests, or start the work in "
            "this session: it holds no run claim. If the comment explicitly asks for something to be "
            "DONE (open a PR, change the implementation), set Disposition to action and name it under "
            "Action; the card is then handed back to you as a normal run. A question, status check or "
            "remark is Disposition answer.\n\n"
            f"Write your reply to {report_path} in exactly this format:\n\n{REPLY_REPORT_FORMAT}\n")
    else:
        instructions = (
            f"# Assignment for kanban task {task_id} ({fence})\n\n"
            "The latest instructions below override the original body wherever they conflict, "
            "including added deliverables and constraints such as not running tests.\n\n"
            f"When you finish, write your final report to {report_path} in exactly this format:\n\n"
            f"{RUN_REPORT_FORMAT}\n\n"
            "List every deliverable the body and the later instructions ask for. Mark one [ ] when "
            "you did not deliver it; an unmet deliverable keeps the card with you instead of going "
            "to Review. Only that report file counts: the terminal transcript is not read.\n")
    assignment_path.write_text(f"{instructions}\n---\n\n{context}", encoding="utf-8")
    verb = "Answer the comment on" if reply_through is not None else "work"
    prompt = "\n".join([
        f"{verb} kanban task {task_id} ({fence})",
        "",
        f"Title: {task.title if task else task_id}",
        "",
        f"Read your full assignment first: {assignment_path}",
        *(["", "Latest instructions, newest first (they override the original body):", *inline] if inline else []),
        "",
        f"Finish by writing your report to {report_path} in the format the assignment gives.",
    ])
    return prompt, report_path, answered_through


def read_final_report(report_path: Path) -> Optional[FinalReport]:
    """Parse the agent's report file, or ``None`` when it is missing, empty or has no
    Status/Disposition section (an unusable report is never read as success)."""
    try:
        text = report_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        match = _SECTION_RE.match(line.strip())
        if match:
            current = match.group(1).strip().lower()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    report = FinalReport({k: "\n".join(v).strip() for k, v in sections.items()})
    for line in report.get("deliverables").splitlines():
        check = _CHECK_RE.match(line)
        if check:
            (report.delivered if check.group(1).lower() == "x" else report.unmet).append(check.group(2))
    if not report.status and not report.disposition:
        return None
    return report


def review_text(report: FinalReport) -> str:
    """The Review summary: the agent's own summary and delivered evidence, verbatim."""
    parts = [report.get("summary")]
    if report.delivered:
        parts.append("Delivered:\n" + "\n".join(f"- {d}" for d in report.delivered))
    return "\n\n".join(p for p in parts if p).strip()


def unmet_requested(report: FinalReport, requested: list[str]) -> list[str]:
    """The report's own unmet deliverables, plus a requested pull request with no PR URL anywhere
    in the delivered evidence: an agent that forgets to list the PR cannot carry the card to
    Review on the rest of the work."""
    from hermes_cli import kanban_comment_replies as replies

    unmet = list(report.unmet)
    wants_pr = any(item.startswith("A pull request") for item in requested)
    if wants_pr and not any(replies.PR_URL.search(d) for d in report.delivered):
        unmet.append("pull request requested by a later instruction; no PR URL in the delivered evidence")
    return unmet


def requested_for(task_id: str, board: Optional[str]) -> list[str]:
    from hermes_cli import kanban_comment_replies as replies
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        return replies.requested_deliverables(kb.list_comments(conn, task_id))
    finally:
        conn.close()


def reply_fence(through_id: int, lock: str) -> str:
    return f"reply-{int(through_id)}-{lock[:8]}"
