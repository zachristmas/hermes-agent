"""What a worker's final report may claim: required vs optional work, exact resolution, business Blocked.

connect t_00c0e992 (STORE-1191), runs 75/76: the worker read source only, skipped the live API and
production image reads, and listed "Fix and scoped tests - not requested as a deliverable" as an
unchecked deliverable. Settlement counted that optional item as unmet twice, the failure breaker
gave up, and the card sat in Blocked with no prerequisite anyone could act on. Earlier sync runs
had already written "waiting on a screen-share" as if it were a requirement.

This module is the policy those settlements lacked. It is pure (no DB, no I/O) so the bridge, the
closeout path and the ``kanban_block`` tool apply one rule set:

- an unchecked item is unmet only when it is required: optional follow-up (the report's
  ``Follow-up`` section, or an item the worker itself marks as not requested / optional / out of
  scope) is recorded, not counted, unless it matches an unconditional acceptance criterion;
- a card that asks for an exact resolution is not satisfied by a likely cause while authorized
  reads the worker could have done remain unchecked;
- a business Blocked needs a typed prerequisite (what kind, the concrete action, who acts) and
  investigation evidence with no authorized read left undone. Anything less is not a business
  block: the run settles as a bounded partial and, if that repeats, coordinator reconciliation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Kinds of intervention a worker may name as a business blocker. Each is something only a person
# (or an outside system) can supply; "I have not looked yet" is never one of them.
BLOCKER_KINDS = ("access", "approval", "human_decision", "external_dependency", "production_write")

_OPTIONAL_RE = re.compile(
    r"\b(not (?:requested|required|asked for|in scope|part of (?:the|this) (?:task|scope|acceptance))"
    r"|optional|out of scope|follow[- ]?up|nice to have|not a (?:requested )?deliverable)\b",
    re.IGNORECASE)
_CONDITIONAL_RE = re.compile(r"\b(if needed|if any|if required|if applicable|where needed|when needed)\b",
                             re.IGNORECASE)
# A skipped read is excused only by a named gate a person controls.
_GATE_RE = re.compile(
    r"\b(gate|access denied|no access|not authori[sz]ed|forbidden|requires? approval|needs? approval"
    r"|approval required|prohibited|not permitted|credentials? (?:missing|required|unavailable)"
    r"|production write)\b", re.IGNORECASE)
_EXACT_RE = re.compile(
    r"\b(exact (?:resolution|cause|root cause|fix|correction)|determine (?:the |an )?exact"
    r"|do not repeat source[- ]only|not (?:a )?source[- ]only)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = frozenset(
    "the and any are for with from that this into not was were has have been its use using only "
    "run ran when then than them they will can should must each all per".split())
_GENERIC_NEEDS = frozenset(("input", "help", "a person", "human", "someone", "zach", "review", "decision"))


@dataclass
class Assessment:
    """The settlement-relevant reading of one final report."""
    required_unmet: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    blocker: Optional[dict] = None
    blocker_problems: list[str] = field(default_factory=list)
    skipped_reads: list[str] = field(default_factory=list)

    @property
    def blocked_valid(self) -> bool:
        return self.blocker is not None and not self.blocker_problems


def acceptance_criteria(body: str) -> list[str]:
    """The bullets under the card body's "Acceptance criteria" heading (markdown or bold)."""
    lines = (body or "").splitlines()
    out: list[str] = []
    inside = False
    for raw in lines:
        line = raw.strip()
        heading = re.sub(r"^[#*\s]+|[*:\s]+$", "", line).lower()
        if heading.startswith("acceptance criteria") and len(heading) <= 40:
            inside = True
            continue
        if not inside:
            continue
        if line.startswith(("#", "**")) and not line.startswith(("- ", "* ")):
            break
        if re.match(r"^[-*]\s+|^\d+[.)]\s+", line):
            out.append(re.sub(r"^[-*]\s+|^\d+[.)]\s+", "", line))
    return out


def is_optional(item: str) -> bool:
    """The worker itself marks this item as outside what the card asked for."""
    return bool(_OPTIONAL_RE.search(item or ""))


def is_gated(item: str) -> bool:
    """A skipped read excused by a named gate a person controls (access, approval, a write ban)."""
    return bool(_GATE_RE.search(item or ""))


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP}


def _matching_criterion(item: str, criteria: Iterable[str]) -> Optional[str]:
    """An UNCONDITIONAL acceptance criterion this item is about (most of the criterion's words are in
    the item), else None. "Any repo changes, if needed, ... with scoped tests run" is conditional,
    so an optional fix-and-tests follow-up does not match it."""
    words = _words(item.split(" - ", 1)[0])
    for crit in criteria:
        if _CONDITIONAL_RE.search(crit):
            continue
        cw = _words(crit)
        if cw and len(words & cw) * 2 >= len(cw):
            return crit
    return None


def requires_exact_resolution(body: str, instructions: Iterable[str]) -> bool:
    """True when the card body or a later non-worker instruction asks for an exact resolution."""
    return any(_EXACT_RE.search(text or "") for text in (body, *instructions))


def section_items(text: str) -> list[str]:
    return [re.sub(r"^\s*[-*]\s*(\[[ xX]\]\s*)?", "", ln).strip() for ln in (text or "").splitlines()
            if ln.strip() and ln.strip().lower().strip(".") not in ("none", "n/a")]


def checklist(text: str) -> tuple[list[str], list[str]]:
    """``(done, not_done)`` from ``- [x] ...`` / ``- [ ] ...`` lines."""
    done, todo = [], []
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*[-*]\s*\[([ xX])\]\s*(.+?)\s*$", ln)
        if m:
            (done if m.group(1).lower() == "x" else todo).append(m.group(2))
    return done, todo


def parse_blocker(text: str) -> Optional[dict]:
    """``kind: access | needs: <action> | owner: <who>`` (pipes or one field per line)."""
    if not (text or "").strip():
        return None
    fields: dict[str, str] = {}
    for part in re.split(r"\s*\|\s*|\n", text):
        m = re.match(r"^\s*[-*]?\s*(kind|needs|owner|tried)\s*:\s*(.+?)\s*$", part, re.IGNORECASE)
        if m:
            fields[m.group(1).lower()] = m.group(2).strip().strip("`")
    return fields or None


def validate_blocker(blocker: Optional[dict], investigated: list[str], skipped: list[str]) -> list[str]:
    """Why this is NOT a valid business block ([] = valid)."""
    problems = []
    if not blocker:
        problems.append("no typed blocker (kind, needs, owner)")
    else:
        kind = (blocker.get("kind") or "").strip().lower()
        if kind not in BLOCKER_KINDS:
            problems.append(f"blocker kind {kind or '(none)'!r} is not one of {', '.join(BLOCKER_KINDS)}")
        needs = (blocker.get("needs") or "").strip()
        if len(needs) < 12 or needs.lower().strip(". ") in _GENERIC_NEEDS:
            problems.append("blocker 'needs' must name the concrete action that unblocks the work")
        if not (blocker.get("owner") or "").strip():
            problems.append("blocker 'owner' (who must act) is missing")
    if not investigated:
        problems.append("no investigation evidence: list the authorized reads done before blocking")
    if skipped:
        problems.append("authorized reads remain undone: " + "; ".join(skipped)[:300])
    return problems


def assess(report, *, criteria: Iterable[str] = (), exact_required: bool = False,
           extra_unmet: Iterable[str] = ()) -> Assessment:
    """Read a :class:`~hermes_cli.kanban_direct_herdr_report.FinalReport` against the card.

    ``extra_unmet``: required items settlement found itself (a requested PR with no URL); they are
    never reclassified as optional.
    """
    criteria = list(criteria)
    out = Assessment(follow_ups=section_items(report.get("follow-up") or report.get("follow up")))
    for item in report.unmet:
        crit = _matching_criterion(item, criteria) if is_optional(item) else None
        if is_optional(item) and crit is None:
            out.follow_ups.append(item)
        elif crit is not None:
            out.required_unmet.append(f"{item} (required by acceptance criterion: {crit[:160]})")
        else:
            out.required_unmet.append(item)
    out.required_unmet += [u for u in extra_unmet if u not in report.unmet]
    investigated, not_done = checklist(report.get("investigation"))
    out.skipped_reads = [r for r in not_done if not is_gated(r)]
    if exact_required:
        resolution = (report.get("resolution").lower().split() or [""])[0].strip(".`*")
        if resolution != "exact":
            out.required_unmet.append(
                "the card asks for an exact resolution; the report gives "
                f"{resolution or 'no Resolution'} (a likely cause from source alone does not satisfy it)")
        out.required_unmet += [f"authorized read not performed: {r}" for r in out.skipped_reads]
    if report.status == "blocked":
        out.blocker = parse_blocker(report.get("blocker"))
        out.blocker_problems = validate_blocker(out.blocker, investigated, out.skipped_reads)
    return out
