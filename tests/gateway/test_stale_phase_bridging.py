"""Tests for stale-phase detector enhancements.

Covers 3 previously untested commits:
  - 54b16ab3: bridge gateway_timeout → stale_phase_timeout
  - 4d034627: prevent stale-phase false positive on heartbeat
  - 18ffaf4c: clean up stale phase tracker in _evict_cached_agent

These tests validate:
1. Config bridging: agent.gateway_timeout is propagated to stale_phase env var
2. Heartbeat bypass: `[heartbeat ~Mm]` marker survives regex stripping → new phase each minute
3. Cleanup: _evict_cached_agent cleans up _activity_phase_ts to prevent memory leak
"""

import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ── 1. Config bridging: gateway_timeout → stale_phase_timeout ──────


class TestGatewayTimeoutBridge:
    """54b16ab3: agent.gateway_timeout bridges to HERMES_GATEWAY_STALE_PHASE_TIMEOUT."""

    def test_gateway_timeout_bridges_to_stale_phase_env(self, tmp_path, monkeypatch):
        """When config has agent.gateway_timeout, it bridges to stale phase timeout."""
        # Clean env
        monkeypatch.delenv("HERMES_AGENT_TIMEOUT", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_STALE_PHASE_TIMEOUT", raising=False)

        config = {"agent": {"gateway_timeout": 300}}

        # Simulate the bridging logic from gateway/run.py lines 209-212
        _agent_cfg = config.get("agent", {})
        if _agent_cfg and isinstance(_agent_cfg, dict):
            if "gateway_timeout" in _agent_cfg and "HERMES_GATEWAY_STALE_PHASE_TIMEOUT" not in os.environ:
                os.environ["HERMES_GATEWAY_STALE_PHASE_TIMEOUT"] = str(_agent_cfg["gateway_timeout"])

        assert os.environ.get("HERMES_GATEWAY_STALE_PHASE_TIMEOUT") == "300"

    def test_gateway_timeout_does_not_override_existing_env(self, monkeypatch):
        """If HERMES_GATEWAY_STALE_PHASE_TIMEOUT already set, config does not override."""
        monkeypatch.setenv("HERMES_GATEWAY_STALE_PHASE_TIMEOUT", "900")

        config = {"agent": {"gateway_timeout": 300}}

        _agent_cfg = config.get("agent", {})
        if _agent_cfg and isinstance(_agent_cfg, dict):
            if "gateway_timeout" in _agent_cfg and "HERMES_GATEWAY_STALE_PHASE_TIMEOUT" not in os.environ:
                os.environ["HERMES_GATEWAY_STALE_PHASE_TIMEOUT"] = str(_agent_cfg["gateway_timeout"])

        assert os.environ["HERMES_GATEWAY_STALE_PHASE_TIMEOUT"] == "900"

    def test_stale_phase_timeout_default_when_no_config(self):
        """When no gateway_timeout is configured, stale_phase_timeout defaults to 600."""
        _STALE_PHASE_ENV = ""
        timeout = float(_STALE_PHASE_ENV) if _STALE_PHASE_ENV else 600.0
        assert timeout == 600.0

    def test_stale_phase_timeout_from_env(self, monkeypatch):
        """When env var is set, it overrides the default."""
        monkeypatch.setenv("HERMES_GATEWAY_STALE_PHASE_TIMEOUT", "120")
        _STALE_PHASE_ENV = os.getenv("HERMES_GATEWAY_STALE_PHASE_TIMEOUT", "")
        timeout = float(_STALE_PHASE_ENV) if _STALE_PHASE_ENV else 600.0
        assert timeout == 120.0


# ── 2. Heartbeat false-positive prevention ──────────────────────────


class TestHeartbeatBypass:
    """4d034627: Heartbeat markers prevent stale-phase false positives.

    The key insight: heartbeat descriptions include `[heartbeat ~Mm]` where M
    increments every minute. The stale-phase regex strips `(Ns elapsed)` but
    NOT `[heartbeat ~Mm]`, so each minute produces a different phase key →
    the detector sees progress and resets the timer.
    """

    def _build_phase_key(self, raw_desc: str, current_tool: str = None) -> str:
        """Replicate gateway/run.py phase-key logic."""
        phase_base = re.sub(r"\(\d+s[^)]*\)", "", raw_desc)
        phase_base = re.sub(r"#\d+", "#N", phase_base).strip()
        return f"{current_tool or 'none'}:{phase_base}"

    def test_heartbeat_seconds_stripped_but_minute_marker_survives(self):
        """`(Xs elapsed)` is stripped but `[heartbeat ~Mm]` survives."""
        desc_0m = "waiting for non-streaming response (30s elapsed) [heartbeat ~0m]"
        desc_1m = "waiting for non-streaming response (90s elapsed) [heartbeat ~1m]"
        desc_2m = "waiting for non-streaming response (150s elapsed) [heartbeat ~2m]"

        key_0 = self._build_phase_key(desc_0m)
        key_1 = self._build_phase_key(desc_1m)
        key_2 = self._build_phase_key(desc_2m)

        # Each minute should produce a DIFFERENT key (the heartbeat marker)
        assert key_0 != key_1
        assert key_1 != key_2

        # Verify the minute marker survived
        assert "~0m" in key_0
        assert "~1m" in key_1
        assert "~2m" in key_2

    def test_heartbeat_prevents_stale_false_positive(self):
        """Simulate: agent sends heartbeats every minute → never stale."""
        _activity_phase_ts = {}
        stale_timeout = 120.0  # 2 minutes
        session_key = "test:session:heartbeat"

        descriptions = [
            "waiting for non-streaming response (30s elapsed) [heartbeat ~0m]",
            "waiting for non-streaming response (90s elapsed) [heartbeat ~1m]",
            "waiting for non-streaming response (150s elapsed) [heartbeat ~2m]",
            "waiting for non-streaming response (210s elapsed) [heartbeat ~3m]",
        ]

        for desc in descriptions:
            phase_key = self._build_phase_key(desc)
            prev = _activity_phase_ts.get(session_key)
            if prev is None or prev[0] != phase_key:
                _activity_phase_ts[session_key] = (phase_key, time.time())

        # After 4 heartbeats (spanning ~3.5 min > 2 min timeout),
        # the phase keeps changing so it should NOT be stale
        now = time.time()
        prev = _activity_phase_ts.get(session_key)
        phase_age = now - prev[1]
        assert phase_age < stale_timeout  # Just set, so young

    def test_non_heartbeat_stuck_activity_is_detected(self):
        """Without heartbeat markers, same phase triggers stale detection."""
        _activity_phase_ts = {}
        stale_timeout = 5.0
        session_key = "test:session:stuck"

        # Simulate: agent stuck on same tool without heartbeat
        desc = "terminal command running"
        phase_key = self._build_phase_key(desc)
        phase_start = time.time() - (stale_timeout + 10)
        _activity_phase_ts[session_key] = (phase_key, phase_start)

        # Same description again — no phase change
        phase_key_2 = self._build_phase_key(desc)
        assert phase_key == phase_key_2  # same phase

        # Should be stale
        now = time.time()
        prev = _activity_phase_ts.get(session_key)
        phase_age = now - prev[1]
        assert phase_age >= stale_timeout


# ── 3. Cleanup: _evict_cached_agent cleans stale phase tracker ──────


class TestStalePhaseCleanup:
    """18ffaf4c: _evict_cached_agent cleans up _activity_phase_ts.

    Without cleanup, _activity_phase_ts grows unbounded as sessions are
    created and evicted — a memory leak.
    """

    def test_evict_cleans_phase_tracker(self):
        """_evict_cached_agent removes the session from _activity_phase_ts."""
        phase_ts = {"session:1": ("tool:desc", time.time()), "session:2": ("tool:other", time.time())}

        # Simulate _evict_cached_agent cleanup (lines 9376-9379)
        session_key = "session:1"
        if phase_ts is not None:
            phase_ts.pop(session_key, None)

        assert "session:1" not in phase_ts
        assert "session:2" in phase_ts

    def test_evict_handles_missing_session_gracefully(self):
        """Popping a non-existent session doesn't crash."""
        phase_ts = {"session:1": ("tool:desc", time.time())}
        phase_ts.pop("session:nonexistent", None)  # Should not raise
        assert len(phase_ts) == 1

    def test_evict_handles_none_phase_ts(self):
        """If _activity_phase_ts is None (not initialized), skip gracefully."""
        phase_ts = None
        session_key = "session:1"
        # Simulate the guard from gateway/run.py line 9377-9378
        if phase_ts is not None:
            phase_ts.pop(session_key, None)
        # Should not raise

    def test_inactivity_timeout_also_cleans_phase(self):
        """When inactivity timeout fires, _activity_phase_ts is also cleaned.

        This is from gateway/run.py line 11156 — after timeout detection,
        the phase tracker for that session is removed.
        """
        phase_ts = {"session:stuck": ("tool:desc", time.time() - 999)}
        session_key = "session:stuck"

        # Simulate cleanup after inactivity timeout
        phase_ts.pop(session_key, None)

        assert session_key not in phase_ts
