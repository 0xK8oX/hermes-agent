"""Tests for session_checkpoint extension."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Setup — ensure imports work
# ---------------------------------------------------------------------------

HERMES_HOME = Path.home() / ".hermes"
REPO = HERMES_HOME / "hermes-agent"
sys.path.insert(0, str(REPO))
os.chdir(REPO)

# Clear extension state between tests
_orig_hooks = None


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """Provide clean state for every test."""
    global _orig_hooks

    # Redirect checkpoints dir to temp
    cp_dir = tmp_path / "checkpoints"
    monkeypatch.setattr(
        "gateway.extensions.session_checkpoint._get_checkpoints_dir",
        lambda: cp_dir,
    )

    # Clear in-memory cache and hook registry for clean state
    import gateway.extensions.session_checkpoint as scm
    scm._cache.clear()

    # Save and restore hooks
    from gateway.extensions import _HOOKS
    saved = {k: list(v) for k, v in _HOOKS.items()}

    yield

    # Restore hooks
    _HOOKS.clear()
    _HOOKS.update(saved)
    scm._cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sk(platform="telegram", channel_id="12345"):
    """Build a session key."""
    return f"agent:main:{platform}:dm:{channel_id}"


# ---------------------------------------------------------------------------
# Tests — _parse_session_key
# ---------------------------------------------------------------------------

class TestParseSessionKey:
    def test_standard_dm(self):
        from gateway.extensions.session_checkpoint import _parse_session_key
        r = _parse_session_key("agent:main:telegram:dm:6911288694")
        assert r["platform"] == "telegram"
        assert r["channel_id"] == "6911288694"

    def test_group_with_thread(self):
        from gateway.extensions.session_checkpoint import _parse_session_key
        r = _parse_session_key("agent:main:discord:group:1493257325866385480:999")
        assert r["platform"] == "discord"
        assert r["channel_id"] == "1493257325866385480"

    def test_short_key(self):
        from gateway.extensions.session_checkpoint import _parse_session_key
        r = _parse_session_key("short:key")
        assert r["platform"] == "unknown"
        assert r["channel_id"] == "unknown"


# ---------------------------------------------------------------------------
# Tests — save + load round-trip
# ---------------------------------------------------------------------------

class TestSaveAndLoad:
    def test_save_creates_file(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_save_checkpoint
        sk = _sk()
        _on_save_checkpoint(sk, "sess-1", "hello", "hi there")

        cp_file = tmp_path / "checkpoints" / "telegram" / "12345.json"
        assert cp_file.exists()

    def test_save_then_load(self, tmp_path):
        from gateway.extensions.session_checkpoint import (
            _on_save_checkpoint,
            _on_get_checkpoint,
        )
        sk = _sk()
        _on_save_checkpoint(sk, "sess-1", "what were we doing?", "building the app")
        result = _on_get_checkpoint(sk)
        assert result is not None
        assert "Session Checkpoint" in result
        assert "what were we doing?" in result

    def test_load_nonexistent_returns_none(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_get_checkpoint
        result = _on_get_checkpoint(_sk())
        assert result is None

    def test_save_updates_existing(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_save_checkpoint
        sk = _sk()
        _on_save_checkpoint(sk, "sess-1", "topic A", "response A")
        _on_save_checkpoint(sk, "sess-1", "topic B", "response B")

        cp_file = tmp_path / "checkpoints" / "telegram" / "12345.json"
        data = json.loads(cp_file.read_text())
        # Should have both topics
        assert "topic A" in data["topics"]
        assert "topic B" in data["topics"]

    def test_empty_session_key_noop(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_save_checkpoint
        _on_save_checkpoint("", "sess", "msg", "resp")
        # No file should be created
        assert not (tmp_path / "checkpoints").exists() or \
            not list((tmp_path / "checkpoints").rglob("*.json"))


# ---------------------------------------------------------------------------
# Tests — TTL expiry
# ---------------------------------------------------------------------------

class TestTTL:
    def test_expired_checkpoint_not_loaded(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_save_checkpoint, _on_get_checkpoint
        sk = _sk()
        _on_save_checkpoint(sk, "sess-1", "hello", "hi")

        # Manually expire the checkpoint
        cp_file = tmp_path / "checkpoints" / "telegram" / "12345.json"
        data = json.loads(cp_file.read_text())
        data["timestamp"] = time.time() - 25 * 3600  # 25h ago
        cp_file.write_text(json.dumps(data))

        # Clear cache to force disk read
        import gateway.extensions.session_checkpoint as scm
        scm._cache.clear()

        result = _on_get_checkpoint(sk)
        assert result is None

    def test_expired_file_removed(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_save_checkpoint, _on_get_checkpoint
        sk = _sk()
        _on_save_checkpoint(sk, "sess-1", "hello", "hi")

        cp_file = tmp_path / "checkpoints" / "telegram" / "12345.json"
        data = json.loads(cp_file.read_text())
        data["timestamp"] = time.time() - 25 * 3600
        cp_file.write_text(json.dumps(data))

        import gateway.extensions.session_checkpoint as scm
        scm._cache.clear()

        _on_get_checkpoint(sk)
        assert not cp_file.exists()


# ---------------------------------------------------------------------------
# Tests — cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_removes_file(self, tmp_path):
        from gateway.extensions.session_checkpoint import (
            _on_save_checkpoint,
            _on_session_cleanup,
        )
        sk = _sk()
        _on_save_checkpoint(sk, "sess-1", "msg", "resp")

        cp_file = tmp_path / "checkpoints" / "telegram" / "12345.json"
        assert cp_file.exists()

        _on_session_cleanup(sk)
        assert not cp_file.exists()

    def test_cleanup_clears_cache(self, tmp_path):
        from gateway.extensions.session_checkpoint import (
            _on_save_checkpoint,
            _on_session_cleanup,
            _cache,
        )
        sk = _sk()
        _on_save_checkpoint(sk, "sess-1", "msg", "resp")
        assert sk in _cache

        _on_session_cleanup(sk)
        assert sk not in _cache

    def test_cleanup_nonexistent_noop(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_session_cleanup
        _on_session_cleanup(_sk())  # Should not raise


# ---------------------------------------------------------------------------
# Tests — session reset clears checkpoint
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_clears_checkpoint(self, tmp_path):
        from gateway.extensions.session_checkpoint import (
            _on_save_checkpoint,
            _on_session_reset,
            _on_get_checkpoint,
        )
        sk = _sk()
        _on_save_checkpoint(sk, "sess-1", "msg", "resp")
        _on_session_reset(sk)
        result = _on_get_checkpoint(sk)
        assert result is None


# ---------------------------------------------------------------------------
# Tests — hook registration
# ---------------------------------------------------------------------------

class TestHookRegistration:
    def test_hooks_registered(self):
        from gateway.extensions import _HOOKS
        assert "save_checkpoint" in _HOOKS
        assert "get_checkpoint" in _HOOKS
        assert len(_HOOKS["save_checkpoint"]) >= 1
        assert len(_HOOKS["get_checkpoint"]) >= 1


# ---------------------------------------------------------------------------
# Tests — multi-channel isolation
# ---------------------------------------------------------------------------

class TestMultiChannel:
    def test_separate_channels_independent(self, tmp_path):
        from gateway.extensions.session_checkpoint import (
            _on_save_checkpoint,
            _on_get_checkpoint,
        )
        sk_telegram = _sk("telegram", "111")
        sk_discord = _sk("discord", "222")

        _on_save_checkpoint(sk_telegram, "s1", "telegram msg", "telegram resp")
        _on_save_checkpoint(sk_discord, "s2", "discord msg", "discord resp")

        t_result = _on_get_checkpoint(sk_telegram)
        d_result = _on_get_checkpoint(sk_discord)

        assert "telegram msg" in t_result
        assert "discord msg" in d_result
        assert "discord msg" not in t_result
        assert "telegram msg" not in d_result

    def test_cleanup_one_doesnt_affect_other(self, tmp_path):
        from gateway.extensions.session_checkpoint import (
            _on_save_checkpoint,
            _on_session_cleanup,
            _on_get_checkpoint,
        )
        sk1 = _sk("telegram", "111")
        sk2 = _sk("telegram", "222")

        _on_save_checkpoint(sk1, "s1", "msg1", "resp1")
        _on_save_checkpoint(sk2, "s2", "msg2", "resp2")

        _on_session_cleanup(sk1)

        assert _on_get_checkpoint(sk1) is None
        assert _on_get_checkpoint(sk2) is not None


# ---------------------------------------------------------------------------
# Tests — topic accumulation
# ---------------------------------------------------------------------------

class TestTopics:
    def test_topics_accumulate(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_save_checkpoint
        sk = _sk()

        for i in range(5):
            _on_save_checkpoint(sk, "sess", f"topic {i}", f"response {i}")

        import gateway.extensions.session_checkpoint as scm
        data = scm._cache[sk]
        assert len(data["topics"]) == 5
        assert "topic 0" in data["topics"]
        assert "topic 4" in data["topics"]

    def test_topics_capped_at_max(self, tmp_path):
        from gateway.extensions.session_checkpoint import (
            _on_save_checkpoint,
            _MAX_TOPICS,
        )
        sk = _sk()

        for i in range(_MAX_TOPICS + 5):
            _on_save_checkpoint(sk, "sess", f"topic {i}", f"response {i}")

        import gateway.extensions.session_checkpoint as scm
        data = scm._cache[sk]
        assert len(data["topics"]) <= _MAX_TOPICS


# ---------------------------------------------------------------------------
# Tests — checkpoint format
# ---------------------------------------------------------------------------

class TestCheckpointFormat:
    def test_output_format(self, tmp_path):
        from gateway.extensions.session_checkpoint import (
            _on_save_checkpoint,
            _on_get_checkpoint,
        )
        sk = _sk()
        _on_save_checkpoint(sk, "sess-1", "hello world", "hi there")

        result = _on_get_checkpoint(sk)
        assert "[Session Checkpoint" in result
        assert "Last active:" in result
        assert "hello world" in result
        assert "hi there" in result
        assert "continuity" in result.lower()

    def test_output_limited_length(self, tmp_path):
        from gateway.extensions.session_checkpoint import (
            _on_save_checkpoint,
            _on_get_checkpoint,
            _MAX_MESSAGE_PREVIEW,
        )
        sk = _sk()
        long_msg = "x" * 1000
        _on_save_checkpoint(sk, "sess-1", long_msg, long_msg)

        result = _on_get_checkpoint(sk)
        # Response should not contain the full 1000-char message
        assert len(result) < 1500


# ---------------------------------------------------------------------------
# Tests — error paths and edge cases
# ---------------------------------------------------------------------------

class TestErrorPaths:
    def test_load_corrupted_checkpoint_returns_none(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_get_checkpoint
        sk = _sk()
        cp_file = tmp_path / "checkpoints" / "telegram" / "12345.json"
        cp_file.parent.mkdir(parents=True, exist_ok=True)
        cp_file.write_text("not json", encoding="utf-8")

        import gateway.extensions.session_checkpoint as scm
        scm._cache.clear()

        result = _on_get_checkpoint(sk)
        assert result is None

    def test_save_disk_error_is_logged(self, tmp_path, caplog):
        from gateway.extensions.session_checkpoint import _on_save_checkpoint
        import logging
        sk = _sk()

        with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            with caplog.at_level(logging.WARNING):
                _on_save_checkpoint(sk, "sess", "msg", "resp")
        assert "disk full" in caplog.text or "save error" in caplog.text

    def test_get_checkpoint_empty_session_key(self):
        from gateway.extensions.session_checkpoint import _on_get_checkpoint
        assert _on_get_checkpoint("") is None

    def test_cleanup_empty_session_key(self):
        from gateway.extensions.session_checkpoint import _on_session_cleanup
        # Should not raise
        _on_session_cleanup("")

    def test_load_missing_checkpoint_returns_none(self, tmp_path):
        from gateway.extensions.session_checkpoint import _on_get_checkpoint
        import gateway.extensions.session_checkpoint as scm
        scm._cache.clear()
        assert _on_get_checkpoint(_sk()) is None


# ---------------------------------------------------------------------------
# Tests — _cleanup_expired
# ---------------------------------------------------------------------------

class TestCleanupExpired:
    def test_removes_expired_files(self, tmp_path):
        from gateway.extensions.session_checkpoint import _cleanup_expired
        sk = _sk()
        cp_file = tmp_path / "checkpoints" / "telegram" / "12345.json"
        cp_file.parent.mkdir(parents=True, exist_ok=True)
        cp_file.write_text(
            json.dumps({"timestamp": time.time() - 25 * 3600}),
            encoding="utf-8",
        )

        removed = _cleanup_expired()
        assert removed == 1
        assert not cp_file.exists()

    def test_keeps_fresh_files(self, tmp_path):
        from gateway.extensions.session_checkpoint import _cleanup_expired
        sk = _sk()
        cp_file = tmp_path / "checkpoints" / "telegram" / "12345.json"
        cp_file.parent.mkdir(parents=True, exist_ok=True)
        cp_file.write_text(
            json.dumps({"timestamp": time.time()}),
            encoding="utf-8",
        )

        removed = _cleanup_expired()
        assert removed == 0
        assert cp_file.exists()

    def test_removes_corrupted_files(self, tmp_path):
        from gateway.extensions.session_checkpoint import _cleanup_expired
        cp_file = tmp_path / "checkpoints" / "telegram" / "bad.json"
        cp_file.parent.mkdir(parents=True, exist_ok=True)
        cp_file.write_text("not json", encoding="utf-8")

        removed = _cleanup_expired()
        assert removed == 1
        assert not cp_file.exists()

    def test_noop_when_dir_missing(self, tmp_path):
        from gateway.extensions.session_checkpoint import _cleanup_expired
        # Ensure checkpoints dir doesn't exist
        with patch("gateway.extensions.session_checkpoint._get_checkpoints_dir", return_value=tmp_path / "nonexistent"):
            removed = _cleanup_expired()
        assert removed == 0
