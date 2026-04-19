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
from typing import Optional

logger = logging.getLogger(__name__)


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
            runner.inject_cross_channel_message(
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
