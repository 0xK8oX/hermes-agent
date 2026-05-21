"""
Cross-Channel Extension — Inter-Agent Dispatch
================================================

Enables agents on one channel to send messages to another channel **and trigger
the target channel's agent** to process and respond.

The send_message tool gains a ``trigger_agent`` parameter.  When set to true,
after the message is delivered to the target Discord/Telegram channel (visible
to the user), a synthetic ``MessageEvent(internal=True)`` is injected into the
target channel's adapter pipeline.  The target agent then processes it with its
own soul, model, and memory scope.

Flow::

    Dev agent → send_message(trigger_agent=true, target="discord:#research")
        → Message posted to #research (user-visible)
        → inject_cross_channel_message() creates synthetic MessageEvent
        → Target agent processes with its own soul/model/memory
        → Response posted to #research (visible to user)
        → Response mirrored back into source agent's session transcript

Public API (called from send_message_tool):
    - ``dispatch_cross_channel(platform, chat_id, text, source_session_key,
       source_user_name, thread_id)`` → dict

No hooks are registered — this is a callable API, not a pipeline hook.
The gateway runner reference is obtained via ``gateway.run.get_gateway_runner()``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Dict, Optional
from gateway.platforms.base import MessageEvent, MessageType, SessionSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Recursion guard (C5) — thread-local dispatch depth counter
# ---------------------------------------------------------------------------

_tls = threading.local()

# ---------------------------------------------------------------------------
# Rate limiting (L6) — per-target minimum interval
# ---------------------------------------------------------------------------

_last_dispatch_time: Dict[tuple, float] = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_INTERVAL = 2.0  # seconds


def reset_rate_limit():
    """Clear rate-limit state — for use in tests."""
    with _rate_limit_lock:
        _last_dispatch_time.clear()


async def inject_cross_channel_message(
    adapters: dict,
    target_platform: str,
    target_chat_id: str,
    text: str,
    source_session_key: str = "",
    source_user_name: str = "System",
    thread_id: str = None,
    source_platform: Optional[str] = None,
    source_chat_id: Optional[str] = None,
) -> dict:
    """Inject a synthetic message into a target channel's agent pipeline.

    Used when ``trigger_agent=true`` in cross-channel dispatch.
    Creates a synthetic MessageEvent with ``internal=True`` (bypasses auth)
    and feeds it through the target channel's adapter into the full
    message-processing pipeline — so the target agent processes it with its
    own soul, model, and memory scope.

    Args:
        adapters: Platform adapter dict from the gateway runner.
        target_platform: Platform name string (e.g. "discord").
        target_chat_id: Target channel/chat ID (numeric string).
        text: The message text to inject.
        source_session_key: The originating session key (for mirror-back).
        source_user_name: Display name for the synthetic event source.
        thread_id: Optional thread ID within the target channel.
        source_platform: Originating platform (for auth check).
        source_chat_id: Originating chat ID (for auth check).

    Returns:
        Dict with ``success`` and details, or ``error`` on failure.
    """
    from gateway.config import Platform

    # ── C5: Recursion guard ──────────────────────────────────────────────
    depth = getattr(_tls, "dispatch_depth", 0)
    if depth >= 3:
        logger.warning(
            "[CrossChannel] Recursion depth %d >= 3 — breaking dispatch cycle "
            "for %s:%s from %s",
            depth, target_platform, target_chat_id, source_session_key,
        )
        return {"error": "Cross-channel dispatch recursion limit reached"}

    # ── L6: Rate limiting ────────────────────────────────────────────────
    target_key = (target_platform, str(target_chat_id))
    now = time.monotonic()
    with _rate_limit_lock:
        last = _last_dispatch_time.get(target_key)
        if last is not None and (now - last) < _RATE_LIMIT_INTERVAL:
            remaining = round(_RATE_LIMIT_INTERVAL - (now - last), 1)
            logger.warning(
                "[CrossChannel] Rate limited: %.1fs remaining for %s:%s",
                remaining, target_platform, target_chat_id,
            )
            return {"error": f"Rate limited — wait {remaining:.1f}s before dispatching to this channel again"}
        _last_dispatch_time[target_key] = now

    # ── H5: Soft authorization check ─────────────────────────────────────
    if source_platform and source_chat_id:
        try:
            from gateway.extensions.channel_binding import is_channel_bound
            if not is_channel_bound(source_platform, str(source_chat_id)):
                logger.warning(
                    "[CrossChannel] Source %s:%s has no bound soul — "
                    "dispatch may be unauthorized (allowing for CLI/cron compat)",
                    source_platform, source_chat_id,
                )
        except Exception:
            logger.debug("[CrossChannel] Auth check failed, continuing", exc_info=True)

    # Resolve platform enum
    platform_map = {p.value: p for p in Platform}
    platform = platform_map.get(target_platform)
    if not platform:
        return {"error": f"Unknown platform: {target_platform}"}

    adapter = adapters.get(platform)
    if not adapter:
        return {"error": f"No adapter connected for {target_platform}"}

    try:
        # Build a synthetic SessionSource for the target channel
        source = SessionSource(
            platform=platform,
            chat_id=str(target_chat_id),
            chat_type="group",
            user_id="cross_channel",
            user_name=source_user_name,
            thread_id=thread_id or "",
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,  # Bypass user authorization
        )

        logger.info(
            "[CrossChannel] Injecting into %s:%s (thread=%s) from %s (depth=%d)",
            target_platform, target_chat_id, thread_id, source_session_key, depth,
        )

        # Increment recursion depth for nested dispatches
        _tls.dispatch_depth = depth + 1
        try:
            await adapter.handle_message(event)
        finally:
            _tls.dispatch_depth = depth

        return {
            "success": True,
            "platform": target_platform,
            "chat_id": target_chat_id,
            "triggered_agent": True,
        }
    except Exception as e:
        logger.error("[CrossChannel] Injection error: %s", e)
        return {"error": str(e)}


def dispatch_cross_channel(
    platform: str,
    chat_id: str,
    text: str,
    source_session_key: str = "",
    source_user_name: str = "System",
    thread_id: str = None,
) -> str:
    """Dispatch a cross-channel message and trigger the target agent.

    Called from the synchronous send_message_tool handler. Bridges to the
    async gateway via ``model_tools._run_async()``.

    Args:
        platform: Target platform name (e.g. "discord").
        chat_id: Target channel/chat ID.
        text: Message text to inject.
        source_session_key: Originating session key (for mirror-back).
        source_user_name: Display name for the synthetic event.
        thread_id: Optional thread ID within the target channel.

    Returns:
        JSON string with result dict.
    """
    try:
        from gateway.run import get_gateway_runner
        runner = get_gateway_runner()
    except ImportError:
        return json.dumps({"error": "Gateway module not available"})

    if not runner:
        return json.dumps({
            "error": "Gateway runner not active. Cross-channel dispatch requires a running gateway.",
        })

    try:
        from model_tools import _run_async
        result = _run_async(
            inject_cross_channel_message(
                adapters=runner.adapters,
                target_platform=platform,
                target_chat_id=chat_id,
                text=text,
                source_session_key=source_session_key,
                source_user_name=source_user_name,
                thread_id=thread_id,
            )
        )
        if isinstance(result, dict):
            return json.dumps(result)
        return json.dumps({"success": True, "raw": str(result)})
    except Exception as e:
        logger.error("[CrossChannel] Dispatch error: %s", e)
        return json.dumps({"error": f"Cross-channel dispatch failed: {e}"})
