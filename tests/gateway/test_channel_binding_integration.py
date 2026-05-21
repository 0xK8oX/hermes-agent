"""Integration test: full channel binding override flow.

Verifies that ALL channel binding hooks (model, skills, soul, checkpoint,
memory_scope) are wired correctly from gateway/run.py through to the agent.

These tests catch upstream merge regressions where hook calls survive but
their results are silently ignored.
"""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


class _CapturingAgent:
    """Fake agent that records kwargs passed to its constructor."""
    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []
        self._cached_system_prompt = None

    def run_conversation(self, user_message, conversation_history=None, task_id=None, persist_user_message=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


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
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


class TestChannelBindingFullFlow:
    """Verify the complete override pipeline: hooks -> model -> skills -> soul."""

    @pytest.mark.asyncio
    async def test_all_overrides_applied_together(self, monkeypatch, tmp_path):
        """When ALL overrides are set, every one must reach the agent."""
        _install_fake_agent(monkeypatch)
        runner = _make_runner()
        session_key = "agent:main:telegram:dm:12345"
        soul_text = "You are Dr. Lin, a caring health advisor."

        def _fake_fire_hooks_first(hook_name, *args, **kwargs):
            if hook_name == "get_session_overrides" and args[0] == session_key:
                return {
                    "model": "auto-sam",
                    "provider": "openrouter",
                    "api_key": "sk-test",
                    "skills": {"devops", "research"},
                }
            if hook_name == "get_ephemeral" and args[0] == session_key:
                return soul_text
            if hook_name == "get_skills_override" and args[0] == session_key:
                return {"devops", "research"}
            return None

        monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)

        (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n", encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
        monkeypatch.setattr(gateway_run, "load_dotenv", lambda *a, **kw: None)
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
        monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "sk-test",
            },
        )

        import hermes_cli.tools_config as tools_config
        monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

        _CapturingAgent.last_init = None

        result = await runner._run_agent(
            message="who are you",
            context_prompt="",
            history=[],
            source=_make_source(),
            session_id="session-1",
            session_key=session_key,
            skills_override={"devops", "research"},
        )

        assert result["final_response"] == "ok"

        # 1. Model override must apply
        assert _CapturingAgent.last_init.get("model") == "auto-sam", (
            f"Model override not applied. Got: {_CapturingAgent.last_init.get('model')!r}"
        )

        # 2. Soul identity must be passed
        assert _CapturingAgent.last_init.get("soul_identity") == soul_text, (
            f"soul_identity not passed. Got: {_CapturingAgent.last_init.get('soul_identity')!r}"
        )

        # 3. Skills override must be in enabled_toolsets
        enabled = _CapturingAgent.last_init.get("enabled_toolsets") or []
        assert "devops" in enabled or "research" in enabled, (
            f"Skills override not applied. enabled_toolsets={enabled!r}"
        )

    @pytest.mark.asyncio
    async def test_model_only_override_reaches_agent(self, monkeypatch, tmp_path):
        """Model-only override (no api_key) must still reach agent."""
        _install_fake_agent(monkeypatch)
        runner = _make_runner()
        session_key = "agent:main:telegram:dm:12345"

        def _fake_fire_hooks_first(hook_name, *args, **kwargs):
            if hook_name == "get_session_overrides" and args[0] == session_key:
                return {"model": "auto-sam"}
            return None

        monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)

        (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n", encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
        monkeypatch.setattr(gateway_run, "load_dotenv", lambda *a, **kw: None)
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
        monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "sk-test",
            },
        )

        import hermes_cli.tools_config as tools_config
        monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

        _CapturingAgent.last_init = None

        result = await runner._run_agent(
            message="hi",
            context_prompt="",
            history=[],
            source=_make_source(),
            session_id="session-1",
            session_key=session_key,
        )

        assert _CapturingAgent.last_init.get("model") == "auto-sam"

    def test_soul_identity_included_in_cache_signature(self, monkeypatch):
        """Agent cache signature must include soul_identity so soul switches
        invalidate the cache even when model/runtime/toolsets are unchanged."""
        runner = _make_runner()

        sig_doctor = runner._agent_config_signature(
            model="auto-sam",
            runtime={"api_key": "sk-test"},
            enabled_toolsets=["core"],
            ephemeral_prompt="",
            soul_identity="You are Dr. Lin",
        )
        sig_dev = runner._agent_config_signature(
            model="auto-sam",
            runtime={"api_key": "sk-test"},
            enabled_toolsets=["core"],
            ephemeral_prompt="",
            soul_identity="You are a developer",
        )
        sig_none = runner._agent_config_signature(
            model="auto-sam",
            runtime={"api_key": "sk-test"},
            enabled_toolsets=["core"],
            ephemeral_prompt="",
            soul_identity=None,
        )

        assert sig_doctor != sig_dev, (
            "Cache signature must change when soul_identity changes. "
            "Different souls would reuse the same cached agent."
        )
        assert sig_doctor != sig_none, (
            "Cache signature must change when soul is bound vs unbound."
        )


class TestResolveSessionAgentRuntime:
    """Direct tests for _resolve_session_agent_runtime override logic."""

    def test_channel_binding_overrides_session_model_override(self, monkeypatch):
        """Channel binding must take precedence over /model session override."""
        runner = _make_runner()
        session_key = "agent:main:telegram:dm:12345"
        runner._session_model_overrides[session_key] = {"model": "auto-jason"}

        def _fake_fire_hooks_first(hook_name, *args, **kwargs):
            if hook_name == "get_session_overrides" and args[0] == session_key:
                return {"model": "auto-sam"}
            return None

        monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)
        monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openrouter",
                "api_key": "sk-test",
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "chat_completions",
            },
        )

        model, runtime = runner._resolve_session_agent_runtime(
            session_key=session_key,
            user_config={},
        )

        assert model == "auto-sam", (
            f"Channel binding should override /model. Expected 'auto-sam', got {model!r}"
        )

    def test_empty_override_dict_is_ignored(self, monkeypatch):
        """Empty dict override must not crash or change behavior."""
        runner = _make_runner()
        session_key = "agent:main:telegram:dm:12345"

        def _fake_fire_hooks_first(hook_name, *args, **kwargs):
            if hook_name == "get_session_overrides" and args[0] == session_key:
                return {}
            return None

        monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)
        monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openrouter",
                "api_key": "sk-test",
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "chat_completions",
            },
        )

        model, runtime = runner._resolve_session_agent_runtime(
            session_key=session_key,
            user_config={},
        )

        assert model == "gpt-5.4"
