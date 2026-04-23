"""Cross-channel dispatch integration test.

Starts a real GatewayRunner (via object.__new__ to skip __init__)
with mock adapters and tests the full inject_cross_channel_message
pipeline.  Uses the same pattern as tests/gateway/restart_test_helpers.py.
"""

import asyncio
import json
import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.extensions.cross_channel import reset_rate_limit
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SessionSource,
)
from gateway.run import GatewayRunner
from typing import List


# ---------------------------------------------------------------------------
# Auto-reset rate limiter to avoid cross-test state leakage
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_cross_channel_rate_limit():
    reset_rate_limit()
    yield


# ---------------------------------------------------------------------------
# Mock adapter that records received messages
# ---------------------------------------------------------------------------
class RecordingAdapter(BasePlatformAdapter):
    """Minimal adapter that records handle_message calls."""

    def __init__(self, platform: Platform):
        self._platform = platform
        self.received_events: List[MessageEvent] = []
        # BasePlatformAdapter needs a PlatformConfig
        super().__init__(PlatformConfig(enabled=True, token="test"), platform)

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="mock_123")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def handle_message(self, event: MessageEvent):
        """Record and acknowledge."""
        self.received_events.append(event)
        return {"success": True}

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "name": f"mock-{chat_id}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_runner_with_adapters():
    """Create a GatewayRunner with recording adapters, bypassing __init__."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(enabled=True, token="test-d"),
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="test-t"),
        }
    )
    runner._running = True
    runner._shutdown_event = asyncio.Event()

    discord_adapter = RecordingAdapter(Platform.DISCORD)
    telegram_adapter = RecordingAdapter(Platform.TELEGRAM)
    runner.adapters = {
        Platform.DISCORD: discord_adapter,
        Platform.TELEGRAM: telegram_adapter,
    }
    return runner, discord_adapter, telegram_adapter


# ---------------------------------------------------------------------------
# Integration tests — inject_cross_channel_message with real code
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_inject_creates_synthetic_event():
    """inject_cross_channel_message creates a proper internal MessageEvent."""
    runner, discord, _ = _make_runner_with_adapters()

    result = await runner.inject_cross_channel_message(
        target_platform="discord",
        target_chat_id="1234567890",
        text="Hello from cross-channel!",
        source_session_key="discord:111:222",
        source_user_name="DevAgent",
    )

    assert result["success"] is True
    assert result["triggered_agent"] is True
    assert len(discord.received_events) == 1

    event = discord.received_events[0]
    assert event.text == "Hello from cross-channel!"
    assert event.internal is True  # Bypasses auth
    assert event.source.chat_id == "1234567890"
    assert event.source.user_name == "DevAgent"
    assert event.source.user_id == "cross_channel"


@pytest.mark.asyncio
async def test_inject_targets_correct_adapter():
    """Messages go to the correct platform adapter."""
    runner, discord, telegram = _make_runner_with_adapters()

    await runner.inject_cross_channel_message(
        target_platform="telegram",
        target_chat_id="-1001234567890",
        text="Testing telegram dispatch",
    )

    assert len(telegram.received_events) == 1
    assert len(discord.received_events) == 0
    assert telegram.received_events[0].source.chat_id == "-1001234567890"


@pytest.mark.asyncio
async def test_inject_with_thread_id():
    """Thread ID is propagated to the synthetic event."""
    runner, discord, _ = _make_runner_with_adapters()

    await runner.inject_cross_channel_message(
        target_platform="discord",
        target_chat_id="111",
        text="Thread message",
        thread_id="222",
    )

    event = discord.received_events[0]
    assert event.source.thread_id == "222"


@pytest.mark.asyncio
async def test_inject_unknown_platform():
    """Unknown platform returns error."""
    runner, _, _ = _make_runner_with_adapters()

    result = await runner.inject_cross_channel_message(
        target_platform="slack",
        target_chat_id="C12345",
        text="Should fail",
    )
    assert "error" in result
    # slack is a valid Platform enum but has no adapter connected
    assert "No adapter" in result["error"] or "Unknown platform" in result["error"]


@pytest.mark.asyncio
async def test_inject_no_adapter():
    """When adapter for a valid platform is missing, returns error."""
    from gateway.extensions.cross_channel import reset_rate_limit
    reset_rate_limit()

    runner, discord, _ = _make_runner_with_adapters()
    # Remove discord adapter to simulate missing connection
    runner.adapters = {}

    result = await runner.inject_cross_channel_message(
        target_platform="discord",
        target_chat_id="123",
        text="No adapter",
    )
    assert "error" in result
    assert "No adapter" in result["error"]


# ---------------------------------------------------------------------------
# Full dispatch pipeline: dispatch_cross_channel → inject → handle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_full_cross_channel_pipeline():
    """Complete pipeline from dispatch_cross_channel through to adapter."""
    from gateway.extensions.cross_channel import dispatch_cross_channel

    runner, discord, _ = _make_runner_with_adapters()

    with patch("gateway.run.get_gateway_runner", return_value=runner):
        with patch("gateway.session_context.get_session_env") as mock_env:
            mock_env.side_effect = lambda key, default="": {
                "HERMES_SESSION_PLATFORM": "discord",
                "HERMES_SESSION_KEY": "discord:999:888",
                "HERMES_SESSION_USER_NAME": "DevAgent",
            }.get(key, default)

            result = dispatch_cross_channel(
                platform="discord",
                chat_id="1493257371349291150",
                text="🧪 E2E integration test: research agent, please confirm receipt",
                source_session_key="discord:999:888",
                source_user_name="DevAgent",
            )

    result_data = json.loads(result)
    assert result_data.get("success") is True
    assert result_data.get("triggered_agent") is True

    # Verify the message reached the discord adapter
    assert len(discord.received_events) == 1
    event = discord.received_events[0]
    assert event.internal is True
    assert "E2E integration test" in event.text
    assert event.source.chat_id == "1493257371349291150"


@pytest.mark.asyncio
async def test_cross_channel_to_telegram():
    """Cross-channel dispatch from Discord to Telegram."""
    from gateway.extensions.cross_channel import dispatch_cross_channel

    runner, _, telegram = _make_runner_with_adapters()

    with patch("gateway.run.get_gateway_runner", return_value=runner):
        result = dispatch_cross_channel(
            platform="telegram",
            chat_id="-1009998887770",
            text="Cross-platform dispatch test",
            source_session_key="discord:111:222",
            source_user_name="DiscordAgent",
        )

    result_data = json.loads(result)
    assert result_data.get("success") is True

    assert len(telegram.received_events) == 1
    assert telegram.received_events[0].text == "Cross-platform dispatch test"


# ---------------------------------------------------------------------------
# Multi-message stress test
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiple_cross_channel_dispatches():
    """Multiple rapid dispatches all reach the target adapter."""
    from gateway.extensions.cross_channel import dispatch_cross_channel, reset_rate_limit

    runner, discord, _ = _make_runner_with_adapters()

    with patch("gateway.run.get_gateway_runner", return_value=runner):
        for i in range(5):
            reset_rate_limit()  # Clear rate limit for rapid-fire test
            result = dispatch_cross_channel(
                platform="discord",
                chat_id="12345",
                text=f"Message {i}",
                source_session_key="discord:source",
            )
            assert json.loads(result).get("success") is True

    assert len(discord.received_events) == 5
    for i, event in enumerate(discord.received_events):
        assert event.text == f"Message {i}"


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dispatch_handles_adapter_exception():
    """If handle_message raises, inject_cross_channel_message returns error."""
    from gateway.extensions.cross_channel import reset_rate_limit
    reset_rate_limit()
    runner, discord, _ = _make_runner_with_adapters()

    # Make handle_message raise
    async def failing_handle(event):
        raise RuntimeError("Simulated adapter failure")

    discord.handle_message = failing_handle

    result = await runner.inject_cross_channel_message(
        target_platform="discord",
        target_chat_id="123",
        text="This should fail",
    )
    assert "error" in result
    assert "Simulated adapter failure" in result["error"]


# ---------------------------------------------------------------------------
# Cleanup webhook created during live E2E attempt
# ---------------------------------------------------------------------------
def test_cleanup_webhook():
    """Clean up the test webhook we created in #research."""
    import subprocess

    result = subprocess.run(
        ["python3", "-c",
         "import os; lines=open(os.path.expanduser('~/.hermes/.env')).readlines(); "
         "token=[l.strip().split('=',1)[1] for l in lines if l.startswith('DISCORD_BOT_TOKEN')]; "
         "print(token[0] if token else '')"],
        capture_output=True, text=True
    )
    bot_token = result.stdout.strip()
    if not bot_token:
        pytest.skip("No Discord token available")

    result = subprocess.run(
        ["curl", "-s", "-X", "DELETE",
         "-H", f"Authorization: Bot {bot_token}",
         "https://discord.com/api/v10/webhooks/1494150686013788171"],
        capture_output=True, text=True
    )
    print(f"Webhook cleanup: {result.stdout or 'deleted (204)'}")
