"""
Session Checkpoint Extension
=============================

Provides per-channel session continuity across gateway restarts.

When a session is active (has recent conversation), this extension:
- AUTO-SAVE: After each agent response, saves a lightweight checkpoint
  summarizing what was discussed and what's in progress.
- AUTO-LOAD: When a session resumes after restart (was_auto_reset=True),
  loads the last checkpoint and injects it into the context prompt so the
  agent can pick up where it left off.
- TTL: Checkpoints older than 24 hours are automatically expired.

Storage: ~/.hermes/checkpoints/{platform}/{channel_id}.json

Hook contract (sync fire_hooks):
  - ``save_checkpoint(session_key, session_id, message, response)`` — save
  - ``get_checkpoint(session_key)`` → str or None — load
  - ``on_session_cleanup(session_key)`` — remove stale checkpoint
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.extensions import register_hook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHECKPOINT_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_MAX_CHECKPOINTS = 500  # total checkpoint files before cleanup
_MAX_MESSAGE_PREVIEW = 300
_MAX_RESPONSE_PREVIEW = 300
_MAX_TOPICS = 10
_MAX_PENDING = 5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_checkpoints_dir() -> Path:
    """Return the checkpoints root directory."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "checkpoints"


def _parse_session_key(session_key: str) -> Dict[str, str]:
    """Extract platform and channel_id from a session key.

    Format: ``agent:main:{platform}:{chat_type}:{chat_id}[:...]``
    """
    parts = session_key.split(":")
    if len(parts) < 5:
        return {"platform": "unknown", "channel_id": "unknown"}
    return {
        "platform": parts[2],
        "channel_id": parts[4],
    }


def _checkpoint_path(platform: str, channel_id: str) -> Path:
    """Return the file path for a given channel's checkpoint."""
    d = _get_checkpoints_dir() / platform
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{channel_id}.json"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# In-memory cache: session_key → checkpoint dict
_cache: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _load_checkpoint_from_disk(session_key: str) -> Optional[dict]:
    """Load checkpoint from disk. Returns None if missing or expired."""
    info = _parse_session_key(session_key)
    path = _checkpoint_path(info["platform"], info["channel_id"])
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data.get("timestamp", 0)
        if time.time() - ts > _CHECKPOINT_TTL_SECONDS:
            # Expired — remove stale file
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("checkpoint load error for %s: %s", session_key, exc)
        return None


def _save_checkpoint_to_disk(session_key: str, data: dict) -> None:
    """Persist checkpoint data to disk."""
    info = _parse_session_key(session_key)
    path = _checkpoint_path(info["platform"], info["channel_id"])
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("checkpoint save error for %s: %s", session_key, exc)


def _get_or_load(session_key: str) -> Optional[dict]:
    """Get from cache, falling back to disk."""
    with _lock:
        if session_key in _cache:
            return _cache[session_key]
    data = _load_checkpoint_from_disk(session_key)
    if data:
        with _lock:
            _cache[session_key] = data
    return data


def _extract_topics(messages_summary: List[dict]) -> List[str]:
    """Extract topic keywords from recent messages.

    Each message_summary has 'role' and 'content' keys.
    Returns a list of short topic strings.
    """
    topics = []
    for msg in messages_summary[-_MAX_TOPICS:]:
        content = msg.get("content", "")
        if not content:
            continue
        # Take first meaningful line as topic hint
        first_line = content.strip().split("\n")[0][:100]
        if first_line:
            topics.append(first_line)
    return topics


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def _on_save_checkpoint(
    session_key: str,
    session_id: str,
    message: str,
    response: str,
) -> None:
    """Save a checkpoint after each agent turn."""
    if not session_key:
        return

    info = _parse_session_key(session_key)
    now = time.time()

    # Build checkpoint
    checkpoint = {
        "platform": info["platform"],
        "channel_id": info["channel_id"],
        "session_id": session_id,
        "timestamp": now,
        "updated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "last_message": (message or "")[:_MAX_MESSAGE_PREVIEW],
        "last_response": (response or "")[:_MAX_RESPONSE_PREVIEW],
        "topics": [],  # filled incrementally
    }

    # Merge with existing checkpoint to keep topic history
    existing = _get_or_load(session_key)
    if existing:
        old_topics = existing.get("topics", [])
        # Keep recent topics, add new one from user message
        new_topic = (message or "").strip().split("\n")[0][:100]
        if new_topic:
            topics = old_topics[-(_MAX_TOPICS - 1):] + [new_topic]
        else:
            topics = old_topics[-_MAX_TOPICS:]
        checkpoint["topics"] = topics
    else:
        new_topic = (message or "").strip().split("\n")[0][:100]
        if new_topic:
            checkpoint["topics"] = [new_topic]

    # Update cache + disk
    with _lock:
        _cache[session_key] = checkpoint
    _save_checkpoint_to_disk(session_key, checkpoint)


def _on_get_checkpoint(session_key: str) -> Optional[str]:
    """Load and format a checkpoint for context injection.

    Returns a formatted string to prepend to the context prompt,
    or None if no checkpoint exists.
    """
    if not session_key:
        return None

    data = _get_or_load(session_key)
    if not data:
        return None

    # Format the checkpoint as a system note
    ts = data.get("updated_at", "unknown")
    topics = data.get("topics", [])
    last_msg = data.get("last_message", "")
    last_resp = data.get("last_response", "")

    parts = [
        "[Session Checkpoint — this is context from your previous conversation before restart.]",
        f"Last active: {ts}",
    ]

    if topics:
        parts.append("Recent topics discussed:")
        for t in topics[-5:]:  # last 5 topics
            parts.append(f"  • {t}")

    if last_msg:
        parts.append(f"Last user message: {last_msg[:200]}")
    if last_resp:
        parts.append(f"Last assistant response: {last_resp[:200]}")

    parts.append("Use this context to maintain continuity. Don't repeat completed tasks.")

    return "\n".join(parts)


def _on_session_cleanup(session_key: str) -> None:
    """Remove checkpoint data when a session expires."""
    if not session_key:
        return

    with _lock:
        _cache.pop(session_key, None)

    info = _parse_session_key(session_key)
    path = _checkpoint_path(info["platform"], info["channel_id"])
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _on_session_reset(session_key: str) -> None:
    """On explicit /reset, clear the checkpoint (user chose to start fresh)."""
    _on_session_cleanup(session_key)


# ---------------------------------------------------------------------------
# Periodic cleanup
# ---------------------------------------------------------------------------

def _cleanup_expired() -> int:
    """Remove all expired checkpoint files. Returns count of removed files."""
    removed = 0
    cp_dir = _get_checkpoints_dir()
    if not cp_dir.exists():
        return 0

    now = time.time()
    for path in cp_dir.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ts = data.get("timestamp", 0)
            if now - ts > _CHECKPOINT_TTL_SECONDS:
                path.unlink()
                removed += 1
        except (json.JSONDecodeError, OSError):
            # Corrupted file — remove it
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass

    return removed


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register_hook("save_checkpoint", _on_save_checkpoint)
register_hook("get_checkpoint", _on_get_checkpoint)
register_hook("on_session_cleanup", _on_session_cleanup)
register_hook("on_session_reset", _on_session_reset)

logger.debug(
    "session_checkpoint extension loaded (TTL=%dh)",
    _CHECKPOINT_TTL_SECONDS // 3600,
)
