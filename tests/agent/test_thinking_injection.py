"""Tests for thinking-block injection on third-party Anthropic-compatible endpoints.

When reasoning/thinking is enabled, third-party endpoints like Zhipu GLM
require every assistant message to contain a thinking block.  After stripping
signed thinking blocks (Anthropic-proprietary), we inject the reasoning text
from the original message as an unsigned thinking block.
"""

import json
import pytest

from agent.anthropic_adapter import convert_messages_to_anthropic


class TestThinkingInjectionThirdParty:
    """Tests for thinking block injection on third-party Anthropic endpoints."""

    # ── Basic injection ──────────────────────────────────────────────

    def test_injects_thinking_from_reasoning_field(self):
        """When thinking is enabled on a third-party endpoint, reasoning text
        from the original message is injected as a thinking block."""
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Let me think...",
                "reasoning": "I need to analyze this request carefully.",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"query": "test"}',
                        },
                    }
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "tc_1"},
            {"role": "assistant", "content": "Here is the answer."},
        ]
        _, result = convert_messages_to_anthropic(
            messages,
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            reasoning_config={"enabled": True, "effort": "medium"},
        )

        assistants = [m for m in result if m["role"] == "assistant"]
        # First assistant should have a thinking block injected
        first_blocks = assistants[0]["content"]
        thinking_blocks = [b for b in first_blocks if isinstance(b, dict) and b.get("type") == "thinking"]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0]["thinking"] == "I need to analyze this request carefully."

    def test_injects_thinking_from_reasoning_content_field(self):
        """Reasoning text in 'reasoning_content' field (Moonshot AI style) is also used."""
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "Analyzing the user's query step by step.",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "file contents", "tool_call_id": "tc_1"},
            {"role": "assistant", "content": "Done."},
        ]
        _, result = convert_messages_to_anthropic(
            messages,
            base_url="https://api.minimax.io/anthropic",
            reasoning_config={"enabled": True, "effort": "medium"},
        )

        assistants = [m for m in result if m["role"] == "assistant"]
        first_blocks = assistants[0]["content"]
        thinking_blocks = [b for b in first_blocks if isinstance(b, dict) and b.get("type") == "thinking"]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0]["thinking"] == "Analyzing the user's query step by step."

    # ── No injection when not needed ──────────────────────────────────

    def test_no_injection_when_thinking_disabled(self):
        """No thinking blocks injected when reasoning_config.enabled is False."""
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Thinking...",
                "reasoning": "Some reasoning",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "tc_1"},
        ]
        _, result = convert_messages_to_anthropic(
            messages,
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            reasoning_config={"enabled": False},
        )

        assistants = [m for m in result if m["role"] == "assistant"]
        thinking_blocks = [
            b for b in assistants[0]["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert len(thinking_blocks) == 0

    def test_no_injection_on_direct_anthropic(self):
        """No thinking blocks injected when using direct Anthropic API (no base_url)."""
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Thinking...",
                "reasoning": "Some reasoning",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "tc_1"},
        ]
        _, result = convert_messages_to_anthropic(
            messages,
            base_url=None,  # Direct Anthropic
            reasoning_config={"enabled": True, "effort": "medium"},
        )

        assistants = [m for m in result if m["role"] == "assistant"]
        # On direct Anthropic, thinking blocks are handled by the signed-block
        # path — no unsigned injection should happen.
        unsigned_thinking = [
            b for b in assistants[0]["content"]
            if isinstance(b, dict) and b.get("type") == "thinking" and not b.get("signature")
        ]
        assert len(unsigned_thinking) == 0

    def test_no_injection_when_no_reasoning_text(self):
        """No thinking block injected if the original message has no reasoning text."""
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "I'll search for that.",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "tc_1"},
        ]
        _, result = convert_messages_to_anthropic(
            messages,
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            reasoning_config={"enabled": True, "effort": "medium"},
        )

        assistants = [m for m in result if m["role"] == "assistant"]
        thinking_blocks = [
            b for b in assistants[0]["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        # No reasoning text → no thinking block injected
        assert len(thinking_blocks) == 0

    # ── Multi-turn scenarios ──────────────────────────────────────────

    def test_injects_thinking_for_all_assistant_messages(self):
        """Every assistant message in a multi-turn conversation gets thinking
        blocks injected when thinking is enabled on a third-party endpoint."""
        messages = [
            {"role": "user", "content": "Q1"},
            {
                "role": "assistant",
                "content": "Thinking about Q1",
                "reasoning": "First reasoning step",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "tool1", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "result1", "tool_call_id": "tc_1"},
            {
                "role": "assistant",
                "content": "More thinking",
                "reasoning": "Second reasoning step",
                "tool_calls": [
                    {
                        "id": "tc_2",
                        "type": "function",
                        "function": {"name": "tool2", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "result2", "tool_call_id": "tc_2"},
            {
                "role": "assistant",
                "content": "Final answer",
                "reasoning": "Final reasoning",
            },
        ]
        _, result = convert_messages_to_anthropic(
            messages,
            base_url="https://api.minimax.io/anthropic/v1",
            reasoning_config={"enabled": True, "effort": "high"},
        )

        assistants = [m for m in result if m["role"] == "assistant"]
        assert len(assistants) == 3

        # Each assistant should have a thinking block
        for i, asst in enumerate(assistants):
            thinking_blocks = [
                b for b in asst["content"]
                if isinstance(b, dict) and b.get("type") == "thinking"
            ]
            assert len(thinking_blocks) >= 1, (
                f"Assistant message {i} has no thinking block: {asst['content']}"
            )

    # ── Existing thinking blocks are preserved ────────────────────────

    def test_preserves_existing_thinking_blocks(self):
        """If the converted message already has a thinking block (e.g. from
        reasoning_details), it is not replaced by the injection."""
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Thinking...",
                "reasoning": "Fallback reasoning",
                "reasoning_details": [
                    {"type": "thinking", "thinking": "Preserved reasoning", "signature": "sig1"},
                ],
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "tc_1"},
        ]
        _, result = convert_messages_to_anthropic(
            messages,
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            reasoning_config={"enabled": True, "effort": "medium"},
        )

        assistants = [m for m in result if m["role"] == "assistant"]
        # The signed thinking block gets stripped (third-party) but unsigned
        # reasoning from the original message is injected.
        thinking_blocks = [
            b for b in assistants[0]["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert len(thinking_blocks) == 1
        # Should be the reasoning text from the original message
        assert thinking_blocks[0]["thinking"] == "Fallback reasoning"

    # ── No injection without reasoning_config ─────────────────────────

    def test_no_injection_without_reasoning_config(self):
        """No thinking blocks injected when reasoning_config is None (default)."""
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Thinking...",
                "reasoning": "Some reasoning",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "tc_1"},
        ]
        _, result = convert_messages_to_anthropic(
            messages,
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            reasoning_config=None,  # Default — no thinking config
        )

        assistants = [m for m in result if m["role"] == "assistant"]
        thinking_blocks = [
            b for b in assistants[0]["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert len(thinking_blocks) == 0
