"""
Hall Tool — Inter-soul messaging via the shared Hall board.
===========================================================

Provides the `hall` tool for agents to send and read cross-channel messages.

Actions:
    send       — post a message to another soul (or "all")
    read       — read unread messages addressed to this soul
    list       — list recent messages (with optional filter)
    mark_read  — mark a specific message as read
    clear      — purge old messages

Requires the `hall` extension (gateway/extensions/hall.py) to be loaded.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


HALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "hall",
        "description": (
            "Inter-soul messaging board for cross-channel communication.\n"
            "Actions:\n"
            "- send: Post a message to another soul or broadcast 'all'. Requires: to, subject, body.\n"
            "- read: Read unread messages for the current soul. Auto-marks as read.\n"
            "- list: Show recent messages. Optional: soul (filter), limit.\n"
            "- mark_read: Mark a specific message as read. Requires: msg_id.\n"
            "- clear: Purge old messages. Optional: older_than_days.\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["send", "read", "list", "mark_read", "clear"],
                    "description": "The action to perform.",
                },
                "to": {
                    "type": "string",
                    "description": "Recipient soul name for 'send' (e.g. 'dev', 'pm') or 'all' for broadcast.",
                },
                "subject": {
                    "type": "string",
                    "description": "Subject line for 'send'.",
                },
                "body": {
                    "type": "string",
                    "description": "Message body for 'send'.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["normal", "high"],
                    "description": "Message priority. Default: normal.",
                },
                "soul": {
                    "type": "string",
                    "description": "Soul name filter for 'list'. Shows messages TO this soul.",
                },
                "msg_id": {
                    "type": "string",
                    "description": "Message ID for 'mark_read'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages for 'list'. Default: 20.",
                },
                "older_than_days": {
                    "type": "integer",
                    "description": "Days threshold for 'clear'. Default: 30.",
                },
            },
            "required": ["action"],
        },
    },
}


def _get_current_soul(**kwargs) -> Optional[str]:
    """Try to determine the current soul from context."""
    # Priority 1: explicitly passed
    if kwargs.get("soul"):
        return kwargs["soul"]
    # Priority 2: from session context (gateway provides this)
    session_key = kwargs.get("session_key") or kwargs.get("session_id")
    if session_key:
        try:
            from gateway.extensions.hall import _get_soul_name
            name = _get_soul_name(session_key)
            if name:
                return name
        except (ImportError, Exception):
            pass
    # Priority 3: from channel_binding module state
    try:
        from gateway.extensions.channel_binding import _session_soul_names
        # Try to find any active soul for this context
        if session_key and session_key in _session_soul_names:
            return _session_soul_names[session_key]
    except (ImportError, Exception):
        pass
    return None


def hall_tool(args: dict, **kwargs) -> str:
    """Handle the hall tool invocation."""
    try:
        from gateway.extensions.hall import (
            hall_send,
            hall_read,
            hall_list,
            hall_mark_read,
            hall_clear,
        )
    except ImportError:
        return "❌ Hall extension not loaded. Ensure gateway/extensions/hall.py exists."

    action = args.get("action", "")

    if action == "send":
        to = args.get("to", "")
        subject = args.get("subject", "")
        body = args.get("body", "")
        priority = args.get("priority", "normal")
        from_soul = _get_current_soul(**kwargs)

        if not from_soul:
            return "❌ Cannot determine your soul name. Provide 'soul' parameter."
        if not to:
            return "❌ 'to' is required for send. Use a soul name (e.g. 'dev', 'pm') or 'all'."
        if not body:
            return "❌ 'body' is required for send."

        try:
            entry = hall_send(from_soul, to, subject or "(no subject)", body, priority)
            return f"✅ Message sent: {from_soul} → {to}\n   Subject: {entry['subject']}\n   ID: {entry['id']}"
        except Exception as e:
            return f"❌ Failed to send: {e}"

    elif action == "read":
        soul = _get_current_soul(**kwargs)
        if not soul:
            return "❌ Cannot determine your soul name."
        messages = hall_read(soul, mark_read=True)
        if not messages:
            return "📭 No unread messages."
        lines = [f"📬 {len(messages)} unread message(s):\n"]
        for msg in messages:
            badge = "🔴 " if msg.get("priority") == "high" else ""
            lines.append(f"  {badge}[{msg['id']}] From: {msg['from']} — {msg.get('subject', '')}")
            lines.append(f"      {msg.get('body', '')}")
            lines.append(f"      _{msg.get('ts', '')}_\n")
        return "\n".join(lines)

    elif action == "list":
        soul = args.get("soul") or _get_current_soul(**kwargs)
        limit = args.get("limit", 20)
        messages = hall_list(soul=soul, limit=limit)
        if not messages:
            return "📭 No messages found."
        lines = [f"📋 Recent messages ({len(messages)}):\n"]
        for msg in messages:
            read_mark = "✓" if (soul and soul in msg.get("read_by", [])) else "●"
            badge = "🔴" if msg.get("priority") == "high" else ""
            lines.append(
                f"  {read_mark} [{msg['id']}] {badge} {msg['from']} → {msg['to']}: "
                f"{msg.get('subject', '')} _({msg.get('ts', '')})_"
            )
        return "\n".join(lines)

    elif action == "mark_read":
        msg_id = args.get("msg_id", "")
        soul = _get_current_soul(**kwargs)
        if not msg_id:
            return "❌ 'msg_id' is required for mark_read."
        if not soul:
            return "❌ Cannot determine your soul name."
        if hall_mark_read(msg_id, soul):
            return f"✅ Message {msg_id} marked as read by {soul}."
        return f"⚠️ Message {msg_id} not found or already read."

    elif action == "clear":
        soul = args.get("soul")
        days = args.get("older_than_days", 30)
        removed = hall_clear(soul=soul, older_than_days=days)
        return f"🗑️ Purged {removed} old message(s)."

    else:
        return f"❌ Unknown action: {action}. Use: send, read, list, mark_read, clear."


# --- Registry ---

from tools.registry import registry

registry.register(
    name="hall",
    toolset="memory",
    schema=HALL_SCHEMA,
    handler=lambda args, **kw: hall_tool(args, **kw),
    emoji="📬",
)
