"""Regression test: channel binding soul must REPLACE the default identity.

Bug (phase 1): `get_ephemeral` hook was registered but `gateway/run.py` never fired it.
Bug (phase 2): Hook fired and soul text was appended to `ephemeral_system_prompt`,
    but `DEFAULT_AGENT_IDENTITY` ("You are Hermes Agent...") came FIRST in the
    stable prompt. The model followed the first identity instruction and ignored
    the soul.

Fix: `gateway/run.py` fires `get_ephemeral`, extracts `soul_content`, and passes
    it as `soul_identity` to `AIAgent`. `_build_system_prompt_parts` uses
    `soul_identity` INSTEAD of `SOUL.md` / `DEFAULT_AGENT_IDENTITY`.
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
from gateway.platforms.base import MessageEvent


class _CapturingAgent:
    """Fake agent that records the kwargs passed to its constructor."""

    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

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


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


@pytest.mark.asyncio
async def test_soul_identity_passed_to_agent_and_replaces_default_identity(monkeypatch, tmp_path):
    """
    When a soul is bound, gateway/run.py MUST pass it as `soul_identity`
    to AIAgent so that _build_system_prompt_parts uses it INSTEAD of
    DEFAULT_AGENT_IDENTITY.
    """
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    soul_text = "You are Dr. Lin, a caring health advisor. Speak in Cantonese."
    session_key = "agent:main:telegram:dm:12345"

    def _fake_fire_hooks_first(hook_name, *args, **kwargs):
        if hook_name == "get_ephemeral" and args and args[0] == session_key:
            return soul_text
        return None

    from gateway.extensions import fire_hooks_first as _orig_fire_hooks_first
    monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)

    (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
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
    )

    assert result["final_response"] == "ok"

    # 1. soul_identity must be passed to the agent constructor.
    assert _CapturingAgent.last_init.get("soul_identity") == soul_text, (
        f"soul_identity not passed to AIAgent.\n"
        f"Expected: {soul_text!r}\n"
        f"Got: {_CapturingAgent.last_init.get('soul_identity')!r}"
    )

    # 2. The old broken behaviour (soul only in ephemeral) is NOT enough.
    #    We still keep it in ephemeral for backward compat, but the key fix
    #    is soul_identity replacing the stable identity.
    ephemeral_prompt = _CapturingAgent.last_init.get("ephemeral_system_prompt") or ""
    assert soul_text in ephemeral_prompt, (
        f"Soul text missing from ephemeral_system_prompt: {ephemeral_prompt!r}"
    )


@pytest.mark.asyncio
async def test_no_session_key_skips_get_ephemeral(monkeypatch, tmp_path):
    """
    When session_key is empty, fire_hooks_first("get_ephemeral", ...)
    must NOT be called.
    """
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    calls = []

    def _fake_fire_hooks_first(hook_name, *args, **kwargs):
        calls.append((hook_name, args))
        return None

    monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)

    (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    _CapturingAgent.last_init = None

    await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="",
    )

    get_ephemeral_calls = [c for c in calls if c[0] == "get_ephemeral"]
    assert not get_ephemeral_calls, (
        f"get_ephemeral should not be called with empty session_key: {get_ephemeral_calls}"
    )

    assert _CapturingAgent.last_init.get("soul_identity") is None


@pytest.mark.asyncio
async def test_no_soul_bound_uses_default_identity(monkeypatch, tmp_path):
    """
    When no soul is bound, soul_identity must be None so the agent falls
    back to SOUL.md / DEFAULT_AGENT_IDENTITY.
    """
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    def _fake_fire_hooks_first(hook_name, *args, **kwargs):
        if hook_name == "get_ephemeral":
            return None
        return None

    monkeypatch.setattr("gateway.extensions.fire_hooks_first", _fake_fire_hooks_first)

    (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    _CapturingAgent.last_init = None

    await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert _CapturingAgent.last_init.get("soul_identity") is None


