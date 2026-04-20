"""Tests for admin-only command guard (GATEWAY_ADMIN_USERS)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    """Clear all auth-related env vars."""
    for key in (
        "TELEGRAM_ALLOWED_USERS", "DISCORD_ALLOWED_USERS",
        "WHATSAPP_ALLOWED_USERS", "SLACK_ALLOWED_USERS",
        "SIGNAL_ALLOWED_USERS", "EMAIL_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS", "DISCORD_ALLOW_ALL_USERS",
        "WHATSAPP_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ADMIN_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_source(platform: Platform, user_id: str, chat_id: str = "chat1",
                 chat_type: str = "group") -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="tester",
        chat_type=chat_type,
    )


def _make_event(text: str, platform: Platform, user_id: str,
                chat_id: str = "chat1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="m1",
        source=_make_source(platform, user_id, chat_id),
    )


def _make_runner(platform: Platform):
    """Create a minimal GatewayRunner for testing."""
    from gateway.run import GatewayRunner

    config = MagicMock(spec=GatewayConfig)
    config.platforms = {}
    config.get_unauthorized_dm_behavior = MagicMock(return_value="pair")
    config.extra = {}

    runner = object.__new__(GatewayRunner)
    runner.config = config
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {platform: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved = MagicMock(return_value=False)
    runner.pairing_store._is_rate_limited = MagicMock(return_value=False)
    runner.pairing_store.generate_code = MagicMock(return_value=None)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_messages = {}
    runner._draining = False
    runner._sessions = {}
    runner._session_key_cache = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner._agents_cache = {}
    runner._agents_cache_lock = MagicMock()
    runner._agents_cache_cv = MagicMock()
    runner.session_store = MagicMock()
    mock_session = MagicMock()
    mock_session.total_tokens = 0
    mock_session.message_count = 0
    from datetime import datetime
    mock_session.created_at = datetime(2026, 1, 1, 0, 0)
    mock_session.model = "test"
    mock_session.platform = "test"
    runner.session_store.get_or_create_session = MagicMock(return_value=mock_session)
    runner._get_agent_for_session = MagicMock(return_value=None)
    runner._release_agent = MagicMock()
    runner._session_db = None
 
    return runner


# ── Unit tests: _is_admin_user ──────────────────────────────────

class TestIsAdminUser:
    """Tests for GatewayRunner._is_admin_user()."""

    def test_no_admin_list_configured_no_one_is_admin(self, monkeypatch):
        """When GATEWAY_ADMIN_USERS is not set, no one is admin (fail-closed)."""
        _clear_auth_env(monkeypatch)
        runner = _make_runner(Platform.TELEGRAM)
        source = _make_source(Platform.TELEGRAM, "12345")
        assert runner._is_admin_user(source) is False

    def test_empty_admin_list_no_one_is_admin(self, monkeypatch):
        """When GATEWAY_ADMIN_USERS is empty string, no one is admin (fail-closed)."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "")
        runner = _make_runner(Platform.TELEGRAM)
        source = _make_source(Platform.TELEGRAM, "12345")
        assert runner._is_admin_user(source) is False

    def test_admin_list_user_match(self, monkeypatch):
        """User ID in GATEWAY_ADMIN_USERS is recognized as admin."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "12345,67890")
        runner = _make_runner(Platform.TELEGRAM)

        assert runner._is_admin_user(_make_source(Platform.TELEGRAM, "12345")) is True
        assert runner._is_admin_user(_make_source(Platform.TELEGRAM, "67890")) is True

    def test_admin_list_user_not_in_list(self, monkeypatch):
        """User ID not in GATEWAY_ADMIN_USERS is not admin."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "12345,67890")
        runner = _make_runner(Platform.TELEGRAM)

        assert runner._is_admin_user(_make_source(Platform.TELEGRAM, "99999")) is False

    def test_admin_list_with_bare_id_match(self, monkeypatch):
        """User with email-style ID matches via bare-ID extraction."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "12345")
        runner = _make_runner(Platform.DISCORD)

        # user_id="12345@something" should match admin "12345"
        source = _make_source(Platform.DISCORD, "12345@something")
        assert runner._is_admin_user(source) is True

    def test_none_user_id_is_not_admin(self, monkeypatch):
        """User with None user_id is not admin."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "12345")
        runner = _make_runner(Platform.TELEGRAM)

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id=None,
            chat_id="chat1",
            user_name="ghost",
            chat_type="group",
        )
        assert runner._is_admin_user(source) is False


# ── Integration tests: admin command guard in _handle_message ──

class TestAdminCommandGuard:
    """Tests for the admin command guard in _handle_message dispatch."""

    @pytest.mark.asyncio
    async def test_bind_rejected_for_non_admin(self, monkeypatch):
        """Non-admin user cannot run /bind."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "admin1")
        monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")

        runner = _make_runner(Platform.WHATSAPP)
        event = _make_event("/bind dev", Platform.WHATSAPP, "random_user", "group1")

        result = await runner._handle_message(event)
        assert "admin-only" in result.lower()

    @pytest.mark.asyncio
    async def test_bind_allowed_for_admin(self, monkeypatch):
        """Admin user can run /bind."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "admin1")
        monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")

        runner = _make_runner(Platform.WHATSAPP)
        event = _make_event("/bind dev", Platform.WHATSAPP, "admin1", "group1")

        result = await runner._handle_message(event)
        # Should NOT return admin-only rejection.
        # The actual response depends on whether soul "dev" exists.
        # We just check it's NOT the admin rejection message.
        assert "admin-only" not in (result or "").lower()

    @pytest.mark.asyncio
    async def test_model_rejected_for_non_admin(self, monkeypatch):
        """Non-admin user cannot run /model."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "admin1")
        monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")

        runner = _make_runner(Platform.WHATSAPP)
        event = _make_event("/model gpt-4", Platform.WHATSAPP, "random_user", "group1")

        result = await runner._handle_message(event)
        assert "admin-only" in result.lower()

    @pytest.mark.asyncio
    async def test_help_allowed_for_non_admin(self, monkeypatch):
        """Non-admin user CAN run /help (not in admin commands list)."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "admin1")
        monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")

        runner = _make_runner(Platform.WHATSAPP)
        event = _make_event("/help", Platform.WHATSAPP, "random_user", "group1")

        result = await runner._handle_message(event)
        assert "admin-only" not in (result or "").lower()

    @pytest.mark.asyncio
    async def test_status_allowed_for_non_admin(self, monkeypatch):
        """Non-admin user CAN run /status (not in admin commands list)."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "admin1")
        monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")

        runner = _make_runner(Platform.WHATSAPP)
        event = _make_event("/status", Platform.WHATSAPP, "random_user", "group1")

        result = await runner._handle_message(event)
        assert "admin-only" not in (result or "").lower()

    @pytest.mark.asyncio
    async def test_no_admin_config_admin_commands_denied(self, monkeypatch):
        """Without GATEWAY_ADMIN_USERS set, admin commands are denied (fail-closed)."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")

        runner = _make_runner(Platform.WHATSAPP)
        event = _make_event("/bind dev", Platform.WHATSAPP, "anyone", "group1")

        result = await runner._handle_message(event)
        assert "admin-only" in (result or "").lower()

    @pytest.mark.asyncio
    async def test_all_admin_commands_protected(self, monkeypatch):
        """All commands in _ADMIN_COMMANDS are rejected for non-admins."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "admin1")
        monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")

        runner = _make_runner(Platform.TELEGRAM)

        for cmd in runner._ADMIN_COMMANDS:
            event = _make_event(f"/{cmd}", Platform.TELEGRAM, "intruder", "chat1")
            result = await runner._handle_message(event)
            assert "admin-only" in (result or "").lower(), f"/{cmd} should be admin-only"

    @pytest.mark.asyncio
    async def test_restart_rejected_for_non_admin_with_running_agent(self, monkeypatch):
        """Non-admin cannot /restart even when agent is running (early-intercept)."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "admin1")
        monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")

        runner = _make_runner(Platform.WHATSAPP)
        # Simulate a running agent
        runner._running_agents = {"whatsapp:group1:intruder": MagicMock()}
        runner._running_agents_ts = {"whatsapp:group1:intruder": __import__("time").time()}

        event = _make_event("/restart", Platform.WHATSAPP, "intruder", "group1")
        result = await runner._handle_message(event)
        assert "admin-only" in result.lower()

    @pytest.mark.asyncio
    async def test_stop_rejected_for_non_admin_with_running_agent(self, monkeypatch):
        """Non-admin cannot /stop even when agent is running (early-intercept)."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "admin1")
        monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")

        runner = _make_runner(Platform.WHATSAPP)
        # Simulate a running agent
        runner._running_agents = {"whatsapp:group1:intruder": MagicMock()}
        runner._running_agents_ts = {"whatsapp:group1:intruder": __import__("time").time()}

        event = _make_event("/stop", Platform.WHATSAPP, "intruder", "group1")
        result = await runner._handle_message(event)
        assert "admin-only" in result.lower()
