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
from typing import Dict, Optional
from gateway.platforms.base import MessageEvent, MessageType, SessionSource

logger = logging.getLogger(__name__)


async def inject_cross_channel_message(
    adapters: dict,
    target_platform: str,
    target_chat_id: str,
    text: str,
    source_session_key: str = "",
    source_user_name: str = "System",
    thread_id: str = None,
) -> dict:
    """Inject a synthetic message into a target channel's agent pipeline.

    Used when ``trigger_agent=true`` in cross-channel dispatch.
    Creates a synthetic MessageEvent with ``internal=True`` (bypasses auth)
    and feeds it through the target channel's adapter into the full
    message-processing pipeline — so the target agent processes it with
    its own soul, model, and memory scope.

    Args:
        adapters: Platform adapter dict from the gateway runner.
        target_platform: Platform name string (e.g. "discord").
        target_chat_id: Target channel/chat ID (numeric string).
        text: The message text to inject.
        source_session_key: The originating session key (for mirror-back).
        source_user_name: Display name for the synthetic event source.
        thread_id: Optional thread ID within the target channel.

    Returns:
        Dict with ``success`` and details, or ``error`` on failure.
    """
    from gateway.config import Platform

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
            extra={
                "cross_channel": True,
                "source_session_key": source_session_key,
            },
        )

        logger.info(
            "[CrossChannel] Injecting into %s:%s (thread=%s) from %s",
            target_platform, target_chat_id, thread_id, source_session_key,
        )
        await adapter.handle_message(event)
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
