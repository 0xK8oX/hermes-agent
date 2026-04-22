"""
Integration test: fallback_providers with real HTTP interception.

Uses ``respx`` to mock the HTTP layer (openai/httpx) while letting all
auxiliary_client internals run for real:
  - _load_fallback_providers
  - _build_client_from_entry
  - _is_rate_limit_or_overload_error
  - _build_call_kwargs
  - _validate_llm_response

Primary endpoint returns 429 → configured fallback returns 200.
"""
import json
import os
import sys
from pathlib import Path

import pytest
import respx
from httpx import Response
from openai import OpenAI, AsyncOpenAI

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.auxiliary_client import (
    call_llm,
    async_call_llm,
    _cached_fallback_providers,
)


@pytest.fixture(autouse=True)
def reset_fallback_cache():
    """Clear the fallback-provider cache before every test."""
    global _cached_fallback_providers
    _cached_fallback_providers = None
    yield
    _cached_fallback_providers = None


def _make_openai_response(content: str, model: str = "gpt-4o") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
    }


class TestFallbackIntegrationSync:
    @respx.mock
    def test_429_triggers_configured_fallback_with_real_http(self, monkeypatch):
        """
        Primary (openrouter) returns 429.
        Configured fallback (custom endpoint) returns 200.
        """
        # --- mock config --------------------------------------------------
        monkeypatch.setattr(
            "agent.auxiliary_client._cached_fallback_providers",
            [
                {
                    "provider": "custom",
                    "model": "fallback-model",
                    "base_url": "https://fallback.example.com/v1",
                    "api_key": "fb-key-123",
                }
            ],
        )

        # --- intercept primary (openrouter) -------------------------------
        primary_route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=Response(429, json={"error": "rate limit"})
        )

        # --- intercept fallback -------------------------------------------
        fallback_route = respx.post(
            "https://fallback.example.com/v1/chat/completions"
        ).mock(return_value=Response(200, json=_make_openai_response("fallback ok")))

        # --- inject a fake primary client so _get_cached_client succeeds ---
        fake_primary = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key="fake-primary-key",
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._get_cached_client",
            lambda *a, **kw: (fake_primary, "openrouter/model"),
        )

        # --- call ---------------------------------------------------------
        result = call_llm(
            task="compression",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.5,
            max_tokens=100,
        )

        # --- assertions ---------------------------------------------------
        assert primary_route.called
        assert fallback_route.called
        assert "fallback ok" in str(result)

        # Verify fallback request headers carried the explicit api_key
        req = fallback_route.calls.last.request
        assert req.headers["authorization"] == "Bearer fb-key-123"

    # NOTE: Testing that configured fallback fails → auto-chain is covered by
    # the mock-based E2E suite (test_fallback_providers_e2e.py).
    # The respx integration tests above prove the real HTTP path is correct.


class TestFallbackIntegrationAsync:
    @pytest.mark.asyncio
    @respx.mock
    async def test_async_429_configured_fallback(self, monkeypatch):
        """Async path with respx interception."""
        monkeypatch.setattr(
            "agent.auxiliary_client._cached_fallback_providers",
            [
                {
                    "provider": "custom",
                    "model": "async-fallback",
                    "base_url": "https://async-fb.example.com/v1",
                    "api_key": "async-fb-key",
                }
            ],
        )

        respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=Response(429, json={"error": "rate limit"})
        )
        fb_route = respx.post("https://async-fb.example.com/v1/chat/completions").mock(
            return_value=Response(200, json=_make_openai_response("async fallback ok"))
        )

        fake_primary = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key="fake-primary-key",
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._get_cached_client",
            lambda *a, **kw: (fake_primary, "openrouter/model"),
        )

        result = await async_call_llm(
            task="compression",
            messages=[{"role": "user", "content": "hello"}],
        )

        assert fb_route.called
        assert "async fallback ok" in str(result)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
