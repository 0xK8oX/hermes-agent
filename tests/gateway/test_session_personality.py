"""Tests for session personality persistence and filtering.

Covers:
- Problem A: personality not persisted for existing sessions
- Problem B: _list_recent_sessions personality filter
"""
import json
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers: lightweight stand-ins to avoid importing the full gateway stack
# ---------------------------------------------------------------------------

from gateway.config import Platform
from hermes_state import SessionDB


def _make_db(tmpdir: Path) -> SessionDB:
    """Create a SessionDB backed by a temporary SQLite file."""
    db_path = tmpdir / "test_state.db"
    return SessionDB(db_path=db_path)


# ---------------------------------------------------------------------------
# Tests for Problem A: update_session_personality
# ---------------------------------------------------------------------------

class TestUpdateSessionPersonality:
    """Verify that SessionDB.update_session_personality works correctly."""

    def test_update_personality_on_existing_session(self, tmp_path):
        """Personality should be updatable for a session that already exists."""
        db = _make_db(tmp_path)
        sid = "20250101_000000_abcd1234"

        # Create a session WITHOUT personality
        db.create_session(
            session_id=sid,
            source="telegram",
            user_id="user1",
            personality=None,
        )

        # Verify personality is NULL
        row = db.get_session(sid)
        assert row is not None
        assert row.get("personality") is None

        # Update personality
        db.update_session_personality(sid, "catgirl")

        # Verify it was persisted
        row = db.get_session(sid)
        assert row["personality"] == "catgirl"

    def test_update_personality_overwrite(self, tmp_path):
        """Personality should be overwritable."""
        db = _make_db(tmp_path)
        sid = "20250101_000000_abcd5678"

        db.create_session(
            session_id=sid,
            source="telegram",
            user_id="user1",
            personality="catgirl",
        )

        row = db.get_session(sid)
        assert row["personality"] == "catgirl"

        db.update_session_personality(sid, "professional")
        row = db.get_session(sid)
        assert row["personality"] == "professional"

    def test_update_personality_nonexistent_session(self, tmp_path):
        """Updating a non-existent session should not raise."""
        db = _make_db(tmp_path)
        # Should not raise — SQLite UPDATE on 0 rows is a no-op
        db.update_session_personality("nonexistent_id", "catgirl")


# ---------------------------------------------------------------------------
# Tests for Problem B: _list_recent_sessions personality filter
# ---------------------------------------------------------------------------

class TestListRecentSessionsPersonalityFilter:
    """Verify _list_recent_sessions filters by personality correctly."""

    def _insert_sessions(self, db: SessionDB):
        """Insert a mix of sessions with different personalities."""
        db.create_session("s_alpha", source="telegram", user_id="u1", personality="alpha")
        db.create_session("s_beta", source="telegram", user_id="u1", personality="beta")
        db.create_session("s_gamma", source="telegram", user_id="u1", personality="gamma")
        db.create_session("s_none", source="telegram", user_id="u1", personality=None)

    def test_filter_returns_only_matching(self, tmp_path):
        """With personality_filter='alpha', only alpha sessions should appear."""
        db = _make_db(tmp_path)
        self._insert_sessions(db)

        # We test the filter logic directly on list_sessions_rich + in-memory filter
        sessions = db.list_sessions_rich(limit=50)
        # Apply personality filter like _list_recent_sessions does
        filtered = [s for s in sessions if s.get("personality") == "alpha"]
        assert len(filtered) == 1
        assert filtered[0]["id"] == "s_alpha"

    def test_no_filter_returns_all(self, tmp_path):
        """Without personality_filter, all sessions should be returned."""
        db = _make_db(tmp_path)
        self._insert_sessions(db)

        sessions = db.list_sessions_rich(limit=50)
        assert len(sessions) == 4  # all 4 sessions

    def test_filter_none_personality(self, tmp_path):
        """Filtering for None personality should return sessions with no personality."""
        db = _make_db(tmp_path)
        self._insert_sessions(db)

        sessions = db.list_sessions_rich(limit=50)
        filtered = [s for s in sessions if s.get("personality") is None]
        assert len(filtered) == 1
        assert filtered[0]["id"] == "s_none"

    def test_filter_nonexistent_personality(self, tmp_path):
        """Filtering for a personality that doesn't exist should return empty."""
        db = _make_db(tmp_path)
        self._insert_sessions(db)

        sessions = db.list_sessions_rich(limit=50)
        filtered = [s for s in sessions if s.get("personality") == "nonexistent"]
        assert len(filtered) == 0


# ---------------------------------------------------------------------------
# Integration: _list_recent_sessions function with personality_filter
# ---------------------------------------------------------------------------

class TestListRecentSessionsIntegration:
    """Test the actual _list_recent_sessions function with personality_filter."""

    def test_list_recent_with_personality_filter(self, tmp_path):
        """_list_recent_sessions should accept and use personality_filter."""
        from tools.session_search_tool import _list_recent_sessions

        db = _make_db(tmp_path)
        db.create_session("s_alpha", source="telegram", user_id="u1", personality="alpha")
        db.create_session("s_beta", source="telegram", user_id="u1", personality="beta")

        result_str = _list_recent_sessions(db, limit=5, personality_filter="alpha")
        result = json.loads(result_str)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["results"][0]["session_id"] == "s_alpha"

    def test_list_recent_without_filter(self, tmp_path):
        """_list_recent_sessions without personality_filter returns all."""
        from tools.session_search_tool import _list_recent_sessions

        db = _make_db(tmp_path)
        db.create_session("s_alpha", source="telegram", user_id="u1", personality="alpha")
        db.create_session("s_beta", source="telegram", user_id="u1", personality="beta")

        result_str = _list_recent_sessions(db, limit=5)
        result = json.loads(result_str)

        assert result["success"] is True
        assert result["count"] == 2

    def test_list_recent_filter_none_personality(self, tmp_path):
        """_list_recent_sessions with personality_filter should exclude non-matching."""
        from tools.session_search_tool import _list_recent_sessions

        db = _make_db(tmp_path)
        db.create_session("s_alpha", source="telegram", user_id="u1", personality="alpha")
        db.create_session("s_none", source="telegram", user_id="u1", personality=None)

        # Filter for "alpha" should only return the alpha session
        result_str = _list_recent_sessions(db, limit=5, personality_filter="alpha")
        result = json.loads(result_str)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["results"][0]["session_id"] == "s_alpha"
