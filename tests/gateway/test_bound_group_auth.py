"""Tests for bound-group auto-open authorization.

When an admin runs /bind in a group, that group should automatically
be open to ALL members — no need for WHATSAPP_OPEN_GROUPS or allowlisting.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.extensions.channel_binding import (
    _session_bindings,
    _state_lock,
    _bound_channel_index,
    is_channel_bound,
)
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


# ── Helpers ──────────────────────────────────────────────────────────


def _set_runtime_binding(session_key: str, binding: dict):
    """Set a runtime binding + index entry (mimics _apply_binding_unlocked)."""
    with _state_lock:
        _session_bindings[session_key] = binding
        parts = session_key.split(":")
        if len(parts) >= 5:
            _bound_channel_index[(parts[2], parts[4])] = True


def _clear_runtime_binding(session_key: str):
    """Remove a runtime binding + index entry."""
    with _state_lock:
        parts = session_key.split(":")
        if len(parts) >= 5:
            _bound_channel_index.pop((parts[2], parts[4]), None)
        _session_bindings.pop(session_key, None)


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
        "WHATSAPP_OPEN_GROUPS", "TELEGRAM_OPEN_GROUPS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_source(platform: Platform, user_id: str, chat_id: str = "group1",
                 chat_type: str = "group") -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="tester",
        chat_type=chat_type,
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


# ── is_channel_bound unit tests ──────────────────────────────────────


class TestIsChannelBound:
    """Tests for the is_channel_bound() helper."""

    def test_no_binding_returns_false(self):
        """No binding at all → not bound."""
        with patch.dict(gateway_run.os.environ, {}, clear=False):
            # Clear any stale runtime bindings
            with _state_lock:
                _session_bindings.clear()
            assert is_channel_bound("whatsapp", "group123") is False

    def test_runtime_binding_returns_true(self):
        """Dynamic /bind creates a runtime binding → bound."""
        session_key = "agent:main:whatsapp:group:group123:group123"
        _set_runtime_binding(session_key, {"soul": "alpha"})

        try:
            assert is_channel_bound("whatsapp", "group123") is True
        finally:
            _clear_runtime_binding(session_key)

    def test_config_binding_returns_true(self):
        """Persisted /bind save creates config binding → bound."""
        with _state_lock:
            _session_bindings.clear()

        mock_bindings = {"whatsapp": [{"id": "group456", "soul": "dev"}]}
        with patch(
            "gateway.extensions.channel_binding._get_all_platform_bindings",
            return_value=mock_bindings,
        ):
            assert is_channel_bound("whatsapp", "group456") is True

    def test_wrong_platform_returns_false(self):
        """Binding exists for different platform → not bound."""
        session_key = "agent:main:telegram:group:group123:group123"
        _set_runtime_binding(session_key, {"soul": "dev"})

        try:
            assert is_channel_bound("whatsapp", "group123") is False
        finally:
            _clear_runtime_binding(session_key)

    def test_wrong_chat_id_returns_false(self):
        """Binding exists for different chat_id → not bound."""
        session_key = "agent:main:whatsapp:group:group999:group999"
        _set_runtime_binding(session_key, {"soul": "alpha"})

        try:
            assert is_channel_bound("whatsapp", "group123") is False
        finally:
            _clear_runtime_binding(session_key)

    def test_empty_inputs_return_false(self):
        assert is_channel_bound("", "group1") is False
        assert is_channel_bound("whatsapp", "") is False
        assert is_channel_bound("", "") is False


# ── _is_user_authorized bound-group tests ────────────────────────────


class TestBoundGroupAutoOpen:
    """Tests for auto-opening bound groups in _is_user_authorized()."""

    def test_bound_group_authorizes_any_user(self, monkeypatch):
        """A random user in a /bind-ed group should be authorized."""
        _clear_auth_env(monkeypatch)
        runner = _make_runner(Platform.WHATSAPP)

        # Simulate a runtime binding for group1
        session_key = "agent:main:whatsapp:group:group1:group1"
        _set_runtime_binding(session_key, {"soul": "alpha"})

        try:
            source = _make_source(
                Platform.WHATSAPP,
                user_id="9999999999",   # random user, NOT in any allowlist
                chat_id="group1",
                chat_type="group",
            )
            assert runner._is_user_authorized(source) is True
        finally:
            _clear_runtime_binding(session_key)

    def test_unbound_group_denies_random_user(self, monkeypatch):
        """A random user in an un-bound group should be denied."""
        _clear_auth_env(monkeypatch)
        runner = _make_runner(Platform.WHATSAPP)

        with _state_lock:
            _session_bindings.clear()
            _bound_channel_index.clear()

        source = _make_source(
            Platform.WHATSAPP,
            user_id="9999999999",
            chat_id="unbound_group",
            chat_type="group",
        )
        assert runner._is_user_authorized(source) is False

    def test_dm_never_auto_opened_by_binding(self, monkeypatch):
        """DM with a runtime binding should NOT auto-open —
        binding is for personality, not access control in DMs."""
        _clear_auth_env(monkeypatch)
        runner = _make_runner(Platform.WHATSAPP)

        # Even if somehow a DM session has a binding (unlikely but defensive)
        session_key = "agent:main:whatsapp:dm:85262155326"
        _set_runtime_binding(session_key, {"soul": "alpha"})

        try:
            source = _make_source(
                Platform.WHATSAPP,
                user_id="9999999999",
                chat_id="85262155326",
                chat_type="dm",
            )
            assert runner._is_user_authorized(source) is False
        finally:
            _clear_runtime_binding(session_key)

    def test_bound_group_plus_allowlisted_dm_both_work(self, monkeypatch):
        """Allowlisted user in DM works, and bound group works for anyone."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "85262155326")
        runner = _make_runner(Platform.WHATSAPP)

        # Set up a binding for group1
        session_key = "agent:main:whatsapp:group:group1:group1"
        _set_runtime_binding(session_key, {"soul": "alpha"})

        try:
            # Allowlisted user in DM → authorized
            dm_source = _make_source(
                Platform.WHATSAPP,
                user_id="85262155326",
                chat_id="85262155326",
                chat_type="dm",
            )
            assert runner._is_user_authorized(dm_source) is True

            # Random user in bound group → authorized
            group_source = _make_source(
                Platform.WHATSAPP,
                user_id="9999999999",
                chat_id="group1",
                chat_type="group",
            )
            assert runner._is_user_authorized(group_source) is True
        finally:
            _clear_runtime_binding(session_key)

    def test_config_binding_also_opens_group(self, monkeypatch):
        """Persisted /bind save also auto-opens the group."""
        _clear_auth_env(monkeypatch)
        runner = _make_runner(Platform.TELEGRAM)

        with _state_lock:
            _session_bindings.clear()

        mock_bindings = {"telegram": [{"id": "tg_group_789", "soul": "pm"}]}
        with patch(
            "gateway.extensions.channel_binding._get_all_platform_bindings",
            return_value=mock_bindings,
        ):
            source = _make_source(
                Platform.TELEGRAM,
                user_id="random_telegram_user",
                chat_id="tg_group_789",
                chat_type="group",
            )
            assert runner._is_user_authorized(source) is True

    def test_admin_commands_still_gated_in_bound_group(self, monkeypatch):
        """Bound group members can chat but NOT run admin commands."""
        _clear_auth_env(monkeypatch)
        monkeypatch.setenv("GATEWAY_ADMIN_USERS", "6911288694")
        runner = _make_runner(Platform.WHATSAPP)

        # Set up a binding
        session_key = "agent:main:whatsapp:group:group1:group1"
        _set_runtime_binding(session_key, {"soul": "alpha"})

        try:
            # Random user is authorized (can chat)
            source = _make_source(
                Platform.WHATSAPP,
                user_id="9999999999",
                chat_id="group1",
                chat_type="group",
            )
            assert runner._is_user_authorized(source) is True

            # But is NOT admin
            assert runner._is_admin_user(source) is False
        finally:
            _clear_runtime_binding(session_key)
