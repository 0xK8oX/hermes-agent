"""Tests for gateway/extensions/cross_channel.py — inter-agent dispatch.

Covers:
1. dispatch_cross_channel() — gateway runner not active → graceful error
2. dispatch_cross_channel() — unknown platform → error
3. dispatch_cross_channel() — no adapter for platform → error
4. dispatch_cross_channel() — happy path: injects synthetic MessageEvent
5. inject_cross_channel_message() — synthetic event has correct fields
6. inject_cross_channel_message() — adapter.handle_message called
7. send_message_tool integration — trigger_agent=true fires dispatch
8. send_message_tool integration — trigger_agent=false (default) skips dispatch
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SessionSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_async_immediately(coro):
    """Run an async coroutine synchronously (for tests without event loop)."""
    return asyncio.run(coro)


def _make_runner(adapters=None, inject_side_effect=None):
    """Build a minimal mock GatewayRunner."""
    runner = MagicMock()
    runner.adapters = adapters or {}
    runner.inject_cross_channel_message = AsyncMock(
        side_effect=inject_side_effect or {"success": True, "triggered_agent": True}
    )
    return runner


def _make_send_config(platform_name="discord", chat_id="123456789"):
    """Build a minimal gateway config for send_message tests."""
    cfg = SimpleNamespace(
        platforms={
            Platform.DISCORD: SimpleNamespace(enabled=True, token="fake-token", extra={}),
        },
        get_home_channel=lambda _p: SimpleNamespace(chat_id=chat_id),
    )
    return cfg


# ---------------------------------------------------------------------------
# 1. dispatch_cross_channel — gateway runner not active
# ---------------------------------------------------------------------------

class TestDispatchCrossChannelNoRunner:
    def test_returns_error_when_runner_is_none(self):
        """dispatch_cross_channel returns error when no gateway runner."""
        with patch("gateway.run.get_gateway_runner", return_value=None):
            from gateway.extensions.cross_channel import dispatch_cross_channel
            result = json.loads(
                dispatch_cross_channel(
                    platform="discord",
                    chat_id="123",
                    text="hello",
                )
            )
        assert "error" in result
        assert "not active" in result["error"].lower() or "gateway" in result["error"].lower()


# ---------------------------------------------------------------------------
# 2. dispatch_cross_channel — import error
# ---------------------------------------------------------------------------

class TestDispatchCrossChannelImportError:
    def test_returns_error_when_import_fails(self):
        """dispatch_cross_channel handles ImportError gracefully."""
        with patch("gateway.run.get_gateway_runner",
                   side_effect=ImportError("no module")):
            from gateway.extensions.cross_channel import dispatch_cross_channel
            result = json.loads(
                dispatch_cross_channel(
                    platform="discord",
                    chat_id="123",
                    text="hello",
                )
            )
        assert "error" in result


# ---------------------------------------------------------------------------
# 3. dispatch_cross_channel — happy path
# ---------------------------------------------------------------------------

class TestDispatchCrossChannelHappyPath:
    def test_calls_inject_with_correct_args(self):
        """dispatch_cross_channel passes all args to inject_cross_channel_message."""
        runner = _make_runner()
        mock_inject = AsyncMock(return_value={
            "success": True,
            "platform": "discord",
            "chat_id": "999",
            "triggered_agent": True,
        })

        with patch("gateway.run.get_gateway_runner", return_value=runner), \
             patch("gateway.extensions.cross_channel.inject_cross_channel_message", mock_inject), \
             patch("model_tools._run_async", side_effect=_run_async_immediately):
            from gateway.extensions.cross_channel import dispatch_cross_channel
            result = json.loads(
                dispatch_cross_channel(
                    platform="discord",
                    chat_id="999",
                    text="Research this topic",
                    source_session_key="agent:main:discord:dev_thread",
                    source_user_name="DevAgent",
                    thread_id="555",
                )
            )

        assert result["success"] is True
        assert result["triggered_agent"] is True
        mock_inject.assert_awaited_once_with(
            adapters=runner.adapters,
            target_platform="discord",
            target_chat_id="999",
            text="Research this topic",
            source_session_key="agent:main:discord:dev_thread",
            source_user_name="DevAgent",
            thread_id="555",
        )

    def test_returns_error_on_exception(self):
        """dispatch_cross_channel returns error dict on unexpected exception."""
        runner = _make_runner()
        mock_inject = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("gateway.run.get_gateway_runner", return_value=runner), \
             patch("gateway.extensions.cross_channel.inject_cross_channel_message", mock_inject), \
             patch("model_tools._run_async", side_effect=_run_async_immediately):
            from gateway.extensions.cross_channel import dispatch_cross_channel
            result = json.loads(
                dispatch_cross_channel(
                    platform="discord",
                    chat_id="999",
                    text="hello",
                )
            )

        assert "error" in result
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# 4. inject_cross_channel_message — event construction
# ---------------------------------------------------------------------------

class TestInjectCrossChannelMessage:
    def setup_method(self):
        from gateway.extensions.cross_channel import reset_rate_limit
        reset_rate_limit()

    def _make_real_runner(self):
        """Create a GatewayRunner via __new__ and minimal init."""
        from gateway.run import GatewayRunner
        runner = GatewayRunner.__new__(GatewayRunner)
        return runner

    def test_builds_correct_synthetic_event(self):
        """inject_cross_channel_message creates a proper internal MessageEvent."""
        runner = self._make_real_runner()
        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)
        runner.adapters = {Platform.DISCORD: mock_adapter}

        result = asyncio.run(
            runner.inject_cross_channel_message(
                target_platform="discord",
                target_chat_id="123456789",
                text="Analyze this code",
                source_session_key="agent:main:discord:dev",
                source_user_name="DevBot",
                thread_id="thread_42",
            )
        )

        assert result["success"] is True
        assert result["platform"] == "discord"
        assert result["chat_id"] == "123456789"
        assert result["triggered_agent"] is True

        # Verify adapter.handle_message was called with the right event
        mock_adapter.handle_message.assert_awaited_once()
        event = mock_adapter.handle_message.call_args[0][0]

        assert isinstance(event, MessageEvent)
        assert event.text == "Analyze this code"
        assert event.message_type == MessageType.TEXT
        assert event.internal is True
        assert event.source.chat_id == "123456789"
        assert event.source.user_name == "DevBot"
        assert event.source.user_id == "cross_channel"

    def test_unknown_platform_returns_error(self):
        """inject_cross_channel_message returns error for unknown platform."""
        runner = self._make_real_runner()
        runner.adapters = {}

        result = asyncio.run(
            runner.inject_cross_channel_message(
                target_platform="unknown_platform",
                target_chat_id="123",
                text="hello",
            )
        )

        assert "error" in result
        assert "unknown_platform" in result["error"].lower() or "Unknown" in result["error"]

    def test_no_adapter_returns_error(self):
        """inject_cross_channel_message returns error when no adapter for platform."""
        runner = self._make_real_runner()
        runner.adapters = {}  # No discord adapter

        result = asyncio.run(
            runner.inject_cross_channel_message(
                target_platform="discord",
                target_chat_id="123",
                text="hello",
            )
        )

        assert "error" in result
        assert "adapter" in result["error"].lower()

    def test_thread_id_passed_to_source(self):
        """inject_cross_channel_message passes thread_id to SessionSource."""
        runner = self._make_real_runner()
        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)
        runner.adapters = {Platform.DISCORD: mock_adapter}

        asyncio.run(
            runner.inject_cross_channel_message(
                target_platform="discord",
                target_chat_id="123",
                text="hello",
                thread_id="thread_99",
            )
        )

        event = mock_adapter.handle_message.call_args[0][0]
        assert event.source.thread_id == "thread_99"

    def test_empty_thread_id_defaults_to_empty_string(self):
        """inject_cross_channel_message handles None thread_id."""
        runner = self._make_real_runner()
        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)
        runner.adapters = {Platform.DISCORD: mock_adapter}

        asyncio.run(
            runner.inject_cross_channel_message(
                target_platform="discord",
                target_chat_id="123",
                text="hello",
                thread_id=None,
            )
        )

        event = mock_adapter.handle_message.call_args[0][0]
        assert event.source.thread_id == ""

    def test_exception_in_adapter_returns_error(self):
        """inject_cross_channel_message catches adapter exceptions."""
        runner = self._make_real_runner()
        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(side_effect=RuntimeError("adapter crashed"))
        runner.adapters = {Platform.DISCORD: mock_adapter}

        result = asyncio.run(
            runner.inject_cross_channel_message(
                target_platform="discord",
                target_chat_id="123",
                text="hello",
            )
        )

        assert "error" in result
        assert "adapter crashed" in result["error"]

    def test_telegram_platform_injection(self):
        """inject_cross_channel_message works with Telegram platform."""
        runner = self._make_real_runner()
        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        result = asyncio.run(
            runner.inject_cross_channel_message(
                target_platform="telegram",
                target_chat_id="-1001234567890",
                text="Research topic X",
                thread_id="17585",
            )
        )

        assert result["success"] is True
        assert result["platform"] == "telegram"
        event = mock_adapter.handle_message.call_args[0][0]
        assert event.source.chat_id == "-1001234567890"
        assert event.source.thread_id == "17585"


# ---------------------------------------------------------------------------
# 5. send_message_tool — trigger_agent integration
# ---------------------------------------------------------------------------

class TestSendMessageTriggerAgent:
    def test_trigger_agent_true_fires_dispatch(self):
        """send_message with trigger_agent=true calls dispatch_cross_channel."""
        config = _make_send_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform",
                   new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True), \
             patch("gateway.extensions.cross_channel.dispatch_cross_channel",
                   return_value=json.dumps({"success": True, "agent_triggered": True})) as dispatch_mock:

            from tools.send_message_tool import send_message_tool
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "discord",
                    "message": "Analyze this code for me",
                    "trigger_agent": True,
                })
            )

        assert result["success"] is True
        assert result.get("agent_triggered") is True
        dispatch_mock.assert_called_once()

        # Verify dispatch was called with correct platform/chat_id
        call_kwargs = dispatch_mock.call_args
        assert call_kwargs.kwargs.get("platform") == "discord" or \
               (call_kwargs[1] if len(call_kwargs) > 1 else {}).get("platform") == "discord"

    def test_trigger_agent_false_skips_dispatch(self):
        """send_message with trigger_agent=false does NOT call dispatch."""
        config = _make_send_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform",
                   new=AsyncMock(return_value={"success": True})), \
             patch("gateway.mirror.mirror_to_session", return_value=True), \
             patch("gateway.extensions.cross_channel.dispatch_cross_channel",
                   return_value=json.dumps({"success": True})) as dispatch_mock:

            from tools.send_message_tool import send_message_tool
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "discord",
                    "message": "Just a regular message",
                })
            )

        assert result["success"] is True
        assert "agent_triggered" not in result
        dispatch_mock.assert_not_called()

    def test_trigger_agent_send_failure_no_dispatch(self):
        """send_message with trigger_agent=true but send failure → no dispatch."""
        config = _make_send_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform",
                   new=AsyncMock(return_value={"error": "send failed"})), \
             patch("gateway.mirror.mirror_to_session", return_value=True), \
             patch("gateway.extensions.cross_channel.dispatch_cross_channel",
                   return_value=json.dumps({"success": True})) as dispatch_mock:

            from tools.send_message_tool import send_message_tool
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "discord",
                    "message": "hello",
                    "trigger_agent": True,
                })
            )

        assert "error" in result
        dispatch_mock.assert_not_called()

    def test_dispatch_failure_still_returns_send_success(self):
        """send_message succeeds even if cross-channel dispatch fails."""
        config = _make_send_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform",
                   new=AsyncMock(return_value={"success": True})), \
             patch("gateway.mirror.mirror_to_session", return_value=True), \
             patch("gateway.extensions.cross_channel.dispatch_cross_channel",
                   return_value=json.dumps({"error": "Gateway not running"})):

            from tools.send_message_tool import send_message_tool
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "discord",
                    "message": "hello",
                    "trigger_agent": True,
                })
            )

        # Message was still sent successfully
        assert result["success"] is True
        # Dispatch failed gracefully
        assert result.get("agent_triggered") is False
        assert "agent_error" in result

    def test_dispatch_exception_handled_gracefully(self):
        """send_message handles unexpected dispatch exceptions."""
        config = _make_send_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform",
                   new=AsyncMock(return_value={"success": True})), \
             patch("gateway.mirror.mirror_to_session", return_value=True), \
             patch("gateway.extensions.cross_channel.dispatch_cross_channel",
                   side_effect=ImportError("no module")):

            from tools.send_message_tool import send_message_tool
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "discord",
                    "message": "hello",
                    "trigger_agent": True,
                })
            )

        assert result["success"] is True
        assert result.get("agent_triggered") is False
        assert "agent_error" in result


# ---------------------------------------------------------------------------
# 6. Recursion guard (C5)
# ---------------------------------------------------------------------------

class TestRecursionGuard:
    def setup_method(self):
        from gateway.extensions.cross_channel import _tls
        _tls.dispatch_depth = 0

    @pytest.mark.asyncio
    async def test_blocks_at_depth_3(self):
        from gateway.extensions.cross_channel import inject_cross_channel_message
        import gateway.extensions.cross_channel as cc
        cc._tls.dispatch_depth = 3

        result = await inject_cross_channel_message(
            adapters={},
            target_platform="discord",
            target_chat_id="123",
            text="hello",
        )
        assert "error" in result
        assert "recursion" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_increments_and_restores_depth(self):
        from gateway.extensions.cross_channel import inject_cross_channel_message
        import gateway.extensions.cross_channel as cc
        from gateway.config import Platform
        cc._tls.dispatch_depth = 0

        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)

        result = await inject_cross_channel_message(
            adapters={Platform.DISCORD: mock_adapter},
            target_platform="discord",
            target_chat_id="123",
            text="hello",
        )
        # After execution, depth should be restored to 0
        assert cc._tls.dispatch_depth == 0


# ---------------------------------------------------------------------------
# 7. Rate limiting (L6)
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def setup_method(self):
        from gateway.extensions.cross_channel import reset_rate_limit
        reset_rate_limit()

    @pytest.mark.asyncio
    async def test_rate_limits_same_target(self):
        from gateway.extensions.cross_channel import inject_cross_channel_message
        from gateway.config import Platform

        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)

        result1 = await inject_cross_channel_message(
            adapters={Platform.DISCORD: mock_adapter},
            target_platform="discord",
            target_chat_id="123",
            text="first",
        )
        assert result1.get("success") is True

        # Immediate second dispatch to same target should be rate limited
        result2 = await inject_cross_channel_message(
            adapters={Platform.DISCORD: mock_adapter},
            target_platform="discord",
            target_chat_id="123",
            text="second",
        )
        assert "error" in result2
        assert "rate limited" in result2["error"].lower()

    @pytest.mark.asyncio
    async def test_different_targets_not_rate_limited(self):
        from gateway.extensions.cross_channel import inject_cross_channel_message
        from gateway.config import Platform

        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)

        result1 = await inject_cross_channel_message(
            adapters={Platform.DISCORD: mock_adapter},
            target_platform="discord",
            target_chat_id="123",
            text="first",
        )
        assert result1.get("success") is True

        # Different chat_id should not be rate limited
        result2 = await inject_cross_channel_message(
            adapters={Platform.DISCORD: mock_adapter},
            target_platform="discord",
            target_chat_id="456",
            text="second",
        )
        assert result2.get("success") is True


# ---------------------------------------------------------------------------
# 8. Auth check (H5)
# ---------------------------------------------------------------------------

class TestAuthCheck:
    def setup_method(self):
        from gateway.extensions.cross_channel import reset_rate_limit
        reset_rate_limit()

    @pytest.mark.asyncio
    async def test_allows_when_no_source_platform(self):
        """No source_platform/source_chat_id → skips auth check entirely."""
        from gateway.extensions.cross_channel import inject_cross_channel_message
        from gateway.config import Platform

        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)

        result = await inject_cross_channel_message(
            adapters={Platform.DISCORD: mock_adapter},
            target_platform="discord",
            target_chat_id="123",
            text="hello",
        )
        assert result.get("success") is True

    @pytest.mark.asyncio
    async def test_warns_when_unbound_source(self):
        """Source channel not bound → logs warning but still dispatches."""
        from gateway.extensions.cross_channel import inject_cross_channel_message
        from gateway.config import Platform

        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)

        with patch("gateway.extensions.channel_binding.is_channel_bound", return_value=False):
            result = await inject_cross_channel_message(
                adapters={Platform.DISCORD: mock_adapter},
                target_platform="discord",
                target_chat_id="123",
                text="hello",
                source_platform="telegram",
                source_chat_id="999",
            )
        assert result.get("success") is True

    @pytest.mark.asyncio
    async def test_allows_when_bound_source(self):
        """Source channel bound → dispatches normally."""
        from gateway.extensions.cross_channel import inject_cross_channel_message
        from gateway.config import Platform

        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)

        with patch("gateway.extensions.channel_binding.is_channel_bound", return_value=True):
            result = await inject_cross_channel_message(
                adapters={Platform.DISCORD: mock_adapter},
                target_platform="discord",
                target_chat_id="123",
                text="hello",
                source_platform="telegram",
                source_chat_id="999",
            )
        assert result.get("success") is True

    @pytest.mark.asyncio
    async def test_survives_auth_check_exception(self):
        """Auth check raises → logs debug and continues."""
        from gateway.extensions.cross_channel import inject_cross_channel_message
        from gateway.config import Platform

        mock_adapter = MagicMock()
        mock_adapter.handle_message = AsyncMock(return_value=None)

        with patch("gateway.extensions.channel_binding.is_channel_bound", side_effect=RuntimeError("boom")):
            result = await inject_cross_channel_message(
                adapters={Platform.DISCORD: mock_adapter},
                target_platform="discord",
                target_chat_id="123",
                text="hello",
                source_platform="telegram",
                source_chat_id="999",
            )
        assert result.get("success") is True


# ---------------------------------------------------------------------------
# 9. Session context propagation
# ---------------------------------------------------------------------------

class TestSessionContextPropagation:
    def test_dispatch_receives_source_session_key(self):
        """send_message passes session context to dispatch_cross_channel."""
        config = _make_send_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform",
                   new=AsyncMock(return_value={"success": True})), \
             patch("gateway.mirror.mirror_to_session", return_value=True), \
             patch("gateway.session_context.get_session_env",
                   side_effect=lambda k, d="": {
                       "HERMES_SESSION_KEY": "agent:main:discord:thread_123",
                       "HERMES_SESSION_USER_NAME": "DevAgent",
                       "HERMES_SESSION_PLATFORM": "discord",
                   }.get(k, d)), \
             patch("gateway.extensions.cross_channel.dispatch_cross_channel",
                   return_value=json.dumps({"success": True})) as dispatch_mock:

            from tools.send_message_tool import send_message_tool
            json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "discord",
                    "message": "hello",
                    "trigger_agent": True,
                })
            )

        # Verify source_session_key was passed
        call = dispatch_mock.call_args
        assert call.kwargs.get("source_session_key") == "agent:main:discord:thread_123" or \
               (len(call) > 1 and call[1].get("source_session_key") == "agent:main:discord:thread_123")
