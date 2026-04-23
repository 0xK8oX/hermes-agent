"""Tests for Hall extension — inter-soul messaging.

Covers previously untested commits:
  - 45dae400: feat: Hall extension — inter-soul messaging board
  - 96f11df8: feat: Hall auto-dispatch with pending file + gateway watcher
  - c5130954: feat: Hall communication commands + cron memory inheritance
  - tools/hall_tool.py: entire file had zero tests

Tests cover:
1. hall_send / hall_read / hall_list / hall_mark_read / hall_clear — public API
2. _auto_dispatch — rate limiting, pending file, subprocess path
3. hall_tool() — tool dispatcher
4. Thread safety — concurrent reads/writes
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture(autouse=True)
def temp_hall(tmp_path, monkeypatch):
    """Redirect hall.jsonl to a temp directory for each test."""
    hall_file = tmp_path / "hall.jsonl"
    hall_file.touch()

    # hall.py imports get_hermes_home inside _hall_path(), so we patch
    # it at the hermes_constants module level
    with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
        # Also redirect dispatch dir
        dispatch_dir = tmp_path / "hall_dispatch"
        dispatch_dir.mkdir(parents=True, exist_ok=True)

        with patch("gateway.extensions.hall._dispatch_dir", return_value=dispatch_dir):
            yield hall_file


# ── 1. Core CRUD operations ────────────────────────────────────────


class TestHallSend:
    """hall_send creates entries in hall.jsonl."""

    def test_send_creates_entry(self, temp_hall):
        from gateway.extensions.hall import hall_send
        entry = hall_send("pm", "dev", "Build status", "Build passed!", "normal")
        assert entry["from"] == "pm"
        assert entry["to"] == "dev"
        assert entry["subject"] == "Build status"
        assert entry["body"] == "Build passed!"
        assert entry["priority"] == "normal"
        assert entry["dispatch"] == "queued"
        assert entry["read_by"] == []
        assert "id" in entry
        assert "ts" in entry

    def test_send_broadcast_to_all(self, temp_hall):
        from gateway.extensions.hall import hall_send
        entry = hall_send("pm", "all", "Announcement", "Deploying v2", "high")
        assert entry["to"] == "all"

    def test_send_validates_from_soul(self, temp_hall):
        from gateway.extensions.hall import hall_send
        with pytest.raises(ValueError, match="from_soul"):
            hall_send("", "dev", "Test", "Body")

    def test_send_validates_to_soul(self, temp_hall):
        from gateway.extensions.hall import hall_send
        with pytest.raises(ValueError, match="to_soul"):
            hall_send("pm", "", "Test", "Body")

    def test_send_auto_dispatch_flag(self, temp_hall):
        from gateway.extensions.hall import hall_send
        with patch("gateway.extensions.hall._auto_dispatch") as mock_dispatch:
            entry = hall_send("admin", "dev", "Urgent", "Fix this", "high", dispatch="auto")
            assert entry["dispatch"] == "auto"
            mock_dispatch.assert_called_once_with(entry)

    def test_send_queued_does_not_dispatch(self, temp_hall):
        from gateway.extensions.hall import hall_send
        with patch("gateway.extensions.hall._auto_dispatch") as mock_dispatch:
            entry = hall_send("pm", "dev", "Status", "OK", dispatch="queued")
            mock_dispatch.assert_not_called()

    def test_send_normalizes_case(self, temp_hall):
        from gateway.extensions.hall import hall_send
        entry = hall_send("PM", "Dev", "Test", "Body")
        assert entry["from"] == "pm"
        assert entry["to"] == "dev"

    def test_send_invalid_priority_defaults_to_normal(self, temp_hall):
        from gateway.extensions.hall import hall_send
        entry = hall_send("pm", "dev", "Test", "Body", priority="urgent")
        assert entry["priority"] == "normal"

    def test_send_invalid_dispatch_defaults_to_queued(self, temp_hall):
        from gateway.extensions.hall import hall_send
        entry = hall_send("pm", "dev", "Test", "Body", dispatch="immediate")
        assert entry["dispatch"] == "queued"


class TestHallRead:
    """hall_read returns unread messages for a soul."""

    def test_read_returns_unread_messages(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_read
        hall_send("pm", "dev", "Task 1", "Do this")
        hall_send("pm", "dev", "Task 2", "Do that")
        hall_send("pm", "ops", "Other", "Not for dev")

        messages = hall_read("dev")
        assert len(messages) == 2
        subjects = [m["subject"] for m in messages]
        assert "Task 1" in subjects
        assert "Task 2" in subjects

    def test_read_marks_as_read(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_read
        hall_send("pm", "dev", "Task", "Do this")
        messages = hall_read("dev")
        assert len(messages) == 1

        # Second read should return nothing
        messages2 = hall_read("dev")
        assert len(messages2) == 0

    def test_read_without_marking(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_read
        hall_send("pm", "dev", "Task", "Do this")

        messages = hall_read("dev", mark_read=False)
        assert len(messages) == 1

        # Should still be unread
        messages2 = hall_read("dev")
        assert len(messages2) == 1

    def test_read_includes_broadcast_messages(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_read
        hall_send("pm", "all", "Broadcast", "Hello everyone")

        messages = hall_read("dev")
        assert len(messages) == 1
        assert messages[0]["to"] == "all"

    def test_read_empty_soul_returns_empty(self, temp_hall):
        from gateway.extensions.hall import hall_read
        assert hall_read("") == []

    def test_read_no_messages(self, temp_hall):
        from gateway.extensions.hall import hall_read
        assert hall_read("dev") == []


class TestHallList:
    """hall_list returns recent messages."""

    def test_list_all_messages(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_list
        hall_send("pm", "dev", "Task 1", "Body")
        hall_send("ops", "dev", "Task 2", "Body")
        hall_send("pm", "ops", "Task 3", "Body")

        messages = hall_list()
        assert len(messages) == 3

    def test_list_filtered_by_soul(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_list
        hall_send("pm", "dev", "Task 1", "Body")
        hall_send("pm", "ops", "Task 2", "Body")

        messages = hall_list(soul="dev")
        # Should include messages TO dev
        assert any(m["to"] == "dev" for m in messages)

    def test_list_respects_limit(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_list
        for i in range(25):
            hall_send("pm", "dev", f"Task {i}", "Body")

        messages = hall_list(limit=10)
        assert len(messages) == 10


class TestHallMarkRead:
    """hall_mark_read marks a specific message as read."""

    def test_mark_read(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_mark_read, hall_read
        entry = hall_send("pm", "dev", "Task", "Body")

        result = hall_mark_read(entry["id"], "dev")
        assert result is True

        # Should not appear as unread anymore
        messages = hall_read("dev")
        assert len(messages) == 0

    def test_mark_read_nonexistent(self, temp_hall):
        from gateway.extensions.hall import hall_mark_read
        result = hall_mark_read("nonexistent-id", "dev")
        assert result is False


class TestHallClear:
    """hall_clear purges old messages."""

    def test_clear_old_messages(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_clear, hall_list
        hall_send("pm", "dev", "Old", "Body")

        removed = hall_clear(older_than_days=0)
        assert removed >= 1

        messages = hall_list()
        assert len(messages) == 0

    def test_clear_keeps_recent_messages(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_clear, hall_list
        hall_send("pm", "dev", "Recent", "Body")

        removed = hall_clear(older_than_days=30)
        assert removed == 0

        messages = hall_list()
        assert len(messages) == 1


# ── 2. Auto-dispatch ───────────────────────────────────────────────


class TestAutoDispatch:
    """Auto-dispatch triggers target soul's agent after hall_send."""

    def test_auto_dispatch_only_for_auto_mode(self, temp_hall):
        """Queued messages should NOT trigger dispatch."""
        from gateway.extensions.hall import _auto_dispatch
        with patch("gateway.extensions.hall._lookup_soul_channel") as mock:
            entry = {"to": "dev", "dispatch": "queued"}
            _auto_dispatch(entry)
            mock.assert_not_called()

    def test_auto_dispatch_skips_broadcast(self, temp_hall):
        """Broadcast (to='all') should not dispatch."""
        from gateway.extensions.hall import _auto_dispatch
        with patch("gateway.extensions.hall._lookup_soul_channel") as mock:
            entry = {"to": "all", "dispatch": "auto"}
            _auto_dispatch(entry)
            mock.assert_not_called()

    def test_auto_dispatch_skips_if_no_bound_channel(self, temp_hall):
        """If soul has no bound channel, skip dispatch."""
        from gateway.extensions.hall import _auto_dispatch
        with patch("gateway.extensions.hall._lookup_soul_channel", return_value=None):
            entry = {"to": "dev", "dispatch": "auto"}
            # Should not raise
            _auto_dispatch(entry)

    def test_auto_dispatch_writes_pending_in_subprocess(self, temp_hall):
        """In subprocess (no gateway runner), writes pending file."""
        from gateway.extensions.hall import _auto_dispatch
        with patch("gateway.extensions.hall._lookup_soul_channel", return_value={"platform": "telegram", "chat_id": "123"}):
            with patch("gateway.run.get_gateway_runner", return_value=None):
                with patch("gateway.extensions.hall._write_dispatch_pending") as mock_write:
                    entry = {"id": "test123", "to": "dev", "dispatch": "auto"}
                    _auto_dispatch(entry)
                    mock_write.assert_called_once_with(entry)

    def test_auto_dispatch_rate_limits(self, temp_hall):
        """Rate limits: one dispatch per soul per 2 seconds."""
        from gateway.extensions.hall import _auto_dispatch, _last_dispatch_time, _dispatch_rate_lock
        import gateway.extensions.hall as hall_mod

        # Set last dispatch time to just now for 'dev'
        with _dispatch_rate_lock:
            _last_dispatch_time["dev"] = time.monotonic()

        with patch("gateway.extensions.hall._lookup_soul_channel", return_value={"platform": "telegram", "chat_id": "123"}):
            with patch("gateway.run.get_gateway_runner", return_value=None):
                with patch("gateway.extensions.hall._write_dispatch_pending") as mock_write:
                    entry = {"id": "test123", "to": "dev", "dispatch": "auto"}
                    _auto_dispatch(entry)
                    mock_write.assert_not_called()  # Rate limited!


# ── 3. Tool dispatcher (hall_tool) ─────────────────────────────────


class TestHallTool:
    """hall_tool() dispatches actions to the right handler."""

    def test_send_action(self, temp_hall):
        from tools.hall_tool import hall_tool
        with patch("tools.hall_tool._get_current_soul", return_value="dev"):
            result = hall_tool({"action": "send", "to": "pm", "subject": "Status", "body": "Done"})
        assert "✅" in result
        assert "dev → pm" in result

    def test_send_without_soul_fails(self, temp_hall):
        from tools.hall_tool import hall_tool
        with patch("tools.hall_tool._get_current_soul", return_value=None):
            result = hall_tool({"action": "send", "to": "pm", "subject": "Status", "body": "Done"})
        assert "❌" in result

    def test_send_without_body_fails(self, temp_hall):
        from tools.hall_tool import hall_tool
        with patch("tools.hall_tool._get_current_soul", return_value="dev"):
            result = hall_tool({"action": "send", "to": "pm", "subject": "Status"})
        assert "❌" in result

    def test_read_action(self, temp_hall):
        from tools.hall_tool import hall_tool
        from gateway.extensions.hall import hall_send
        hall_send("pm", "dev", "Task", "Do it")
        with patch("tools.hall_tool._get_current_soul", return_value="dev"):
            result = hall_tool({"action": "read"})
        assert "📬" in result or "1 unread" in result

    def test_list_action(self, temp_hall):
        from tools.hall_tool import hall_tool
        from gateway.extensions.hall import hall_send
        hall_send("pm", "dev", "Task", "Body")
        result = hall_tool({"action": "list"})
        assert "📋" in result or "Recent" in result

    def test_unknown_action(self, temp_hall):
        from tools.hall_tool import hall_tool
        result = hall_tool({"action": "teleport"})
        assert "❌" in result
        assert "Unknown" in result

    def test_mark_read_action(self, temp_hall):
        from tools.hall_tool import hall_tool
        from gateway.extensions.hall import hall_send
        entry = hall_send("pm", "dev", "Task", "Body")
        with patch("tools.hall_tool._get_current_soul", return_value="dev"):
            result = hall_tool({"action": "mark_read", "msg_id": entry["id"]})
        assert "✅" in result

    def test_clear_action(self, temp_hall):
        from tools.hall_tool import hall_tool
        result = hall_tool({"action": "clear", "older_than_days": 0})
        assert "🗑️" in result


# ── 4. Thread safety ───────────────────────────────────────────────


class TestHallThreadSafety:
    """Concurrent writes should not corrupt hall.jsonl."""

    def test_concurrent_sends(self, temp_hall):
        """Multiple hall_send calls produce valid JSONL."""
        from gateway.extensions.hall import hall_send
        import threading

        results = []
        errors = []

        def send_msg(i):
            try:
                entry = hall_send(f"soul_{i}", "dev", f"Msg {i}", f"Body {i}")
                results.append(entry)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=send_msg, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 20

        # Verify JSONL is valid
        from gateway.extensions.hall import _read_all_entries
        entries = _read_all_entries()
        assert len(entries) == 20
        for entry in entries:
            assert "id" in entry
            assert "from" in entry
