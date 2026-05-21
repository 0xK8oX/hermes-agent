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
        "dispatch": "auto" | "queued",
        "ts": "2026-04-16T01:30:00",
        "read_by": ["dev"]
    }

Hooks registered:
    - ``get_ephemeral(session_key)`` — injects unread messages as soul context
    - ``on_session_cleanup(session_key)`` — optionally purges old messages

Auto-dispatch:
    After ``hall_send()``, if the target soul is bound to a channel and the
    gateway is running, a cross-channel dispatch automatically triggers the
    target soul's agent.  This enables real-time inter-soul communication
    without waiting for a human to start a session on the target channel.

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
import threading
import time
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from gateway.extensions import register_hook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_MAX_HALL_SIZE = 10_000  # max entries before auto-purge
_PURGE_OLDER_THAN_DAYS = 30

# Module-level lock for thread-safe JSONL access.
# Gateway handles multiple platforms concurrently — without this,
# concurrent hall_send / hall_read / hall_mark_read can corrupt the file.
_hall_lock = threading.Lock()

# Bounded thread pool for auto-dispatch (replaces unbounded daemon thread spawning)
_dispatch_executor = ThreadPoolExecutor(max_workers=4)

# Rate limiting: last dispatch timestamp per soul
_last_dispatch_time: Dict[str, float] = {}
_dispatch_rate_lock = threading.Lock()
_DISPATCH_RATE_LIMIT_SECONDS = 2.0


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
# Soul → Channel reverse lookup (for auto-dispatch)
# ---------------------------------------------------------------------------

def _lookup_soul_channel(soul_name: str) -> Optional[Dict[str, str]]:
    """Find the (platform, chat_id) that a soul is bound to.

    Scans config.yaml channel_personality_bindings across all platforms.
    Returns the *first* match as {"platform": ..., "chat_id": ...} or None.
    """
    if not soul_name:
        return None
    soul_lower = soul_name.lower().strip()
    try:
        from gateway.extensions.channel_binding import _get_all_platform_bindings
        all_bindings = _get_all_platform_bindings()
    except (ImportError, Exception):
        return None

    for platform_name, bindings_list in all_bindings.items():
        if not isinstance(bindings_list, list):
            continue
        for entry in bindings_list:
            if isinstance(entry, dict) and entry.get("soul", "").lower() == soul_lower:
                chan_id = entry.get("id")
                if chan_id:
                    return {"platform": platform_name, "chat_id": str(chan_id)}
    return None


def _is_admin_soul(soul_name: str) -> bool:
    """Check if a soul is bound to a channel owned by an admin user.

    Admin users are defined in GATEWAY_ADMIN_USERS env var (comma-separated user IDs).
    A soul is considered 'admin' if any of its bound channels' user_id is an admin,
    OR if the soul name matches a known admin soul pattern (legacy fallback).
    """
    if not soul_name:
        return False

    soul_lower = soul_name.lower().strip()

    # Legacy fallback: well-known admin soul names
    if soul_lower in ("alpha", "pm"):
        return True

    # Check GATEWAY_ADMIN_USERS env var
    admin_env = os.environ.get("GATEWAY_ADMIN_USERS", "").strip()
    if not admin_env:
        return False

    admin_ids = set(aid.strip() for aid in admin_env.split(",") if aid.strip())
    if not admin_ids:
        return False

    # Check if the soul's bound channel has an admin user_id
    target = _lookup_soul_channel(soul_name)
    if not target:
        return False

    # Try to find the user_id for the soul's binding from config
    try:
        from gateway.extensions.channel_binding import _get_all_platform_bindings
        all_bindings = _get_all_platform_bindings()
        for platform_name, bindings_list in all_bindings.items():
            if not isinstance(bindings_list, list):
                continue
            for entry in bindings_list:
                if (isinstance(entry, dict)
                        and entry.get("soul", "").lower() == soul_lower):
                    user_id = str(entry.get("user_id", "")).strip()
                    if user_id and user_id in admin_ids:
                        return True
    except (ImportError, Exception):
        pass

    return False


def _dispatch_dir() -> Path:
    """Return the pending dispatch directory path."""
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home()
    except ImportError:
        base = Path.home() / ".hermes"
    d = base / "hall_dispatch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _failed_dispatch_dir() -> Path:
    """Return the dead-letter directory for permanently failed dispatches."""
    d = _dispatch_dir() / "failed"
    d.mkdir(parents=True, exist_ok=True)
    return d


_MAX_DISPATCH_RETRIES = 5
_BACKOFF_BASE_SECONDS = 5


def _write_dispatch_pending(entry: dict) -> None:
    """Write a pending dispatch file atomically for the gateway watcher to pick up.

    Uses temp-file + rename to avoid the watcher reading a partially-written file.
    Embeds retry tracking metadata so the watcher can apply exponential backoff.
    """
    d = _dispatch_dir()
    entry_id = entry.get("id", "unknown")
    pending_path = d / f"{entry_id}.json"
    tmp_path = d / f".{entry_id}.json.tmp"

    payload = {
        "entry": entry,
        "_meta": {
            "retries": 0,
            "next_retry": time.time(),
        },
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp_path, pending_path)
    logger.info("[Hall] Wrote pending dispatch file: %s", pending_path.name)


def _auto_dispatch(entry: dict) -> None:
    """After hall_send, try to trigger the target soul's agent via cross-channel dispatch.

    Two modes based on entry["dispatch"]:
      - "auto": Immediate dispatch — admin/command-initiated, target soul must respond.
      - "queued" (default): No dispatch — message waits for soul to read it.

    Uses a bounded ThreadPoolExecutor to avoid unbounded thread spawning.
    Rate-limited to at most one dispatch per soul per 2 seconds.
    If gateway runner is unavailable (subprocess), writes a pending file instead.
    """
    dispatch_mode = entry.get("dispatch", "queued")
    if dispatch_mode != "auto":
        return

    to_soul = entry.get("to", "")
    if not to_soul or to_soul == "all":
        return

    target = _lookup_soul_channel(to_soul)
    if not target:
        logger.debug("[Hall] No bound channel found for soul '%s', skipping auto-dispatch", to_soul)
        return

    # Rate limit: enforce minimum interval between dispatches per soul
    now = time.monotonic()
    with _dispatch_rate_lock:
        last = _last_dispatch_time.get(to_soul, 0.0)
        if now - last < _DISPATCH_RATE_LIMIT_SECONDS:
            logger.info(
                "[Hall] Rate-limited dispatch for soul '%s' (%.1fs since last)",
                to_soul, now - last,
            )
            return
        _last_dispatch_time[to_soul] = now

    # Fast path: check if we're in a subprocess first (synchronous — no thread needed)
    from gateway.run import get_gateway_runner
    runner = get_gateway_runner()

    if runner is None:
        # We're in a subprocess — write pending file synchronously (daemon threads
        # get killed when the main thread exits, so pending file may never be written)
        logger.info("[Hall] No gateway runner (subprocess), writing pending dispatch for '%s'", to_soul)
        _write_dispatch_pending(entry)
        return

    def _dispatch():
        try:
            logger.info(
                "[Hall] Auto-dispatch: soul '%s' → %s:%s",
                to_soul, target["platform"], target["chat_id"],
            )
            _execute_dispatch(entry, target)

        except Exception as e:
            logger.warning("[Hall] Auto-dispatch error for soul '%s': %s", to_soul, e)

    _dispatch_executor.submit(_dispatch)


def _execute_dispatch(entry: dict, target: dict) -> None:
    """Execute the actual cross-channel dispatch. Called from gateway process."""
    from gateway.extensions.cross_channel import dispatch_cross_channel

    to_soul = entry.get("to", "")
    from_soul = entry.get("from", "hall")
    subject = entry.get("subject", "")
    body = entry.get("body", "")
    msg_id = entry.get("id", "")
    text = (
        f"📬 **Hall Message** (from: {from_soul})\n\n"
        f"**Subject:** {subject}\n\n"
        f"{body}\n\n"
        f"_Message ID: {msg_id} — use `hall` tool to reply._"
    )
    result_json = dispatch_cross_channel(
        platform=target["platform"],
        chat_id=target["chat_id"],
        text=text,
        source_session_key=f"hall:{from_soul}",
        source_user_name=f"{from_soul} (via Hall)",
    )
    import json as _json
    result = _json.loads(result_json)
    if result.get("success"):
        logger.info(
            "[Hall] Auto-dispatched to %s:%s for soul '%s'",
            target["platform"], target["chat_id"], to_soul,
        )
    else:
        error_msg = result.get("error", "unknown")
        logger.warning(
            "[Hall] Auto-dispatch failed for soul '%s': %s",
            to_soul, error_msg,
        )
        raise RuntimeError(f"Dispatch failed for soul '{to_soul}': {error_msg}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def hall_send(
    from_soul: str,
    to_soul: str,
    subject: str,
    body: str,
    priority: str = "normal",
    dispatch: str = "queued",
) -> dict:
    """Post a message to the Hall.

    Args:
        from_soul: sender soul name (e.g. "pm")
        to_soul: recipient soul name (e.g. "dev") or "all" for broadcast
        subject: short subject line
        body: message body
        priority: "normal" or "high"
        dispatch: "auto" (immediate — admin sends) or "queued" (system notification)
            Default is "queued" — caller must explicitly pass dispatch="auto"
            to trigger immediate cross-channel dispatch.

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
    if dispatch not in ("auto", "queued"):
        dispatch = "queued"

    entry = {
        "id": uuid.uuid4().hex[:16],
        "from": from_soul.lower().strip(),
        "to": to_soul.lower().strip(),
        "subject": subject.strip(),
        "body": body.strip(),
        "priority": priority,
        "dispatch": dispatch,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "read_by": [],
    }

    p = _ensure_hall()
    with _hall_lock:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    logger.info(
        "[Hall] %s → %s: %s (priority=%s, dispatch=%s)",
        from_soul, to_soul, subject[:50], priority, dispatch,
    )

    # Auto-dispatch: trigger target soul's agent if dispatch="auto"
    if dispatch == "auto":
        _auto_dispatch(entry)

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
    with _hall_lock:
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
    entries: List[dict]
    with _hall_lock:
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
    with _hall_lock:
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
    with _hall_lock:
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
# Gateway command handlers (extracted from gateway/run.py)
# ---------------------------------------------------------------------------

async def start_hall_dispatch_watcher(running_check: Callable[[], bool], interval: int = 3) -> None:
    """Background task that processes pending Hall auto-dispatch requests.

    When ``hall_send(dispatch="auto")`` is called from a subprocess (cron,
    agent tool), it writes a pending file to ``~/.hermes/hall_dispatch/``.
    This watcher scans that directory and executes the dispatch inside the
    gateway process where the runner is available.

    Reliability features:
    - Exponential backoff: retry interval grows 5s, 10s, 20s, 40s, 80s.
    - Dead-letter queue: after 5 failed retries the file moves to ``failed/``.
    - Atomic writes: pending files are written via temp+rename (no partial reads).
    - Non-retryable errors (corrupted JSON, missing channel) are discarded immediately.
    """
    # Short initial delay — adapters need a moment to connect, but 15s is too long
    # for interactive use.  We also re-check on each loop, so a late-starting adapter
    # will be picked up on the next scan.
    await asyncio.sleep(3)
    logger.info("[Hall-Dispatch-Watcher] Started, scanning every %ds", interval)

    while running_check():
        try:
            dispatch_dir = _dispatch_dir()
            pending_files = sorted(dispatch_dir.glob("*.json"))
            now = time.time()

            for pf in pending_files:
                if not running_check():
                    return

                # Skip temp files from atomic writes
                if pf.name.startswith("."):
                    continue

                try:
                    payload = json.loads(pf.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    logger.error("[Hall-Dispatch-Watcher] Corrupted file %s: %s — discarding", pf.name, e)
                    pf.unlink(missing_ok=True)
                    continue

                # Support both new wrapped format and legacy bare-entry format
                if "entry" in payload:
                    entry = payload["entry"]
                    meta = payload.get("_meta", {})
                else:
                    entry = payload
                    meta = {}

                to_soul = entry.get("to", "")

                # Exponential backoff: honour next_retry timestamp
                next_retry = meta.get("next_retry", 0)
                if now < next_retry:
                    logger.debug(
                        "[Hall-Dispatch-Watcher] Skipping %s (backoff until %.0f)",
                        pf.name, next_retry,
                    )
                    continue

                retries = meta.get("retries", 0)
                if retries >= _MAX_DISPATCH_RETRIES:
                    # Move to dead-letter queue
                    failed_path = _failed_dispatch_dir() / pf.name
                    try:
                        os.replace(pf, failed_path)
                        logger.warning(
                            "[Hall-Dispatch-Watcher] Max retries exceeded for %s → moved to %s",
                            pf.name, failed_path,
                        )
                    except OSError:
                        pf.unlink(missing_ok=True)
                    continue

                target = _lookup_soul_channel(to_soul)
                if not target:
                    logger.warning(
                        "[Hall-Dispatch-Watcher] No channel found for soul '%s', discarding %s",
                        to_soul, pf.name,
                    )
                    pf.unlink(missing_ok=True)
                    continue

                logger.info(
                    "[Hall-Dispatch-Watcher] Processing pending dispatch (attempt %d/%d): %s → %s",
                    retries + 1, _MAX_DISPATCH_RETRIES, entry.get("from", "?"), to_soul,
                )

                try:
                    # Run dispatch in thread pool to avoid blocking event loop
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, _execute_dispatch, entry, target)

                    # Success — remove pending file
                    pf.unlink(missing_ok=True)
                    logger.info(
                        "[Hall-Dispatch-Watcher] Dispatch succeeded: %s → %s",
                        entry.get("from", "?"), to_soul,
                    )

                except Exception as e:
                    retries += 1
                    backoff = _BACKOFF_BASE_SECONDS * (2 ** (retries - 1))
                    next_retry = time.time() + backoff

                    # Rewrite file with updated metadata for next attempt
                    updated_payload = {
                        "entry": entry,
                        "_meta": {
                            "retries": retries,
                            "next_retry": next_retry,
                            "last_error": str(e)[:200],
                        },
                    }
                    tmp_path = dispatch_dir / f".{pf.stem}.json.tmp"
                    try:
                        with open(tmp_path, "w", encoding="utf-8") as f:
                            json.dump(updated_payload, f, ensure_ascii=False)
                        os.replace(tmp_path, pf)
                    except OSError:
                        pass

                    logger.error(
                        "[Hall-Dispatch-Watcher] Dispatch failed for %s (attempt %d/%d), "
                        "retry in %ds: %s",
                        pf.name, retries, _MAX_DISPATCH_RETRIES, backoff, e,
                    )

        except Exception as e:
            logger.error("[Hall-Dispatch-Watcher] Scan error: %s", e)

        # Wait before next scan
        for _ in range(interval):
            if not running_check():
                return
            await asyncio.sleep(1)


async def handle_hall_send(runner: Any, event: Any) -> str:
    """Send a Hall message from the current soul to another soul."""
    from gateway.extensions.channel_binding import _session_soul_names, _ensure_binding_loaded

    args = event.get_command_args().strip()
    if not args:
        return (
            "Usage: /hall-send <target_soul> [subject] | <body>\n"
            "Example: /hall-send pm Status Update | Sprint review is done"
        )

    # Parse args: first word is target, rest is message. Optional subject separated by |
    parts = args.split(None, 1)
    to_soul = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    subject = ""
    body = rest
    if "|" in rest:
        subj_part, body_part = rest.split("|", 1)
        subject = subj_part.strip()
        body = body_part.strip()

    # Determine from_soul: check channel binding for current session
    session_key = runner._session_key_for_source(event.source)
    _ensure_binding_loaded(session_key)
    from_soul = _session_soul_names.get(session_key, "hall")

    # Admin souls get auto-dispatch (immediate), others get queued
    dispatch_mode = "auto" if _is_admin_soul(from_soul) else "queued"

    try:
        entry = hall_send(
            from_soul=from_soul, to_soul=to_soul, subject=subject,
            body=body, dispatch=dispatch_mode,
        )
        dispatch_tag = "⚡ auto" if dispatch_mode == "auto" else "📋 queued"
        return (
            f"✉️ Hall message sent! ({dispatch_tag})\n"
            f"From: {from_soul} → To: {to_soul}\n"
            f"Subject: {subject or '(none)'}\n"
            f"ID: {entry.get('id', '?' )}"
        )
    except Exception as e:
        return f"Failed to send Hall message: {e}"


async def handle_hall_read(runner: Any, event: Any) -> str:
    """Read unread Hall messages for the current soul."""
    from gateway.extensions.channel_binding import _session_soul_names, _ensure_binding_loaded

    session_key = runner._session_key_for_source(event.source)
    _ensure_binding_loaded(session_key)
    soul = _session_soul_names.get(session_key, "")

    if not soul:
        return "No soul bound to this channel. Use /bind first."

    messages = hall_read(soul, mark_read=True)

    if not messages:
        return f"📭 No unread messages for '{soul}'."

    lines = [f"📬 {len(messages)} unread message(s) for '{soul}':\n"]
    for msg in messages:
        lines.append(f"  From: {msg.get('from', '?')} | Subject: {msg.get('subject', '(none)')}")
        lines.append(f"  {msg.get('body', '')[:200]}")
        lines.append(f"  ID: {msg.get('id', '?')} | Time: {msg.get('ts', '?')}")
        lines.append("")

    return "\n".join(lines)


async def handle_hall_status(runner: Any, event: Any) -> str:
    """Show status of all bound channel souls and their unread counts."""
    from gateway.extensions.channel_binding import _get_all_platform_bindings

    all_bindings = _get_all_platform_bindings()
    if not all_bindings:
        return "No channel personality bindings configured."

    lines = ["🏛️ **Hall Status — All Channels**\n"]

    for platform_name, bindings_list in all_bindings.items():
        if not isinstance(bindings_list, list):
            continue
        lines.append(f"**{platform_name.upper()}**:")
        for entry in bindings_list:
            if not isinstance(entry, dict):
                continue
            soul = entry.get("soul", "?")
            chan_id = entry.get("id", "?")
            model = entry.get("model", "default")
            scope = entry.get("memory_scope", "default")

            # Count unread
            try:
                unread = hall_read(soul, mark_read=False)
                unread_count = len(unread) if unread else 0
            except Exception:
                unread_count = "?"

            unread_icon = "🔴" if isinstance(unread_count, int) and unread_count > 0 else "🟢"
            lines.append(
                f"  {unread_icon} {soul} (ch:{chan_id}, model:{model}, scope:{scope}) — {unread_count} unread"
            )
        lines.append("")

    return "\n".join(lines)


async def handle_hall_report(runner: Any, event: Any) -> str:
    """Send a status report from the current soul to its manager."""
    from gateway.extensions.channel_binding import _session_soul_names, _ensure_binding_loaded, _get_all_platform_bindings

    args = event.get_command_args().strip()
    session_key = runner._session_key_for_source(event.source)
    _ensure_binding_loaded(session_key)
    from_soul = _session_soul_names.get(session_key, "")

    if not from_soul:
        return "No soul bound to this channel. Use /bind first."

    # Find manager soul: look for pm, alpha, manager, operation in bindings
    all_bindings = _get_all_platform_bindings()
    manager_soul = None
    manager_priority = ["pm", "alpha", "manager", "operation", "coordinator"]

    for _platform, bindings_list in (all_bindings or {}).items():
        if not isinstance(bindings_list, list):
            continue
        for entry in bindings_list:
            if not isinstance(entry, dict):
                continue
            soul_name = (entry.get("soul") or "").lower()
            for candidate in manager_priority:
                if soul_name == candidate and soul_name != from_soul.lower():
                    manager_soul = entry.get("soul")
                    break
            if manager_soul:
                break
        if manager_soul:
            break

    if not manager_soul:
        # Fallback: just send to "pm"
        manager_soul = "pm"

    # Build structured report
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # Check for any unread messages (pending tasks from manager)
    unread = hall_read(from_soul, mark_read=False)
    unread_count = len(unread) if unread else 0

    subject = f"Status Report — {from_soul} @ {now}"
    body_lines = [
        f"**From:** {from_soul}",
        f"**Time:** {now}",
        f"**Pending Inbox:** {unread_count} unread message(s)",
    ]
    if args:
        body_lines.append(f"**Status:** {args}")
    else:
        body_lines.append("**Status:** (no message provided — routine check-in)")
    body = "\n".join(body_lines)

    try:
        entry = hall_send(from_soul=from_soul, to_soul=manager_soul, subject=subject, body=body, priority="normal", dispatch="queued")
        return (
            f"📋 Report sent!\n"
            f"From: {from_soul} → To: {manager_soul}\n"
            f"Subject: {subject}\n"
            f"ID: {entry.get('id', '?')}"
        )
    except Exception as e:
        return f"Failed to send report: {e}"


# ---------------------------------------------------------------------------
# Register hooks
# ---------------------------------------------------------------------------

register_hook("get_ephemeral", _get_ephemeral)
register_hook("on_session_cleanup", _on_session_cleanup)
