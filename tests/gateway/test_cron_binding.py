"""Tests for _get_cron_binding hook and run_job binding inheritance.

Covers:
  1. _get_cron_binding unit tests — origin → binding resolution
  2. run_job integration — binding injection into turn_route / prompt
"""

import threading
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import target — channel_binding module-level state is process-global so we
# must carefully reset between tests.
# ---------------------------------------------------------------------------
from gateway.extensions.channel_binding import (
    _get_cron_binding,
    _apply_binding,
    _session_souls,
    _session_model_overrides,
    _session_skills,
    _session_memory_scopes,
    _bound_channel_index,
    _state_lock,
)


# ---------------------------------------------------------------------------
# Fixtures — reset global dicts between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_state():
    """Clear all channel binding in-memory state before each test."""
    with _state_lock:
        _session_souls.clear()
        _session_model_overrides.clear()
        _session_skills.clear()
        _session_memory_scopes.clear()
        _bound_channel_index.clear()
    yield
    with _state_lock:
        _session_souls.clear()
        _session_model_overrides.clear()
        _session_skills.clear()
        _session_memory_scopes.clear()
        _bound_channel_index.clear()


# ---------------------------------------------------------------------------
# 1. _get_cron_binding unit tests
# ---------------------------------------------------------------------------

class TestGetCronBindingBasic:
    """Core _get_cron_binding — origin parsing + state lookup."""

    def test_none_origin_returns_none(self):
        assert _get_cron_binding(None) is None

    def test_empty_dict_returns_none(self):
        assert _get_cron_binding({}) is None

    def test_non_dict_returns_none(self):
        assert _get_cron_binding("not a dict") is None

    def test_missing_platform_returns_none(self):
        assert _get_cron_binding({"chat_id": "12345"}) is None

    def test_missing_chat_id_returns_none(self):
        assert _get_cron_binding({"platform": "whatsapp"}) is None

    def test_empty_platform_returns_none(self):
        assert _get_cron_binding({"platform": "", "chat_id": "123"}) is None

    def test_empty_chat_id_returns_none(self):
        assert _get_cron_binding({"platform": "telegram", "chat_id": ""}) is None


class TestGetCronBindingGroup:
    """_get_cron_binding for group origins."""

    def test_no_binding_returns_none(self):
        origin = {"platform": "whatsapp", "chat_id": "120363xxx@g.us"}
        assert _get_cron_binding(origin) is None

    def test_soul_content_inherited(self):
        session_key = "agent:main:whatsapp:group:120363xxx@g.us"
        _apply_binding(session_key, {
            "soul": "alpha",
            "_content": "You are Alpha, a helpful assistant.",
        })

        origin = {"platform": "whatsapp", "chat_id": "120363xxx@g.us"}
        result = _get_cron_binding(origin)

        assert result is not None
        assert result["soul_content"] == "You are Alpha, a helpful assistant."

    def test_model_override_inherited(self):
        session_key = "agent:main:telegram:group:-100123456"
        _apply_binding(session_key, {
            "soul": "dev",
            "_content": "Dev soul",
            "model": "anthropic/claude-sonnet-4",
            "provider": "anthropic",
            "api_key": "${ANTHROPIC_API_KEY}",
            "base_url": "https://api.anthropic.com",
        })

        origin = {"platform": "telegram", "chat_id": "-100123456"}
        result = _get_cron_binding(origin)

        assert result is not None
        assert result["model"] == "anthropic/claude-sonnet-4"
        assert result["provider"] == "anthropic"
        assert result["base_url"] == "https://api.anthropic.com"

    def test_skills_inherited(self):
        session_key = "agent:main:discord:group:1234567890"
        _apply_binding(session_key, {
            "soul": "pm",
            "_content": "PM soul",
            "skills": ["plan", "writing-plans"],
        })

        origin = {"platform": "discord", "chat_id": "1234567890"}
        result = _get_cron_binding(origin)

        assert result is not None
        assert result["skills"] == ["plan", "writing-plans"]

    def test_memory_scope_inherited(self):
        session_key = "agent:main:whatsapp:group:98765@g.us"
        _apply_binding(session_key, {
            "soul": "research",
            "_content": "Research soul",
            "memory_scope": "research",
        })

        origin = {"platform": "whatsapp", "chat_id": "98765@g.us"}
        result = _get_cron_binding(origin)

        assert result is not None
        assert result["memory_scope"] == "research"

    def test_full_binding_all_fields(self):
        session_key = "agent:main:whatsapp:group:full@test"
        _apply_binding(session_key, {
            "soul": "alpha",
            "_content": "Alpha soul content",
            "model": "openai/gpt-4o",
            "provider": "openai",
            "api_key": "${OPENAI_API_KEY}",
            "base_url": "https://api.openai.com/v1",
            "skills": ["investment-research-wiki", "plan"],
            "memory_scope": "investment",
        })

        origin = {"platform": "whatsapp", "chat_id": "full@test"}
        result = _get_cron_binding(origin)

        assert result is not None
        assert result["model"] == "openai/gpt-4o"
        assert result["provider"] == "openai"
        assert result["base_url"] == "https://api.openai.com/v1"
        assert result["soul_content"] == "Alpha soul content"
        assert "investment-research-wiki" in result["skills"]
        assert result["memory_scope"] == "investment"

    def test_chat_id_coerced_to_str(self):
        """Origin with int chat_id should still resolve."""
        session_key = "agent:main:telegram:group:12345"
        _apply_binding(session_key, {
            "soul": "dev",
            "_content": "Dev",
        })

        origin = {"platform": "telegram", "chat_id": 12345}  # int, not str
        result = _get_cron_binding(origin)
        assert result is not None
        assert result["soul_content"] == "Dev"


class TestGetCronBindingThread:
    """_get_cron_binding for thread/Discord topic origins."""

    def test_thread_resolves_with_thread_id(self):
        """Thread origin builds session_key with thread as effective_id."""
        session_key = "agent:main:discord:thread:42:42"
        _apply_binding(session_key, {
            "soul": "dev",
            "_content": "Thread dev soul",
        })

        origin = {"platform": "discord", "chat_id": "12345", "thread_id": "42"}
        result = _get_cron_binding(origin)

        assert result is not None
        assert result["soul_content"] == "Thread dev soul"

    def test_thread_fallback_to_parent_group(self):
        """If thread has no binding, fall back to parent group channel."""
        # Bind the parent group, not the thread
        parent_key = "agent:main:discord:group:12345"
        _apply_binding(parent_key, {
            "soul": "pm",
            "_content": "PM from parent group",
            "model": "anthropic/claude-opus-4.6",
        })

        origin = {"platform": "discord", "chat_id": "12345", "thread_id": "99"}
        result = _get_cron_binding(origin)

        assert result is not None
        assert result["soul_content"] == "PM from parent group"
        assert result["model"] == "anthropic/claude-opus-4.6"

    def test_thread_no_binding_no_fallback(self):
        """Thread with no binding and no parent binding returns None."""
        origin = {"platform": "discord", "chat_id": "99999", "thread_id": "77"}
        result = _get_cron_binding(origin)
        assert result is None

    def test_thread_binding_takes_priority_over_parent(self):
        """If both thread and parent have bindings, thread wins."""
        parent_key = "agent:main:discord:group:55555"
        thread_key = "agent:main:discord:thread:10:10"

        _apply_binding(parent_key, {
            "soul": "parent-soul",
            "_content": "Parent content",
        })
        _apply_binding(thread_key, {
            "soul": "thread-soul",
            "_content": "Thread content",
        })

        origin = {"platform": "discord", "chat_id": "55555", "thread_id": "10"}
        result = _get_cron_binding(origin)

        assert result is not None
        assert result["soul_content"] == "Thread content"


class TestGetCronBindingEdgeCases:
    """Edge cases and error resilience."""

    def test_origin_with_extra_fields_ignored(self):
        """Extra fields in origin dict don't break resolution."""
        session_key = "agent:main:whatsapp:group:edge@test"
        _apply_binding(session_key, {
            "soul": "alpha",
            "_content": "Alpha",
        })

        origin = {
            "platform": "whatsapp",
            "chat_id": "edge@test",
            "chat_name": "Test Group",
            "thread_id": None,
            "extra_field": "ignored",
        }
        result = _get_cron_binding(origin)
        assert result is not None
        assert result["soul_content"] == "Alpha"

    def test_model_only_no_soul(self):
        """Binding with only model override (no soul content) works."""
        session_key = "agent:main:telegram:group:monly"
        _apply_binding(session_key, {
            "soul": "dev",
            "_content": None,  # no soul content
            "model": "openai/gpt-4o-mini",
        })

        # Manually clear soul to simulate model-only
        with _state_lock:
            _session_souls.pop(session_key, None)

        origin = {"platform": "telegram", "chat_id": "monly"}
        result = _get_cron_binding(origin)

        assert result is not None
        assert result["model"] == "openai/gpt-4o-mini"
        assert "soul_content" not in result

    def test_different_platforms_isolated(self):
        """Bindings on different platforms don't cross-contaminate."""
        wa_key = "agent:main:whatsapp:group:shared-id"
        tg_key = "agent:main:telegram:group:shared-id"

        _apply_binding(wa_key, {"soul": "wa-soul", "_content": "WA content"})
        _apply_binding(tg_key, {"soul": "tg-soul", "_content": "TG content"})

        wa_result = _get_cron_binding({"platform": "whatsapp", "chat_id": "shared-id"})
        tg_result = _get_cron_binding({"platform": "telegram", "chat_id": "shared-id"})

        assert wa_result["soul_content"] == "WA content"
        assert tg_result["soul_content"] == "TG content"


# ---------------------------------------------------------------------------
# 2. run_job binding integration tests
# ---------------------------------------------------------------------------

class TestRunJobBindingInheritance:
    """Integration: cron run_job inherits binding from origin channel."""

    @pytest.fixture
    def _setup_binding(self):
        """Pre-populate a binding so _get_cron_binding can resolve it."""
        session_key = "agent:main:whatsapp:group:test-group@g.us"
        _apply_binding(session_key, {
            "soul": "alpha",
            "_content": "You are Alpha, the全能 assistant.",
            "model": "openai/gpt-4o",
            "provider": "openai",
            "api_key": "sk-test-key",
            "base_url": "https://api.openai.com/v1",
        })
        yield
        # cleanup
        with _state_lock:
            _session_souls.clear()
            _session_model_overrides.clear()
            _session_skills.clear()
            _session_memory_scopes.clear()
            _bound_channel_index.clear()

    def test_run_job_inherits_binding_model(self, tmp_path, _setup_binding):
        """run_job with origin in a bound channel inherits the bound model."""
        from cron.scheduler import run_job

        origin = {
            "platform": "whatsapp",
            "chat_id": "test-group@g.us",
            "chat_name": "Test",
        }
        job = {
            "id": "binding-test-1",
            "name": "inherit model",
            "prompt": "hello",
            "origin": origin,
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "default-key",
                     "base_url": "https://default.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        assert error is None
        # The agent should have been constructed with the inherited model
        kwargs = mock_agent_cls.call_args.kwargs
        assert kwargs["model"] == "openai/gpt-4o"

    def test_run_job_inherits_binding_soul_content(self, tmp_path, _setup_binding):
        """run_job with origin in a bound channel prepends soul content to prompt."""
        from cron.scheduler import run_job

        origin = {
            "platform": "whatsapp",
            "chat_id": "test-group@g.us",
            "chat_name": "Test",
        }
        job = {
            "id": "binding-test-2",
            "name": "inherit soul",
            "prompt": "hello",
            "origin": origin,
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "default-key",
                     "base_url": "https://default.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        # Verify the prompt passed to AIAgent includes soul content
        # run_conversation(prompt) is called with a single positional arg
        call_args = mock_agent.run_conversation.call_args
        user_msg = call_args.args[0] if call_args.args else ""
        assert "You are Alpha" in user_msg
        assert "hello" in user_msg

    def test_run_job_inherits_binding_runtime(self, tmp_path, _setup_binding):
        """run_job inherits provider/api_key/base_url from bound channel."""
        from cron.scheduler import run_job

        origin = {
            "platform": "whatsapp",
            "chat_id": "test-group@g.us",
            "chat_name": "Test",
        }
        job = {
            "id": "binding-test-3",
            "name": "inherit runtime",
            "prompt": "hello",
            "origin": origin,
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "default-key",
                     "base_url": "https://default.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        kwargs = mock_agent_cls.call_args.kwargs
        # Should inherit from binding, not from default runtime
        assert kwargs.get("api_key") == "sk-test-key"
        assert kwargs.get("base_url") == "https://api.openai.com/v1"
        assert kwargs.get("provider") == "openai"

    def test_run_job_job_model_takes_priority_over_binding(self, tmp_path, _setup_binding):
        """Job-level model field overrides the inherited binding model."""
        from cron.scheduler import run_job

        origin = {
            "platform": "whatsapp",
            "chat_id": "test-group@g.us",
            "chat_name": "Test",
        }
        job = {
            "id": "binding-test-4",
            "name": "job model priority",
            "prompt": "hello",
            "model": "anthropic/claude-opus-4.6",  # job-level model
            "origin": origin,
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "default-key",
                     "base_url": "https://default.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        kwargs = mock_agent_cls.call_args.kwargs
        # Job model should win over binding model
        assert kwargs["model"] == "anthropic/claude-opus-4.6"

    def test_run_job_no_origin_no_binding(self, tmp_path):
        """run_job without origin skips binding entirely."""
        from cron.scheduler import run_job

        job = {
            "id": "no-origin-job",
            "name": "no origin",
            "prompt": "hello",
            # no "origin" key
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "default-key",
                     "base_url": "https://default.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        kwargs = mock_agent_cls.call_args.kwargs
        # No binding injection — model should be whatever default resolves to
        assert "model" not in kwargs or kwargs.get("model") != "openai/gpt-4o"

    def test_run_job_binding_error_does_not_crash(self, tmp_path):
        """If _get_cron_binding throws, run_job still succeeds (try/except guard)."""
        from cron.scheduler import run_job

        job = {
            "id": "error-binding-job",
            "name": "error test",
            "prompt": "hello",
            "origin": {"platform": "whatsapp", "chat_id": "broken"},
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "default-key",
                     "base_url": "https://default.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch(
                 "gateway.extensions.fire_hooks_first",
                 side_effect=ImportError("hook not available"),
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        assert error is None
