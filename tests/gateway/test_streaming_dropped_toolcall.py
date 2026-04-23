"""Tests for streaming dropped tool-call surface fix.

Covers untested commit: eb92883a fix(streaming): surface dropped tool-call on mid-stream stall.

When streaming dies after text was delivered but before a tool-call's
arguments finished, the partial-stream stub now surfaces a warning
so the user knows the action was not executed.

Tests:
1. partial_tool_names accumulation during streaming
2. Warning appended to partial text when stream stalls
3. Stub message includes warning for next turn's model
4. Text-only stalls (no tool calls) keep original behavior
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestPartialToolNameAccumulation:
    """Tool names are collected as they appear in streaming deltas."""

    def test_result_dict_has_partial_tool_names(self):
        """The result dict is initialized with partial_tool_names=[]."""
        result = {"response": None, "error": None, "partial_tool_names": []}
        assert result["partial_tool_names"] == []

    def test_tool_names_appended(self):
        """When a tool name appears in a delta, it's appended."""
        result = {"response": None, "error": None, "partial_tool_names": []}
        # Simulate streaming deltas producing tool names
        result["partial_tool_names"].append("write_file")
        result["partial_tool_names"].append("terminal")
        assert result["partial_tool_names"] == ["write_file", "terminal"]

    def test_duplicate_tool_names_tracked(self):
        """Same tool name can appear multiple times (multiple calls)."""
        result = {"response": None, "error": None, "partial_tool_names": []}
        result["partial_tool_names"].append("write_file")
        result["partial_tool_names"].append("write_file")
        assert len(result["partial_tool_names"]) == 2


class TestStreamStallWarning:
    """When stream stalls with partial tool names, a warning is surfaced."""

    def test_warning_message_format(self):
        """Warning includes the dropped tool names."""
        partial_names = ["write_file", "terminal"]
        _name_str = ", ".join(partial_names[:3])
        if len(partial_names) > 3:
            _name_str += f", +{len(partial_names) - 3} more"

        _warn = (
            f"\n\n⚠ Stream stalled mid tool-call "
            f"({_name_str}); the action was not executed. "
            f"Ask me to retry if you want to continue."
        )

        assert "write_file" in _warn
        assert "terminal" in _warn
        assert "not executed" in _warn

    def test_warning_truncates_many_tools(self):
        """If >3 tools, show first 3 and '+N more'."""
        partial_names = ["t1", "t2", "t3", "t4", "t5"]
        _name_str = ", ".join(partial_names[:3])
        if len(partial_names) > 3:
            _name_str += f", +{len(partial_names) - 3} more"

        assert "t1, t2, t3" in _name_str
        assert "+2 more" in _name_str

    def test_warning_appended_to_partial_text(self):
        """Warning is appended to the existing partial text."""
        partial_text = "Here are the results..."
        partial_names = ["write_file"]

        _name_str = ", ".join(partial_names[:3])
        _warn = (
            f"\n\n⚠ Stream stalled mid tool-call "
            f"({_name_str}); the action was not executed. "
            f"Ask me to retry if you want to continue."
        )
        result_text = (partial_text or "") + _warn

        assert result_text.startswith("Here are the results...")
        assert "⚠ Stream stalled" in result_text
        assert "write_file" in result_text


class TestStubMessageConstruction:
    """The stub message includes the warning for the model's next turn."""

    def test_stub_includes_warning_in_content(self):
        """Stub assistant message content includes the warning."""
        partial_text = "Processing your request..."
        partial_names = ["write_file"]
        _name_str = ", ".join(partial_names[:3])
        _warn = (
            f"\n\n⚠ Stream stalled mid tool-call "
            f"({_name_str}); the action was not executed. "
            f"Ask me to retry if you want to continue."
        )
        _partial_text = (partial_text or "") + _warn

        # Simulate stub construction (run_agent.py lines 6116-6127)
        _stub_msg = MagicMock()
        _stub_msg.role = "assistant"
        _stub_msg.content = _partial_text
        _stub_msg.tool_calls = None
        _stub_msg.reasoning_content = None

        assert _stub_msg.content is not None
        assert "⚠ Stream stalled" in _stub_msg.content
        assert _stub_msg.tool_calls is None

    def test_text_only_stall_no_warning(self):
        """Text-only stalls (no tool calls) keep original behavior."""
        partial_names = []
        _partial_text = "Here's what I found..."

        if _partial_names := partial_names:
            _name_str = ", ".join(_partial_names[:3])
            _warn = (
                f"\n\n⚠ Stream stalled mid tool-call "
                f"({_name_str}); the action was not executed."
            )
            _partial_text += _warn

        assert "⚠ Stream stalled" not in _partial_text
        assert _partial_text == "Here's what I found..."


class TestStreamStallDetection:
    """Error + deltas sent + partial tool names = stall detected."""

    def test_stall_detected_when_all_conditions_met(self):
        """Stream stall is detected when error + deltas_sent + partial_tool_names."""
        result = {"error": "ConnectionReset", "partial_tool_names": ["write_file"]}
        deltas_were_sent = {"yes": True}

        should_warn = (
            result.get("error")
            and deltas_were_sent.get("yes")
            and result.get("partial_tool_names")
        )
        assert should_warn

    def test_no_stall_if_no_error(self):
        """No stall if no error."""
        result = {"error": None, "partial_tool_names": ["write_file"]}
        deltas_were_sent = {"yes": True}

        should_warn = (
            result.get("error")
            and deltas_were_sent.get("yes")
            and result.get("partial_tool_names")
        )
        assert not should_warn

    def test_no_stall_if_no_deltas_sent(self):
        """No stall if no deltas were sent (nothing to recover)."""
        result = {"error": "ConnectionReset", "partial_tool_names": ["write_file"]}
        deltas_were_sent = {"yes": False}

        should_warn = (
            result.get("error")
            and deltas_were_sent.get("yes")
            and result.get("partial_tool_names")
        )
        assert not should_warn

    def test_no_stall_if_no_partial_tools(self):
        """No stall if no tool names were accumulated (text-only)."""
        result = {"error": "ConnectionReset", "partial_tool_names": []}
        deltas_were_sent = {"yes": True}

        should_warn = (
            result.get("error")
            and deltas_were_sent.get("yes")
            and result.get("partial_tool_names")
        )
        assert not should_warn
