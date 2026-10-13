"""Last-session recall for Realtime voice: a plain, deterministic digest of the speaker's latest Discord
conversations, read straight from the session store when a Realtime session starts. No model, no
network, no writes: one read-only SQLite open, a few bounded queries, a hard latency budget.

Per session, best context first: its title (model-written when the session was created), the Goal and
Active State of its newest compaction summary (newest session of each channel only; model-written at
compaction time, so labelled as possibly incomplete), then its last messages verbatim. Layers that do
not exist are simply absent.

Source: ``<HERMES_HOME>/state.db`` (``hermes_state_common.SCHEMA_SQL``): ``sessions`` rows with
``source = 'discord'`` carry ``chat_id`` / ``chat_type`` / ``thread_id`` / ``display_name`` / ``title``;
their ``messages`` rows hold the text (``_compressed_summary = 1`` rows are compaction summaries). Only the allowlisted speakers' own sessions are read, and only live
(``active = 1``) user/assistant text, secrets redacted. Any failure, an expired budget included,
yields an empty digest: recall is a convenience and must never delay or break a session start."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Tuple

from agent.redact import redact_sensitive_text
from hermes_state_holders import read_only_db_uri

logger = logging.getLogger(__name__)

RECALL_BUDGET_S = 0.25          # whole digest: open, queries, formatting
MAX_CHANNELS = 4
MAX_SESSIONS_PER_CHANNEL = 2
MESSAGES_PER_SESSION = 5
MAX_MESSAGE_CHARS = 240
MAX_DIGEST_CHARS = 8000
MAX_TITLE_CHARS = 100
MAX_SUMMARY_PART_CHARS = 300
_CANDIDATE_SESSIONS = 40        # newest sessions considered before grouping by channel
_RAW_MESSAGE_CHARS = 2000       # bound on text handed to the redactor

# Headings of the sections of a compaction summary that say where things stand (agent/context_compressor.py).
_SUMMARY_PARTS = (("goal", "Goal"), ("active state", "Active State"))
_SUMMARY_SQL = ("SELECT content FROM messages WHERE session_id = ? AND _compressed_summary = 1 "
                "ORDER BY id DESC LIMIT 1")
_SESSIONS_SQL = (
    "SELECT id, chat_id, chat_type, thread_id, display_name, COALESCE(last_activity_at, started_at), title "
    "FROM sessions WHERE source = 'discord' AND chat_id IS NOT NULL AND hidden = 0 AND user_id IN ({marks}) "
    "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT ?")
_MESSAGES_SQL = (
    "SELECT role, content FROM messages WHERE session_id = ? AND active = 1 AND _compressed_summary = 0 "
    "AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != '' ORDER BY id DESC LIMIT ?")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


class _BudgetExpired(Exception):
    pass


def _clean(text: Any, limit: int) -> str:
    """One line of plain, redacted text, at most *limit* characters; never able to close its wrapper."""
    raw = text if isinstance(text, str) else ""
    redacted = redact_sensitive_text(raw[:_RAW_MESSAGE_CHARS], force=True, redact_url_credentials=True)
    line = " ".join(_CONTROL_CHARS.sub(" ", redacted).split()).replace("</recall", "<\\/recall")
    return line if len(line) <= limit else line[: max(limit - 1, 0)].rstrip() + "…"


def _summary_parts(content: Any) -> List[Tuple[str, str]]:
    """(label, text) for the Goal / Active State sections of a compaction summary; nothing when absent."""
    if not isinstance(content, str):
        return []
    parts = []
    for label, heading in _SUMMARY_PARTS:
        found = re.search(rf"(?ms)^## {re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", content)
        text = _clean(found.group(1), MAX_SUMMARY_PART_CHARS) if found else ""
        if text:
            parts.append((label, text))
    return parts


def _stamp(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown time"


def _pick(rows: Iterable[Tuple]) -> List[Tuple[str, List[Tuple]]]:
    """Newest-first session rows -> up to MAX_CHANNELS channels (by newest activity), each with up to
    MAX_SESSIONS_PER_CHANNEL of its own newest sessions."""
    channels: Dict[str, List[Tuple]] = {}
    for row in rows:
        chat_id = str(row[1])
        if chat_id not in channels and len(channels) >= MAX_CHANNELS:
            continue
        sessions = channels.setdefault(chat_id, [])
        if len(sessions) < MAX_SESSIONS_PER_CHANNEL:
            sessions.append(row)
    return list(channels.items())


def recall_digest(db_path: Any, user_ids: Iterable[Any], *, budget_s: float = RECALL_BUDGET_S,
                  max_chars: int = MAX_DIGEST_CHARS, clock: Callable[[], float] = time.monotonic) -> str:
    """Plain-text digest of the speakers' latest Discord channels, sessions and messages; ``""`` when
    there is nothing to recall or anything at all goes wrong (missing/corrupt store, budget exhausted)."""
    ids = sorted({str(u) for u in user_ids if u})
    if not ids:
        return ""
    deadline = clock() + budget_s

    def expired() -> bool:
        return clock() > deadline

    conn = None
    try:
        conn = sqlite3.connect(read_only_db_uri(db_path), uri=True, timeout=0.05)
        conn.execute("PRAGMA query_only = 1")
        conn.set_progress_handler(lambda: 1 if expired() else 0, 1000)   # SQLite aborts a slow query
        rows = conn.execute(_SESSIONS_SQL.format(marks=",".join("?" * len(ids))),
                            (*ids, _CANDIDATE_SESSIONS)).fetchall()
        sections: List[str] = []
        for chat_id, sessions in _pick(rows):
            first = sessions[0]
            section = [f"Channel {_clean(first[4] or 'unnamed', 80)} (id {chat_id}, {first[2] or 'chat'})"]
            for rank, (sid, _cid, _ctype, thread_id, _name, active_at, title) in enumerate(sessions):
                if expired():
                    raise _BudgetExpired
                messages = conn.execute(_MESSAGES_SQL, (sid, MESSAGES_PER_SESSION)).fetchall()
                if not messages:
                    continue
                where = f", thread {thread_id}" if thread_id else ""
                topic = _clean(title, MAX_TITLE_CHARS)
                quoted = f' "{topic}"' if topic else ""
                section.append(f"  Session {str(sid)[:24]}{quoted} (last active {_stamp(active_at)}{where}):")
                summary = conn.execute(_SUMMARY_SQL, (sid,)).fetchone() if rank == 0 else None
                parts = _summary_parts(summary[0]) if summary else []
                if parts:
                    section.append("    Compaction summary (model-written, may be incomplete):")
                    section.extend(f"      {label}: {text}" for label, text in parts)
                section.append("    Last messages, oldest first:")
                for role, content in reversed(messages):
                    text = _clean(content, MAX_MESSAGE_CHARS)
                    if text:
                        section.append(f"      {role}: {text}")
            if len(section) > 1:
                sections.append("\n".join(section))
        # Whole channels while they fit (newest first); only a lone oversize channel is cut mid-text.
        digest = ""
        for block in sections:
            joined = f"{digest}\n{block}" if digest else block
            if len(joined) > max_chars:
                break
            digest = joined
        if not digest and sections:
            digest = sections[0][: max_chars - 1].rstrip() + "…"
        return digest
    except (sqlite3.Error, _BudgetExpired, OSError, ValueError) as e:
        logger.debug("Realtime recall unavailable: %s", type(e).__name__)
        return ""
    finally:
        if conn is not None:
            conn.close()
