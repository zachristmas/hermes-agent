"""Review-handoff contract for the direct Herdr bridge, for boards that hand a card to a human
``/open-pr`` (``kanban.direct_herdr_open_pr_handoff_boards``).

The bridge used to save whatever the agent's terminal showed as the Review summary, so a usage-limit
banner, a startup screen or the echoed task prompt became the "handoff" (Titan cards t_adc72060 and
t_e119b0f7, 2026-10-16). On a gated board a settled turn is now only a lifecycle signal. The card
reaches Review only with a deliberate handoff:

1. a ``HANDOFF-BEGIN run=<this run> task=<this task>`` ... ``HANDOFF-END`` block in the transcript
   whose first body line is a real ``/open-pr`` command, or
2. the card's last comment, when its first line is a real ``/open-pr`` command.

Anything else (banner or usage/auth error, the echoed instructions, a blank turn) is a failure
reported with the transcript tail as evidence; it never turns into a Review summary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

BEGIN_MARK = "HANDOFF-BEGIN"
END_MARK = "HANDOFF-END"

# Terminal bullets/prefixes an interactive agent puts in front of its own lines.
_LEAD = r"[\s●⏺•>*]*"
_BEGIN_RE = re.compile(rf"^{_LEAD}{BEGIN_MARK}\s+run=(\d+)\s+task=(\S+)\s*$")
_END_RE = re.compile(rf"^{_LEAD}{END_MARK}\s*$")
_FENCE_RE = re.compile(r"^```[a-z]*\s*$")
_PLACEHOLDER_RE = re.compile(r"<[^<>\n]{1,120}>|\b(?:placeholder|TBD|TODO)\b|\.\.\.|…", re.IGNORECASE)
_MIN_COMMAND_LEN = len("/open-pr ") + 8

_USAGE_RE = re.compile(
    r"hit your [\w ]{0,20}limit|usage[- ]limit|limit reached|/usage-credits|reset it now",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"please run /login|not logged in|invalid api key|oauth token|authentication[ _](?:error|failed)",
    re.IGNORECASE,
)

_TAIL_LINES = 30
_TAIL_CHARS = 3000


@dataclass(frozen=True)
class Handoff:
    ok: bool
    summary: str = ""
    source: str = ""  # "transcript" | "card_comment"
    reason: str = ""


def contract_instructions(task_id: str, run_id: int) -> str:
    """The text appended to the agent's task prompt. It carries this run's real ids but a
    placeholder command, so its own echo can never validate as a handoff."""
    return "\n".join([
        "",
        "Handoff contract for this board: when the work is ready for a human /open-pr, end with "
        "exactly one block and nothing after it:",
        "",
        f"{BEGIN_MARK} run={run_id} task={task_id}",
        "/open-pr <the real, copy-ready command for this delivery>",
        "<2 to 6 short plain lines: what changed, what you verified (only results you saw), "
        "what stays gated>",
        END_MARK,
        "",
        "The line after the begin marker must be the actual command, not a placeholder. If you "
        "cannot produce one, emit no block and say what is blocking. Posting the same command as "
        "the card's last comment (`hermes kanban comment`) is also accepted.",
    ])


def _normalize(text: str) -> str:
    return " ".join(text.split())


def valid_open_pr_line(line: str) -> bool:
    """A real, copy-ready ``/open-pr <args>`` line: no angle-bracket or TODO-style placeholders."""
    line = line.strip()
    if not line.startswith("/open-pr") or len(line) < _MIN_COMMAND_LEN:
        return False
    if not line[len("/open-pr")].isspace():
        return False
    return _PLACEHOLDER_RE.search(line) is None


def _body_to_summary(lines: list[str]) -> Optional[str]:
    """Command first, then the prose. ``None`` when the first line is not a real command."""
    lines = [ln.strip() for ln in lines]
    while lines and not lines[0]:
        lines.pop(0)
    if lines and _FENCE_RE.match(lines[0]):
        lines.pop(0)
        while lines and not lines[0]:
            lines.pop(0)
    if not lines or not valid_open_pr_line(lines[0]):
        return None
    command, rest = lines[0], [ln for ln in lines[1:] if not _FENCE_RE.match(ln)]
    while rest and not rest[0]:
        rest.pop(0)
    while rest and not rest[-1]:
        rest.pop()
    return "\n".join([command, "", *rest]) if rest else command


def extract_transcript_handoff(transcript: str, prompt: str, task_id: str, run_id: int) -> Optional[str]:
    """The last deliberate handoff block for THIS run and task, as a command-first summary.

    A block is skipped when its text is contained in the prompt (an echo of the instructions) or
    its command line is a placeholder.
    """
    prompt_norm = _normalize(prompt)
    found: Optional[str] = None
    lines = transcript.splitlines()
    i = 0
    while i < len(lines):
        m = _BEGIN_RE.match(lines[i])
        i += 1
        if not m or int(m.group(1)) != run_id or m.group(2) != task_id:
            continue
        body: list[str] = []
        j = i
        while j < len(lines) and not _END_RE.match(lines[j]):
            body.append(lines[j])
            j += 1
        if j >= len(lines):
            break  # unterminated block: not a deliberate, finished handoff
        i = j + 1
        summary = _body_to_summary(body)
        if summary is not None and _normalize(summary) not in prompt_norm:
            found = summary
    return found


def comment_handoff(last_comment_body: Optional[str]) -> Optional[str]:
    """The card's LAST comment as a command-first summary, when it opens with a real /open-pr."""
    if not last_comment_body:
        return None
    return _body_to_summary(last_comment_body.splitlines())


def failure_reason(transcript: str) -> str:
    """Why a settled turn without a handoff is not a success, naming the likely cause."""
    if _USAGE_RE.search(transcript):
        return "the agent hit a usage limit before producing a handoff"
    if _AUTH_RE.search(transcript):
        return "the agent could not start (login or authentication error)"
    if not transcript.strip():
        return "the agent's turn settled with no output"
    return "the agent's turn settled without a handoff block or a /open-pr card comment"


def resolve_handoff(
    transcript: str, prompt: str, task_id: str, run_id: int, last_comment_body: Optional[str],
) -> Handoff:
    """Transcript block for this run first, then the card's last comment. Otherwise a failure
    whose reason carries the transcript tail, so the evidence is not lost."""
    summary = extract_transcript_handoff(transcript, prompt, task_id, run_id)
    if summary is not None:
        return Handoff(True, summary, "transcript")
    summary = comment_handoff(last_comment_body)
    if summary is not None:
        return Handoff(True, summary, "card_comment")
    tail = "\n".join(transcript.strip().splitlines()[-_TAIL_LINES:])[-_TAIL_CHARS:]
    reason = (
        f"Not moved to Review: {failure_reason(transcript)}. A Review handoff needs a "
        f"{BEGIN_MARK} block for this run, or a last card comment that starts with a real "
        f"/open-pr command."
    )
    return Handoff(False, "", "", f"{reason}\n\nTranscript tail:\n{tail}" if tail else reason)
