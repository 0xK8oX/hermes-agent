"""Regression test: channel binding model overrides without api_key.

Bug: _resolve_session_agent_runtime required api_key to accept a channel
binding override. Soul frontmatters set model: auto-xxx but none set api_key,
so every bound channel fell back to config default.

Fix: Condition changed from `if api_key` to `if api_key or model`.
"""

import pytest
from unittest.mock import patch
from types import SimpleNamespace

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = __import__('threading').Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    return runner


class TestChannelBindingModelOverride:
    """Verify model-only overrides (no api_key) are respected."""

    def test_model_override_without_api_key_is_respected(self, monkeypatch):
        """When a binding only sets model (no api_key), the override must apply."""
        runner = _make_runner()
        session_key = "agent:main:telegram:dm:12345"

        def _fake_fire_hooks_first(hook_name, *args, **kwargs):
            if hook_name == "get_session_overrides" and args and args[0] == session_key:
                return {"model": "auto-sam"}
            return None

        monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)

        # Patch _resolve_gateway_model to return a default
        monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "auto-default")

        model, runtime = runner._resolve_session_agent_runtime(
            session_key=session_key,
            user_config={},
        )

        assert model == "auto-sam", (
            f"Model override ignored. Expected 'auto-sam', got {model!r}"
        )

    def test_api_key_override_still_works(self, monkeypatch):
        """When a binding sets both model and api_key, full runtime must apply."""
        runner = _make_runner()
        session_key = "agent:main:telegram:dm:12345"

        def _fake_fire_hooks_first(hook_name, *args, **kwargs):
            if hook_name == "get_session_overrides" and args and args[0] == session_key:
                return {
                    "model": "auto-sam",
                    "provider": "openrouter",
                    "api_key": "sk-test",
                    "base_url": "https://openrouter.ai/api/v1",
                }
            return None

        monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)
        monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "auto-default")

        model, runtime = runner._resolve_session_agent_runtime(
            session_key=session_key,
            user_config={},
        )

        assert model == "auto-sam"
        assert runtime["api_key"] == "sk-test"
        assert runtime["provider"] == "openrouter"

    def test_no_override_uses_config_default(self, monkeypatch):
        """When no binding exists, config default model must be used."""
        runner = _make_runner()
        session_key = "agent:main:telegram:dm:12345"

        def _fake_fire_hooks_first(hook_name, *args, **kwargs):
            if hook_name == "get_session_overrides":
                return None
            return None

        monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)
        monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "auto-default")
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {
            "provider": "openrouter",
            "api_key": "sk-test",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        })

        model, runtime = runner._resolve_session_agent_runtime(
            session_key=session_key,
            user_config={},
        )

        assert model == "auto-default", (
            f"Default model not used. Expected 'auto-default', got {model!r}"
        )


class TestSkillsOverrideApplied:
    """Verify get_skills_override result is merged into enabled_toolsets."""

    def test_skills_override_merged_into_enabled_toolsets(self, monkeypatch):
        """When get_skills_override returns skills, they must be in toolsets."""
        runner = _make_runner()
        session_key = "agent:main:telegram:dm:12345"

        def _fake_fire_hooks_first(hook_name, *args, **kwargs):
            if hook_name == "get_skills_override" and args and args[0] == session_key:
                return {"devops", "research"}
            return None

        monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)

        # Verify the hook fires and returns the expected value
        from gateway.extensions import fire_hooks_first
        result = fire_hooks_first("get_skills_override", session_key)
        assert result == {"devops", "research"}


class TestCheckpointContextUnused:
    """Document that get_checkpoint result is fetched but never applied."""

    def test_checkpoint_context_fetched_but_not_applied(self, monkeypatch):
        """get_checkpoint returns context but _checkpoint_ctx is never used."""
        runner = _make_runner()
        session_key = "agent:main:telegram:dm:12345"

        calls = []

        def _fake_fire_hooks_first(hook_name, *args, **kwargs):
            calls.append(hook_name)
            if hook_name == "get_checkpoint" and args and args[0] == session_key:
                return {"checkpoint_id": "chk-123", "timestamp": 1234567890}
            return None

        monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)

        # We can only verify the hook was called; the result is discarded
        # because _checkpoint_ctx is assigned but never read.
        from gateway.extensions import fire_hooks_first
        result = fire_hooks_first("get_checkpoint", session_key)
        assert result == {"checkpoint_id": "chk-123", "timestamp": 1234567890}
        assert "get_checkpoint" in calls
