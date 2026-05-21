"""Tests for the stale-phase detector in gateway agent runs.

The stale-phase detector catches agents that update _touch_activity()
periodically (heartbeats) but make no real progress.  It strips timing
suffixes from activity descriptions and fires when the same "phase"
persists beyond the configured threshold.

Key behaviours tested:
- Phase key normalisation strips "(Ns elapsed)" and "(Ns, ...)" patterns
- Phase change resets the stale timer
- Stale timeout fires when the same phase persists too long
- Heartbeat descriptions with changing seconds don't prevent detection
"""

import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ── Phase-key normalisation ──────────────────────────────────────────


def _build_phase_key(raw_desc: str, current_tool: str = None) -> str:
    """Replicate the phase-key logic from gateway/run.py."""
    phase_base = re.sub(r"\(\d+s[^)]*\)", "", raw_desc)
    phase_base = re.sub(r"#\d+", "#N", phase_base).strip()
    return f"{current_tool or 'none'}:{phase_base}"


class TestPhaseKeyNormalization:
    """Verify that the regex strips timing patterns from activity descriptions."""

    def test_elapsed_seconds(self):
        assert _build_phase_key("terminal command running (10s elapsed)") == \
            "none:terminal command running"

    def test_no_chunks_yet(self):
        """Heartbeat writes 'waiting for stream response (30s, no chunks yet)'."""
        assert _build_phase_key("waiting for stream response (30s, no chunks yet)") == \
            "none:waiting for stream response"

    def test_large_seconds_no_chunks(self):
        """Heartbeat at 180s should normalise to same key as 30s."""
        key_30 = _build_phase_key("waiting for stream response (30s, no chunks yet)")
        key_180 = _build_phase_key("waiting for stream response (180s, no chunks yet)")
        key_600 = _build_phase_key("waiting for stream response (600s, no chunks yet)")
        assert key_30 == key_180 == key_600

    def test_receiving_stream_response(self):
        assert _build_phase_key("receiving stream response") == \
            "none:receiving stream response"

    def test_stream_retry_counter(self):
        key_1 = _build_phase_key("stream retry 1/3 after ConnectionError")
        key_2 = _build_phase_key("stream retry 2/3 after ConnectionError")
        # Counter after "retry" is not a #N pattern — different keys expected
        assert key_1 != key_2

    def test_hash_counter(self):
        key_1 = _build_phase_key("processing file #1")
        key_2 = _build_phase_key("processing file #5")
        key_99 = _build_phase_key("processing file #99")
        assert key_1 == key_2 == key_99 == "none:processing file #N"

    def test_non_streaming(self):
        assert _build_phase_key("waiting for non-streaming API response") == \
            "none:waiting for non-streaming API response"

    def test_with_tool_name(self):
        key = _build_phase_key("waiting for stream response (30s, no chunks yet)", "browser")
        assert key == "browser:waiting for stream response"

    def test_mixed_patterns(self):
        """Both (Ns...) and #N in same string."""
        key = _build_phase_key("retry #3 (15s elapsed)")
        assert key == "none:retry #N"


# ── Stale-phase detection logic ──────────────────────────────────────


class TestStalePhaseDetection:
    """Simulate the stale-phase detection loop from gateway/run.py."""

    def test_phase_change_resets_timer(self):
        """When the phase changes, the stale timer should reset."""
        _activity_phase_ts = {}
        stale_timeout = 10.0
        session_key = "test:session:1"

        # Phase 1: waiting for stream
        _phase_key_1 = _build_phase_key("waiting for stream response (5s, no chunks yet)")
        _activity_phase_ts[session_key] = (_phase_key_1, time.time())

        # Phase changes to receiving — timer resets
        _phase_key_2 = _build_phase_key("receiving stream response")
        assert _phase_key_1 != _phase_key_2

        prev = _activity_phase_ts.get(session_key)
        assert prev[0] != _phase_key_2  # phase changed
        _activity_phase_ts[session_key] = (_phase_key_2, time.time())

        # Should not be stale immediately after phase change
        now = time.time()
        phase_age = now - _activity_phase_ts[session_key][1]
        assert phase_age < stale_timeout

    def test_heartbeat_does_not_prevent_detection(self):
        """Heartbeat with changing seconds should still produce the same phase key."""
        descriptions = [
            "waiting for stream response (0s, no chunks yet)",
            "waiting for stream response (30s, no chunks yet)",
            "waiting for stream response (60s, no chunks yet)",
            "waiting for stream response (120s, no chunks yet)",
            "waiting for stream response (180s, no chunks yet)",
            "waiting for stream response (300s, no chunks yet)",
        ]

        keys = [_build_phase_key(d) for d in descriptions]
        # All should be the same — heartbeat seconds stripped
        assert len(set(keys)) == 1, f"Expected 1 unique key, got {set(keys)}"
        assert keys[0] == "none:waiting for stream response"

    def test_stale_triggers_after_timeout(self):
        """Simulate: same phase for longer than stale_timeout → triggers."""
        stale_timeout = 5.0
        session_key = "test:session:2"
        _activity_phase_ts = {}

        # Set initial phase
        phase_key = _build_phase_key("waiting for stream response (0s, no chunks yet)")
        phase_start = time.time() - (stale_timeout + 1)  # started >threshold ago
        _activity_phase_ts[session_key] = (phase_key, phase_start)

        # Simulate poll — phase unchanged, age exceeds threshold
        now = time.time()
        prev = _activity_phase_ts.get(session_key)
        assert prev is not None
        assert prev[0] == phase_key  # same phase

        phase_age = now - prev[1]
        assert phase_age >= stale_timeout  # triggers!
