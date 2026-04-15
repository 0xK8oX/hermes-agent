"""
Hall Extension — Inter-soul Messaging (Shared Memory Board)
============================================================

A lightweight message board that lets different souls communicate
across channels.  Souls can post messages to specific recipients
or broadcast to all.  Unread messages are auto-injected into the
ephemeral context when a new session starts.

Storage:
    ``~/.hermes/hall.jsonl`` — append-only JSON Lines log.

Each entry:
    {
        "id": "unique-int-id",
        "from": "pm",
        "to": "dev" | "all",
        "subject": "New task assigned",
        "body": "Check GitHub issue #42",
        "priority": "normal" | "high",
        "ts": "2026-04-16T01:30:00",
        "read_by": ["dev"]
    }

Hooks registered:
    - ``get_ephemeral(session_key)`` — injects unread messages as soul context
    - ``on_session_cleanup(session_key)`` — optionally purges old messages

Public API (used by memory tool or other callers):
    - ``hall_send(from_soul, to_soul, subject, body, priority)`` → entry dict
    - ``hall_read(soul, mark_read)`` → list of unread messages
    - ``hall_list(soul, limit)`` → recent messages (read + unread)
    - ``hall_mark_read(msg_id, soul)`` → mark single message as read
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.extensions import register_hook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_MAX_HALL_SIZE = 10_000  # max entries before auto-purge
_PURGE_OLDER_THAN_DAYS = 30


def _hall_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home()
    except ImportError:
        base = Path.home() / ".hermes"
    return base / "hall.jsonl"


def _ensure_hall() -> Path:
    p = _hall_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.touch()
    return p


def _read_all_entries() -> List[dict]:
    p = _hall_path()
    if not p.exists():
        return []
    entries = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _write_all_entries(entries: List[dict]) -> None:
    p = _ensure_hall()
    with open(p, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Soul resolution — reuse channel_binding's session state
# ---------------------------------------------------------------------------

def _get_soul_name(session_key: str) -> Optional[str]:
    """Get the soul name for a session from channel_binding state."""
    try:
        from gateway.extensions.channel_binding import _session_soul_names
        return _session_soul_names.get(session_key)
    except (ImportError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def hall_send(
    from_soul: str,
    to_soul: str,
    subject: str,
    body: str,
    priority: str = "normal",
) -> dict:
    """Post a message to the Hall.

    Args:
        from_soul: sender soul name (e.g. "pm")
        to_soul: recipient soul name (e.g. "dev") or "all" for broadcast
        subject: short subject line
        body: message body
        priority: "normal" or "high"

    Returns:
        The created entry dict.
    """
    # Validate inputs
    if not from_soul or not isinstance(from_soul, str):
        raise ValueError("from_soul must be a non-empty string")
    if not to_soul or not isinstance(to_soul, str):
        raise ValueError("to_soul must be a non-empty string")
    if priority not in ("normal", "high"):
        priority = "normal"

    entry = {
        "id": uuid.uuid4().hex[:12],
        "from": from_soul.lower().strip(),
        "to": to_soul.lower().strip(),
        "subject": subject.strip(),
        "body": body.strip(),
        "priority": priority,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "read_by": [],
    }

    p = _ensure_hall()
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    logger.info(
        "[Hall] %s → %s: %s (priority=%s)",
        from_soul, to_soul, subject[:50], priority,
    )
    return entry


def hall_read(soul: str, mark_read: bool = True) -> List[dict]:
    """Read unread messages for a soul.

    Args:
        soul: the soul name to fetch messages for
        mark_read: if True, mark fetched messages as read

    Returns:
        List of unread message dicts.
    """
    if not soul:
        return []

    soul = soul.lower().strip()
    entries = _read_all_entries()
    if not entries:
        return []

    unread = []
    modified = False
    for entry in entries:
        to_val = entry.get("to", "")
        # Match if message is addressed to this soul, or "all"
        if to_val in (soul, "all") and soul not in entry.get("read_by", []):
            unread.append(entry)
            if mark_read:
                entry.setdefault("read_by", []).append(soul)
                modified = True

    if modified:
        _write_all_entries(entries)

    return unread


def hall_list(soul: str = None, limit: int = 20) -> List[dict]:
    """List recent messages, optionally filtered by recipient.

    Args:
        soul: if provided, only show messages where to matches soul or "all"
        limit: max messages to return (most recent first)

    Returns:
        List of message dicts (newest first).
    """
    entries = _read_all_entries()
    if soul:
        soul = soul.lower().strip()
        entries = [
            e for e in entries
            if e.get("to") in (soul, "all")
        ]
    # Return newest first
    entries.reverse()
    return entries[:limit]


def hall_mark_read(msg_id: str, soul: str) -> bool:
    """Mark a specific message as read by a soul.

    Returns:
        True if the message was found and updated.
    """
    if not msg_id or not soul:
        return False

    soul = soul.lower().strip()
    entries = _read_all_entries()
    found = False
    for entry in entries:
        if str(entry.get("id")) == str(msg_id):
            if soul not in entry.get("read_by", []):
                entry.setdefault("read_by", []).append(soul)
                found = True
            break

    if found:
        _write_all_entries(entries)
    return found


def hall_clear(soul: str = None, older_than_days: int = _PURGE_OLDER_THAN_DAYS) -> int:
    """Purge old messages.

    Args:
        soul: if provided, only purge messages sent TO this soul
        older_than_days: remove messages older than N days

    Returns:
        Number of messages removed.
    """
    entries = _read_all_entries()
    if not entries:
        return 0

    cutoff = time.time() - (older_than_days * 86400)
    original_count = len(entries)

    filtered = []
    for entry in entries:
        # Parse ts
        try:
            ts_str = entry.get("ts", "")
            ts_epoch = time.mktime(time.strptime(ts_str, "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, OverflowError):
            ts_epoch = 0

        is_old = ts_epoch < cutoff
        is_target = (soul is None or entry.get("to") in (soul.lower(), "all"))

        if not (is_old and is_target):
            filtered.append(entry)

    removed = original_count - len(filtered)
    if removed:
        _write_all_entries(filtered)
        logger.info("[Hall] Purged %d old messages", removed)
    return removed


# ---------------------------------------------------------------------------
# Hook: inject unread messages into ephemeral context
# ---------------------------------------------------------------------------

def _get_ephemeral(session_key: str) -> Optional[str]:
    """Inject unread Hall messages into the session's ephemeral context.

    This is called by ``fire_hooks_first("get_ephemeral", session_key)``.
    Returns a formatted string with unread messages, or None.
    """
    soul = _get_soul_name(session_key)
    if not soul:
        return None

    unread = hall_read(soul, mark_read=True)
    if not unread:
        return None

    lines = [f"## 📬 Hall — Unread Messages ({len(unread)})\n"]
    for msg in unread:
        priority_badge = "🔴 " if msg.get("priority") == "high" else ""
        lines.append(
            f"**{priority_badge}From: {msg['from']}** | {msg.get('subject', '(no subject)')}\n"
            f"> {msg.get('body', '')}\n"
            f"> _{msg.get('ts', '')}_\n"
        )
    lines.append("_Use `hall_list` to see all messages, `hall_send` to reply._")

    content = "\n".join(lines)
    logger.info("[Hall] Injected %d unread messages for soul '%s'", len(unread), soul)
    return content


def _on_session_cleanup(session_key: str) -> None:
    """Auto-purge old messages when sessions are cleaned up."""
    hall_clear(older_than_days=_PURGE_OLDER_THAN_DAYS)


# ---------------------------------------------------------------------------
# Register hooks
# ---------------------------------------------------------------------------

register_hook("get_ephemeral", _get_ephemeral)
register_hook("on_session_cleanup", _on_session_cleanup)
