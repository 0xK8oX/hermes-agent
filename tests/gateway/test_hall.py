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


# ── 4. Retry / Backoff / DLQ ───────────────────────────────────────


class TestWriteDispatchPending:
    """Verify atomic write and wrapped payload format."""

    def test_writes_wrapped_payload_with_meta(self, temp_hall, tmp_path):
        from gateway.extensions.hall import _write_dispatch_pending, _dispatch_dir
        entry = {"id": "abc123", "to": "dev", "from": "pm"}
        _write_dispatch_pending(entry)

        dispatch_dir = _dispatch_dir()
        files = list(dispatch_dir.glob("*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["entry"] == entry
        assert payload["_meta"]["retries"] == 0
        assert isinstance(payload["_meta"]["next_retry"], (int, float))

    def test_uses_atomic_temp_file(self, temp_hall, tmp_path):
        from gateway.extensions.hall import _write_dispatch_pending, _dispatch_dir
        entry = {"id": "xyz789", "to": "dev", "from": "pm"}
        _write_dispatch_pending(entry)

        dispatch_dir = _dispatch_dir()
        # No temp files should remain
        temp_files = list(dispatch_dir.glob(".*.tmp"))
        assert temp_files == []


class TestDispatchWatcherRetryBackoff:
    """Verify exponential backoff, DLQ, and retry metadata rewrite."""

    @pytest.mark.asyncio
    async def test_skips_file_before_next_retry(self, temp_hall, tmp_path):
        from gateway.extensions.hall import start_hall_dispatch_watcher, _dispatch_dir
        dispatch_dir = _dispatch_dir()
        # Write a file with next_retry in the future
        future_retry = time.time() + 3600
        payload = {
            "entry": {"id": "r1", "to": "dev", "from": "pm"},
            "_meta": {"retries": 1, "next_retry": future_retry},
        }
        pending = dispatch_dir / "r1.json"
        pending.write_text(json.dumps(payload), encoding="utf-8")

        call_count = [0]

        def running():
            call_count[0] += 1
            return call_count[0] <= 2

        with patch("gateway.extensions.hall._lookup_soul_channel") as mock_lookup:
            await start_hall_dispatch_watcher(running, interval=1)
            mock_lookup.assert_not_called()

        # File should still exist (not processed, not deleted)
        assert pending.exists()

    @pytest.mark.asyncio
    async def test_moves_to_dlq_after_max_retries(self, temp_hall, tmp_path):
        from gateway.extensions.hall import start_hall_dispatch_watcher, _dispatch_dir, _failed_dispatch_dir, _MAX_DISPATCH_RETRIES
        dispatch_dir = _dispatch_dir()
        payload = {
            "entry": {"id": "dlq1", "to": "dev", "from": "pm"},
            "_meta": {"retries": _MAX_DISPATCH_RETRIES, "next_retry": 0},
        }
        pending = dispatch_dir / "dlq1.json"
        pending.write_text(json.dumps(payload), encoding="utf-8")

        call_count = [0]

        def running():
            call_count[0] += 1
            return call_count[0] <= 2

        await start_hall_dispatch_watcher(running, interval=1)

        assert not pending.exists()
        failed_path = _failed_dispatch_dir() / "dlq1.json"
        assert failed_path.exists()

    @pytest.mark.asyncio
    async def test_success_removes_pending_file(self, temp_hall, tmp_path):
        from gateway.extensions.hall import start_hall_dispatch_watcher, _dispatch_dir
        dispatch_dir = _dispatch_dir()
        payload = {
            "entry": {"id": "ok1", "to": "dev", "from": "pm"},
            "_meta": {"retries": 0, "next_retry": 0},
        }
        pending = dispatch_dir / "ok1.json"
        pending.write_text(json.dumps(payload), encoding="utf-8")

        call_count = [0]

        def running():
            call_count[0] += 1
            return call_count[0] <= 2

        with patch("gateway.extensions.hall._lookup_soul_channel", return_value={"platform": "telegram", "chat_id": "123"}):
            with patch("gateway.extensions.hall._execute_dispatch") as mock_exec:
                await start_hall_dispatch_watcher(running, interval=1)
                mock_exec.assert_called_once()

        assert not pending.exists()

    @pytest.mark.asyncio
    async def test_failure_rewrites_retry_metadata(self, temp_hall, tmp_path):
        from gateway.extensions.hall import start_hall_dispatch_watcher, _dispatch_dir
        dispatch_dir = _dispatch_dir()
        payload = {
            "entry": {"id": "fail1", "to": "dev", "from": "pm"},
            "_meta": {"retries": 0, "next_retry": 0},
        }
        pending = dispatch_dir / "fail1.json"
        pending.write_text(json.dumps(payload), encoding="utf-8")

        call_count = [0]

        def running():
            call_count[0] += 1
            return call_count[0] <= 2

        with patch("gateway.extensions.hall._lookup_soul_channel", return_value={"platform": "telegram", "chat_id": "123"}):
            with patch("gateway.extensions.hall._execute_dispatch", side_effect=RuntimeError("boom")):
                await start_hall_dispatch_watcher(running, interval=1)

        # File should still exist with updated metadata
        assert pending.exists()
        updated = json.loads(pending.read_text(encoding="utf-8"))
        assert updated["_meta"]["retries"] == 1
        assert updated["_meta"]["next_retry"] > time.time()  # future
        assert "boom" in updated["_meta"]["last_error"]

    @pytest.mark.asyncio
    async def test_backoff_interval_doubles(self, temp_hall, tmp_path):
        from gateway.extensions.hall import start_hall_dispatch_watcher, _dispatch_dir
        dispatch_dir = _dispatch_dir()
        # Start with retries=2, expect next_retry = now + 5 * 2^2 = now + 20
        now = time.time()
        payload = {
            "entry": {"id": "back1", "to": "dev", "from": "pm"},
            "_meta": {"retries": 2, "next_retry": 0},
        }
        pending = dispatch_dir / "back1.json"
        pending.write_text(json.dumps(payload), encoding="utf-8")

        call_count = [0]

        def running():
            call_count[0] += 1
            return call_count[0] <= 2

        with patch("gateway.extensions.hall._lookup_soul_channel", return_value={"platform": "telegram", "chat_id": "123"}):
            with patch("gateway.extensions.hall._execute_dispatch", side_effect=RuntimeError("boom")):
                await start_hall_dispatch_watcher(running, interval=1)

        updated = json.loads(pending.read_text(encoding="utf-8"))
        assert updated["_meta"]["retries"] == 3
        # Backoff = 5 * 2^(3-1) = 20 seconds
        expected_min = now + 15
        assert updated["_meta"]["next_retry"] >= expected_min

    @pytest.mark.asyncio
    async def test_discards_corrupted_json(self, temp_hall, tmp_path):
        from gateway.extensions.hall import start_hall_dispatch_watcher, _dispatch_dir
        dispatch_dir = _dispatch_dir()
        pending = dispatch_dir / "bad.json"
        pending.write_text("not json at all", encoding="utf-8")

        call_count = [0]

        def running():
            call_count[0] += 1
            return call_count[0] <= 2

        await start_hall_dispatch_watcher(running, interval=1)
        assert not pending.exists()

    @pytest.mark.asyncio
    async def test_discards_missing_channel(self, temp_hall, tmp_path):
        from gateway.extensions.hall import start_hall_dispatch_watcher, _dispatch_dir
        dispatch_dir = _dispatch_dir()
        payload = {
            "entry": {"id": "nochan", "to": "ghost", "from": "pm"},
            "_meta": {"retries": 0, "next_retry": 0},
        }
        pending = dispatch_dir / "nochan.json"
        pending.write_text(json.dumps(payload), encoding="utf-8")

        call_count = [0]

        def running():
            call_count[0] += 1
            return call_count[0] <= 2

        with patch("gateway.extensions.hall._lookup_soul_channel", return_value=None):
            await start_hall_dispatch_watcher(running, interval=1)

        assert not pending.exists()

    @pytest.mark.asyncio
    async def test_skips_temp_files(self, temp_hall, tmp_path):
        from gateway.extensions.hall import start_hall_dispatch_watcher, _dispatch_dir
        dispatch_dir = _dispatch_dir()
        temp_file = dispatch_dir / ".something.json.tmp"
        temp_file.write_text("{}", encoding="utf-8")

        call_count = [0]

        def running():
            call_count[0] += 1
            return call_count[0] <= 2

        await start_hall_dispatch_watcher(running, interval=1)
        # Temp file should still exist (not processed, not deleted)
        assert temp_file.exists()

    @pytest.mark.asyncio
    async def test_supports_legacy_bare_entry_format(self, temp_hall, tmp_path):
        from gateway.extensions.hall import start_hall_dispatch_watcher, _dispatch_dir
        dispatch_dir = _dispatch_dir()
        # Legacy format: just the entry dict, no wrapper
        pending = dispatch_dir / "legacy.json"
        pending.write_text(json.dumps({"id": "legacy", "to": "dev", "from": "pm"}), encoding="utf-8")

        call_count = [0]

        def running():
            call_count[0] += 1
            return call_count[0] <= 2

        with patch("gateway.extensions.hall._lookup_soul_channel", return_value={"platform": "telegram", "chat_id": "123"}):
            with patch("gateway.extensions.hall._execute_dispatch") as mock_exec:
                await start_hall_dispatch_watcher(running, interval=1)
                mock_exec.assert_called_once()

        assert not pending.exists()


# ── 5. Hook functions ──────────────────────────────────────────────


class TestHookFunctions:
    """_get_ephemeral and _on_session_cleanup are called by run.py via hooks."""

    def test_get_ephemeral_returns_none_when_no_soul(self, temp_hall):
        from gateway.extensions.hall import _get_ephemeral
        assert _get_ephemeral("some_session") is None

    def test_get_ephemeral_returns_none_when_no_unread(self, temp_hall):
        from gateway.extensions.hall import _get_ephemeral
        with patch("gateway.extensions.hall._get_soul_name", return_value="dev"):
            assert _get_ephemeral("session-123") is None

    def test_get_ephemeral_injects_unread_messages(self, temp_hall):
        from gateway.extensions.hall import _get_ephemeral, hall_send
        hall_send("pm", "dev", "Task", "Do this")
        with patch("gateway.extensions.hall._get_soul_name", return_value="dev"):
            result = _get_ephemeral("session-123")
        assert result is not None
        assert "Hall" in result
        assert "pm" in result
        assert "Task" in result

    def test_get_ephemeral_marks_as_read(self, temp_hall):
        from gateway.extensions.hall import _get_ephemeral, hall_send, hall_read
        hall_send("pm", "dev", "Task", "Do this")
        with patch("gateway.extensions.hall._get_soul_name", return_value="dev"):
            _get_ephemeral("session-123")
        # Should be marked as read
        assert hall_read("dev") == []

    def test_get_ephemeral_includes_high_priority_badge(self, temp_hall):
        from gateway.extensions.hall import _get_ephemeral, hall_send
        hall_send("pm", "dev", "Urgent", "Do this", priority="high")
        with patch("gateway.extensions.hall._get_soul_name", return_value="dev"):
            result = _get_ephemeral("session-123")
        assert "🔴" in result

    def test_on_session_cleanup_calls_hall_clear(self, temp_hall):
        from gateway.extensions.hall import _on_session_cleanup, hall_send, hall_list
        hall_send("pm", "dev", "Old", "Body")
        # _on_session_cleanup calls hall_clear(older_than_days=30)
        # so the message won't be cleared unless it's >30 days old.
        # Just verify it doesn't raise and the function was called.
        _on_session_cleanup("session-123")
        # Message still there (not old enough)
        assert len(hall_list()) == 1


class TestAdminSoulCheck:
    """_is_admin_soul validates admin status from bindings or legacy names."""

    def test_alpha_is_admin_legacy(self, temp_hall):
        from gateway.extensions.hall import _is_admin_soul
        assert _is_admin_soul("alpha") is True
        assert _is_admin_soul("ALPHA") is True

    def test_pm_is_admin_legacy(self, temp_hall):
        from gateway.extensions.hall import _is_admin_soul
        assert _is_admin_soul("pm") is True

    def test_regular_soul_is_not_admin(self, temp_hall):
        from gateway.extensions.hall import _is_admin_soul
        assert _is_admin_soul("dev") is False

    def test_empty_soul_is_not_admin(self, temp_hall):
        from gateway.extensions.hall import _is_admin_soul
        assert _is_admin_soul("") is False

    def test_admin_from_env_and_binding(self, temp_hall):
        from gateway.extensions.hall import _is_admin_soul
        with patch.dict("os.environ", {"GATEWAY_ADMIN_USERS": "12345"}):
            with patch("gateway.extensions.hall._lookup_soul_channel", return_value={"platform": "telegram", "chat_id": "123"}):
                with patch("gateway.extensions.channel_binding._get_all_platform_bindings", return_value={
                    "telegram": [{"soul": "dev", "user_id": "12345", "id": "123"}]
                }):
                    assert _is_admin_soul("dev") is True

    def test_non_admin_binding_user_id(self, temp_hall):
        from gateway.extensions.hall import _is_admin_soul
        with patch.dict("os.environ", {"GATEWAY_ADMIN_USERS": "99999"}):
            with patch("gateway.extensions.hall._lookup_soul_channel", return_value={"platform": "telegram", "chat_id": "123"}):
                with patch("gateway.extensions.channel_binding._get_all_platform_bindings", return_value={
                    "telegram": [{"soul": "dev", "user_id": "12345", "id": "123"}]
                }):
                    assert _is_admin_soul("dev") is False


class TestSoulResolution:
    """_get_soul_name and _lookup_soul_channel resolution."""

    def test_get_soul_name_from_binding_state(self, temp_hall):
        from gateway.extensions.hall import _get_soul_name
        with patch("gateway.extensions.channel_binding._session_soul_names", {"sess-1": "pm"}):
            assert _get_soul_name("sess-1") == "pm"

    def test_get_soul_name_returns_none_when_no_binding(self, temp_hall):
        from gateway.extensions.hall import _get_soul_name
        with patch("gateway.extensions.channel_binding._session_soul_names", {}):
            assert _get_soul_name("sess-1") is None

    def test_get_soul_name_survives_import_error(self, temp_hall):
        from gateway.extensions.hall import _get_soul_name
        with patch("gateway.extensions.hall._get_soul_name", side_effect=ImportError("no module")):
            # Test the actual fallback by calling a wrapper
            pass
        # Just verify the real function handles missing module gracefully
        with patch.dict("sys.modules", {"gateway.extensions.channel_binding": None}):
            assert _get_soul_name("sess-1") is None

    def test_lookup_soul_channel_finds_match(self, temp_hall):
        from gateway.extensions.hall import _lookup_soul_channel
        with patch("gateway.extensions.channel_binding._get_all_platform_bindings", return_value={
            "telegram": [{"soul": "dev", "id": "123"}]
        }):
            result = _lookup_soul_channel("dev")
        assert result == {"platform": "telegram", "chat_id": "123"}

    def test_lookup_soul_channel_normalizes_case(self, temp_hall):
        from gateway.extensions.hall import _lookup_soul_channel
        with patch("gateway.extensions.channel_binding._get_all_platform_bindings", return_value={
            "telegram": [{"soul": "DEV", "id": "123"}]
        }):
            result = _lookup_soul_channel("dev")
        assert result == {"platform": "telegram", "chat_id": "123"}

    def test_lookup_soul_channel_returns_none_when_no_bindings(self, temp_hall):
        from gateway.extensions.hall import _lookup_soul_channel
        with patch("gateway.extensions.channel_binding._get_all_platform_bindings", return_value={}):
            assert _lookup_soul_channel("dev") is None

    def test_lookup_soul_channel_survives_exception(self, temp_hall):
        from gateway.extensions.hall import _lookup_soul_channel
        with patch("gateway.extensions.channel_binding._get_all_platform_bindings", side_effect=RuntimeError("boom")):
            assert _lookup_soul_channel("dev") is None

    def test_lookup_soul_channel_skips_non_dict_bindings(self, temp_hall):
        from gateway.extensions.hall import _lookup_soul_channel
        with patch("gateway.extensions.channel_binding._get_all_platform_bindings", return_value={
            "telegram": ["bad-binding", {"soul": "dev", "id": "123"}]
        }):
            result = _lookup_soul_channel("dev")
        assert result == {"platform": "telegram", "chat_id": "123"}


class TestHallClearWithSoulFilter:
    """hall_clear with soul filter purges only matching messages."""

    def test_clear_with_soul_filter(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_clear, hall_list
        hall_send("pm", "dev", "Dev msg", "Body")
        hall_send("pm", "ops", "Ops msg", "Body")

        removed = hall_clear(soul="dev", older_than_days=0)
        assert removed == 1

        remaining = hall_list()
        assert len(remaining) == 1
        assert remaining[0]["to"] == "ops"

    def test_clear_with_soul_keeps_broadcast(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_clear, hall_list
        hall_send("pm", "all", "Broadcast", "Body")
        hall_send("pm", "dev", "Dev msg", "Body")

        removed = hall_clear(soul="dev", older_than_days=0)
        # broadcast "all" matches dev too, so it should also be removed
        assert removed == 2

    def test_clear_invalid_timestamp_defaults_to_epoch(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_clear, hall_list
        # Create a message with invalid timestamp
        entry = hall_send("pm", "dev", "Bad ts", "Body")
        entry["ts"] = "invalid-timestamp"
        # Rewrite the file with bad timestamp
        from gateway.extensions.hall import _write_all_entries, _read_all_entries
        entries = _read_all_entries()
        for e in entries:
            if e["id"] == entry["id"]:
                e["ts"] = "invalid-timestamp"
        _write_all_entries(entries)

        removed = hall_clear(older_than_days=30)
        # Invalid timestamp defaults to epoch 0, which is very old
        assert removed >= 1


class TestHallMarkReadEdgeCases:
    """Edge cases for hall_mark_read."""

    def test_mark_read_already_read(self, temp_hall):
        from gateway.extensions.hall import hall_send, hall_mark_read, hall_read
        entry = hall_send("pm", "dev", "Task", "Body")
        hall_mark_read(entry["id"], "dev")
        # Second mark returns False because soul already in read_by
        result = hall_mark_read(entry["id"], "dev")
        assert result is False

    def test_mark_read_empty_inputs(self, temp_hall):
        from gateway.extensions.hall import hall_mark_read
        assert hall_mark_read("", "dev") is False
        assert hall_mark_read("id", "") is False


# ── 6. Thread safety ───────────────────────────────────────────────


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
