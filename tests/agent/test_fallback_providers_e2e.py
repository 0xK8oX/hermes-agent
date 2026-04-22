"""
E2E dry-run test for configured fallback_providers (rate-limit/server-overload fallback).

Key design constraints for these tests:
- `_try_configured_fallback` is patched to return a mock client directly,
  bypassing real OpenAI SDK initialization (which has internal HTTP transport
  that resists simple mock.patch on .create()).
- `_resolve_task_provider_model` is patched to return "auto" so that
  `is_auto=True` and the fallback block (lines 2834-2871) is entered.
- `_try_payment_fallback` (the old auto-detection chain) is also patched
  when we need to simulate "no configured fallback → try auto chain".
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent.auxiliary_client import (
    call_llm,
    async_call_llm,
    _load_fallback_providers,
    _try_configured_fallback,
    _build_client_from_entry,
    _is_rate_limit_or_overload_error,
)


# ---------------------------------------------------------------------------
# Fixtures

@pytest.fixture(autouse=True)
def clean_cached_fallback_providers():
    import agent.auxiliary_client as aux
    aux._cached_fallback_providers = None
    yield
    aux._cached_fallback_providers = None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in (
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# Helpers

def make_primary_fails(code=500, msg="Internal Server Error"):
    exc = Exception(msg)
    exc.status_code = code
    c = MagicMock()
    c.chat.completions.create.side_effect = exc
    return c


def make_success_client(model="fallback-model"):
    c = MagicMock(unsafe=True)
    completion = MagicMock()
    completion.model = model
    choice = MagicMock()
    choice.message = MagicMock()
    choice.message.role = "assistant"
    choice.message.content = "fallback response"
    choice.finish_reason = "stop"
    choice.index = 0
    completion.choices = [choice]
    completion.usage = MagicMock()
    completion.usage.prompt_tokens = 5
    completion.usage.completion_tokens = 10
    completion.usage.total_tokens = 15
    # __str__ must return something that contains the expected text for
    # "fallback response" assertions.  MagicMock's default repr is <MagicMock>.
    completion.__str__ = lambda self: "fallback response"
    # Only set return_value on the specific method, NOT on the parent mock.
    # Setting parent.return_value corrupts child mock chains (MagicMock quirk).
    c.chat.completions.create.return_value = completion
    return c


# Standard patches that must always be present for call_llm E2E tests.
# Ensures is_auto=True so the fallback block is entered.
BASE_E2E_PATCHES = [
    patch("agent.auxiliary_client._resolve_task_provider_model",
          return_value=("auto", "google/gemini-3-flash-preview", None, None, None)),
]


# ---------------------------------------------------------------------------
# _is_rate_limit_or_overload_error

class TestIsRateLimitOrOverloadError:
    def test_429_returns_true(self):
        exc = Exception("Rate limit hit"); exc.status_code = 429
        assert _is_rate_limit_or_overload_error(exc) is True

    def test_500_returns_true(self):
        exc = Exception("Internal Server Error"); exc.status_code = 500
        assert _is_rate_limit_or_overload_error(exc) is True

    def test_502_returns_true(self):
        exc = Exception("Bad Gateway"); exc.status_code = 502
        assert _is_rate_limit_or_overload_error(exc) is True

    def test_503_returns_true(self):
        exc = Exception("Service Unavailable"); exc.status_code = 503
        assert _is_rate_limit_or_overload_error(exc) is True

    def test_529_returns_true(self):
        exc = Exception("Bandwidth Limit Exceeded"); exc.status_code = 529
        assert _is_rate_limit_or_overload_error(exc) is True

    def test_400_returns_false(self):
        exc = Exception("Bad Request"); exc.status_code = 400
        assert _is_rate_limit_or_overload_error(exc) is False

    def test_401_returns_false(self):
        exc = Exception("Unauthorized"); exc.status_code = 401
        assert _is_rate_limit_or_overload_error(exc) is False

    def test_rate_limit_keyword_no_status(self):
        exc = Exception("upstream rate limit exceeded, retry after 60s")
        exc.status_code = None
        assert _is_rate_limit_or_overload_error(exc) is True

    def test_overload_keyword_nonstandard_code(self):
        exc = Exception("Server overloaded, please try again")
        exc.status_code = 520
        assert _is_rate_limit_or_overload_error(exc) is True


# ---------------------------------------------------------------------------
# _load_fallback_providers

class TestLoadFallbackProviders:
    def test_missing_key_returns_empty_list(self, monkeypatch):
        with patch("hermes_cli.config.load_config", return_value={}):
            assert _load_fallback_providers() == []

    def test_null_value_returns_empty_list(self, monkeypatch):
        with patch("hermes_cli.config.load_config",
                    return_value={"fallback_providers": None}):
            assert _load_fallback_providers() == []

    def test_string_value_rejected_returns_empty(self, monkeypatch):
        with patch("hermes_cli.config.load_config",
                    return_value={"fallback_providers": "openrouter"}):
            assert _load_fallback_providers() == []

    def test_valid_list_loaded(self, monkeypatch):
        entries = [{"provider": "openrouter"},
                   {"provider": "custom", "base_url": "https://proxy/v1"}]
        with patch("hermes_cli.config.load_config",
                   return_value={"fallback_providers": entries}):
            assert _load_fallback_providers() == entries


# ---------------------------------------------------------------------------
# _build_client_from_entry

class TestBuildClientFromEntry:
    def test_skips_entry_no_model_no_base_url(self):
        entry = {"provider": "openrouter", "model": ""}
        client, model = _build_client_from_entry(entry)
        assert client is None

    def test_invalid_base_url_returns_none(self):
        entry = {"base_url": "not-a-valid-url"}
        client, model = _build_client_from_entry(entry)
        assert client is None

    def test_empty_base_url_returns_none(self):
        entry = {"base_url": ""}
        client, model = _build_client_from_entry(entry)
        assert client is None

    def test_custom_endpoint_with_explicit_key(self):
        """Client is built with correct base_url (no network call in unit test)."""
        entry = {
            "base_url": "https://my-proxy.com/v1",
            "api_key": "my-key-123",
            "model": "gpt-4o",
        }
        client, model = _build_client_from_entry(entry)
        assert client is not None
        # base_url may be URL object or string — normalize to str for assertion.
        base_str = str(getattr(client, "base_url", "") or "")
        assert "my-proxy.com" in base_str
        assert model == "gpt-4o"

    def test_custom_endpoint_falls_back_to_env_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key-456")
        entry = {"base_url": "https://my-proxy.com/v1", "model": "gpt-4o"}
        client, model = _build_client_from_entry(entry)
        assert client is not None
        assert client.api_key == "env-key-456"

    def test_custom_endpoint_no_creds_returns_none(self):
        entry = {"base_url": "https://my-proxy.com/v1", "model": "gpt-4o"}
        client, model = _build_client_from_entry(entry)
        assert client is None


# ---------------------------------------------------------------------------
# _try_configured_fallback

class TestTryConfiguredFallback:
    def test_empty_config_returns_none(self, monkeypatch):
        with patch("hermes_cli.config.load_config", return_value={}):
            client, model, label = _try_configured_fallback("openrouter", "compression")
        assert client is None and label == ""

    def test_skips_failed_provider(self, monkeypatch):
        """Provider that just failed should be skipped even if configured."""
        mock_client = MagicMock()
        with patch("hermes_cli.config.load_config",
                   return_value={"fallback_providers": [
                       {"provider": "openrouter", "base_url": "https://or.fake/v1",
                        "api_key": "or-key"}
                   ]}), \
               patch("agent.auxiliary_client._build_client_from_entry",
                     return_value=(mock_client, "gpt-4o")):
            client, model, label = _try_configured_fallback("openrouter", "compression")
        assert client is None  # only provider was skipped

    def test_skips_non_dict_entry_continues_loop(self, monkeypatch):
        """A malformed non-dict entry should be skipped without crashing."""
        mock_client = MagicMock()
        with patch("hermes_cli.config.load_config",
                   return_value={"fallback_providers": [
                       None,  # would crash loop without isinstance guard
                       {"provider": "good-entry", "base_url": "https://good.fake/v1",
                        "api_key": "fk"},
                   ]}), \
               patch("agent.auxiliary_client._build_client_from_entry",
                     return_value=(mock_client, "gpt-4o")):
            client, model, label = _try_configured_fallback("openrouter", "compression")
        assert client is mock_client
        assert label == "good-entry"

    def test_successful_fallback_returns_client(self, monkeypatch):
        mock_client = MagicMock()
        with patch("hermes_cli.config.load_config",
                   return_value={"fallback_providers": [
                       {"provider": "my-custom", "base_url": "https://my.fake/v1",
                        "api_key": "fk"}
                   ]}), \
               patch("agent.auxiliary_client._build_client_from_entry",
                     return_value=(mock_client, "gpt-4o")):
            client, model, label = _try_configured_fallback("openrouter", "compression")
        assert client is mock_client
        assert label == "my-custom"


# ---------------------------------------------------------------------------
# call_llm — configured fallback triggered by 500 / 429 / 502 / 503 / 529
# ---------------------------------------------------------------------------

class TestCallLlmConfiguredFallback:
    def test_500_triggers_configured_fallback_success(self, monkeypatch):
        """
        Primary raises 500 → _is_rate_limit_or_overload_error → _try_configured_fallback
        returns mock client → call succeeds without any real network activity.
        """
        fallback = make_success_client("gpt-4o")
        messages = [{"role": "user", "content": "hello"}]
        fb_kwargs = {
            "model": "gpt-4o",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1024,
            "stream": False,
        }

        with patch("agent.auxiliary_client._try_configured_fallback",
                   return_value=(fallback, "gpt-4o", "my-fb")), \
             patch("agent.auxiliary_client._build_call_kwargs",
                   return_value=fb_kwargs), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(make_primary_fails(500), "main-model")):
            result = call_llm(
                task="compression",
                messages=messages,
            )
        assert "fallback response" in str(result)

    def test_429_triggers_configured_fallback(self, monkeypatch):
        """429 (rate-limit code) also triggers the configured fallback path."""
        fallback = make_success_client("gpt-4o")
        messages = [{"role": "user", "content": "hello"}]
        fb_kwargs = {"model": "gpt-4o", "messages": messages, "stream": False}

        with patch("agent.auxiliary_client._try_configured_fallback",
                   return_value=(fallback, "gpt-4o", "my-fb")), \
             patch("agent.auxiliary_client._build_call_kwargs",
                   return_value=fb_kwargs), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(make_primary_fails(429, "Rate limit exceeded"), "main-model")):
            result = call_llm(
                task="compression",
                messages=messages,
            )
        assert "fallback response" in str(result)

    def test_502_triggers_configured_fallback(self, monkeypatch):
        fallback = make_success_client("gpt-4o")
        messages = [{"role": "user", "content": "hello"}]
        fb_kwargs = {"model": "gpt-4o", "messages": messages, "stream": False}

        with patch("agent.auxiliary_client._try_configured_fallback",
                   return_value=(fallback, "gpt-4o", "my-fb")), \
             patch("agent.auxiliary_client._build_call_kwargs",
                   return_value=fb_kwargs), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(make_primary_fails(502, "Bad Gateway"), "main-model")):
            result = call_llm(task="compression",
                               messages=messages)
        assert "fallback response" in str(result)

    def test_500_no_configured_fallback_falls_through_to_auto_chain(self, monkeypatch):
        """Configured fallback returns (None, None, '') → auto-chain (_try_payment_fallback) is tried."""
        fallback = make_success_client("gpt-4o-mini")
        messages = [{"role": "user", "content": "hello"}]
        fb_kwargs = {"model": "gpt-4o-mini", "messages": messages, "stream": False}

        with patch("agent.auxiliary_client._try_configured_fallback",
                   return_value=(None, None, "")), \
             patch("agent.auxiliary_client._try_payment_fallback",
                   return_value=(fallback, "gpt-4o-mini", "openrouter")), \
             patch("agent.auxiliary_client._build_call_kwargs",
                   return_value=fb_kwargs), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(make_primary_fails(500), "main-model")):
            result = call_llm(
                task="compression",
                messages=messages,
            )
        assert "fallback response" in str(result)

    def test_500_no_fallbacks_available_raises_original(self, monkeypatch):
        """Both configured and auto-chain return None → original error propagates."""
        with patch("agent.auxiliary_client._try_configured_fallback",
                   return_value=(None, None, "")), \
             patch("agent.auxiliary_client._try_payment_fallback",
                   return_value=(None, None, "")), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(make_primary_fails(500), "main-model")):
            with pytest.raises(Exception, match="Internal Server Error"):
                call_llm(task="compression",
                         messages=[{"role": "user", "content": "hello"}])

    def test_402_payment_error_skips_to_auto_chain(self, monkeypatch):
        """402 → _is_payment_error → configured fallback tried first (returns None)
        → auto-chain succeeds."""
        fallback = make_success_client("gpt-4o")
        messages = [{"role": "user", "content": "hello"}]
        fb_kwargs = {"model": "gpt-4o", "messages": messages, "stream": False}

        with patch("agent.auxiliary_client._try_configured_fallback",
                   return_value=(None, None, "")), \
             patch("agent.auxiliary_client._try_payment_fallback",
                   return_value=(fallback, "gpt-4o", "openrouter")), \
             patch("agent.auxiliary_client._build_call_kwargs",
                   return_value=fb_kwargs), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(make_primary_fails(402, "Payment Required"), "main-model")):
            result = call_llm(task="compression",
                               messages=messages)
        assert "fallback response" in str(result)

    def test_401_unauthorized_does_not_trigger_fallback(self, monkeypatch):
        """401 is not a payment/connection/rate-limit error → no fallback attempted."""
        primary = make_primary_fails(401, "Unauthorized")

        with patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary, "main-model")), \
             patch("agent.auxiliary_client._try_configured_fallback") as mock_cfg, \
             patch("agent.auxiliary_client._try_payment_fallback") as mock_auto:
            with pytest.raises(Exception, match="Unauthorized"):
                call_llm(task="compression",
                         messages=[{"role": "user", "content": "hello"}])
        # Neither fallback path should have been attempted.
        mock_cfg.assert_not_called()
        mock_auto.assert_not_called()


# ---------------------------------------------------------------------------
# async_call_llm

class TestAsyncCallLlmConfiguredFallback:
    @pytest.mark.asyncio
    async def test_async_500_configured_fallback(self, monkeypatch):
        """AsyncAuxClient wraps the fallback in a real AsyncOpenAI, so patch
        _to_async_client to return (AsyncMock, model) — the correct 2-tuple."""
        completion = MagicMock()
        completion.model = "gpt-4o"
        completion.choices = [MagicMock(message=MagicMock(content="fallback response"))]
        completion.__str__ = lambda self: "fallback response"
        # AsyncMock so that `await fallback.chat.completions.create()` works.
        fallback = AsyncMock()
        fallback.chat.completions.create.return_value = completion
        messages = [{"role": "user", "content": "hello"}]
        fb_kwargs = {"model": "gpt-4o", "messages": messages, "stream": False}

        with patch("agent.auxiliary_client._try_configured_fallback",
                   return_value=(fallback, "gpt-4o", "my-fb")), \
             patch("agent.auxiliary_client._build_call_kwargs",
                   return_value=fb_kwargs), \
             patch("agent.auxiliary_client._to_async_client",
                   return_value=(fallback, "gpt-4o")), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(make_primary_fails(500), "main-model")):
            result = await async_call_llm(
                task="compression",
                messages=messages,
            )
        assert "fallback response" in str(result)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
