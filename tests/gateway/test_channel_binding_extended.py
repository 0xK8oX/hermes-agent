"""
Extended tests for channel_binding: allow_config_write, _enrich_from_soul_frontmatter,
and dynamic binding persistence through session reset.

Covers commits:
  - 706d0c64b: protect Hermes config paths from bound channel souls
  - 0face4db0: refactor: soul frontmatter as single source of truth
  - 739d5d93c: fix: channel binding persistence, memory tool dedup, WhatsApp group/JID
"""
import unittest
from unittest.mock import patch, MagicMock
import threading
from pathlib import Path

from gateway.extensions.channel_binding import (
    _session_allow_config_write,
    _session_bindings,
    _session_soul_names,
    _enrich_from_soul_frontmatter,
    _apply_binding_unlocked,
    _on_new_session,
    _on_session_reset,
    _on_session_cleanup,
    has_config_write_permission,
    _state_lock,
    resolve_memory_dirs,
)


class TestAllowConfigWrite(unittest.TestCase):
    def setUp(self):
        # Clear state before each test
        with _state_lock:
            _session_allow_config_write.clear()
            _session_bindings.clear()
            _session_soul_names.clear()

    def tearDown(self):
        with _state_lock:
            _session_allow_config_write.clear()
            _session_bindings.clear()
            _session_soul_names.clear()

    def test_allow_config_write_populated_from_frontmatter_true(self):
        """When soul frontmatter has allow_config_write: True → session gets True."""
        session_key = "test_session:12345"
        binding = {"soul": "test_soul"}

        mock_meta = {"allow_config_write": True}
        with _state_lock:
            _apply_binding_unlocked(session_key, binding, preloaded_meta=mock_meta)

        with _state_lock:
            self.assertIn(session_key, _session_allow_config_write)
            self.assertTrue(_session_allow_config_write[session_key])

    def test_allow_config_write_defaults_false_when_missing(self):
        """When frontmatter doesn't specify allow_config_write → defaults to False."""
        session_key = "test_session:12345"
        binding = {"soul": "test_soul"}

        mock_meta = {}
        with _state_lock:
            _apply_binding_unlocked(session_key, binding, preloaded_meta=mock_meta)

        with _state_lock:
            self.assertIn(session_key, _session_allow_config_write)
            self.assertFalse(_session_allow_config_write[session_key])

    def test_allow_config_write_false_when_falsy(self):
        """When allow_config_write: false → stored as False."""
        session_key = "test_session:12345"
        binding = {"soul": "test_soul"}

        mock_meta = {"allow_config_write": False}
        with _state_lock:
            _apply_binding_unlocked(session_key, binding, preloaded_meta=mock_meta)

        with _state_lock:
            self.assertFalse(_session_allow_config_write[session_key])

    def test_has_config_write_permission_true_for_unbound_sessions(self):
        """Unbound sessions (no entry in dict) default to True (backward compat)."""
        session_key = "unbound:67890"
        result = has_config_write_permission(session_key)
        self.assertTrue(result)

    def test_on_session_cleanup_clears_allow_config_write(self):
        """_on_session_cleanup removes entry from _session_allow_config_write."""
        session_key = "test_session:12345"
        binding = {"soul": "test_soul"}
        mock_meta = {"allow_config_write": True}

        with patch(
            "gateway.extensions.channel_binding._load_soul_with_meta",
            return_value=("# Soul Test", mock_meta),
        ):
            with _state_lock:
                _apply_binding_unlocked(session_key, binding)

        with _state_lock:
            self.assertIn(session_key, _session_allow_config_write)

        # Call cleanup
        _on_session_cleanup(session_key)

        with _state_lock:
            self.assertNotIn(session_key, _session_allow_config_write)


class TestEnrichFromSoulFrontmatter(unittest.TestCase):
    def test_no_soul_entry_returns_unchanged(self):
        """Entry without 'soul' key returns unchanged."""
        entry = {"model": "gpt-4", "provider": "openai"}
        result = _enrich_from_soul_frontmatter(entry)
        self.assertEqual(result, entry)

    def test_non_string_soul_returns_unchanged(self):
        """Non-string soul returns unchanged."""
        entry = {"soul": 123}
        result = _enrich_from_soul_frontmatter(entry)
        self.assertEqual(result, entry)

    def test_soul_not_found_returns_unchanged(self):
        """When soul file doesn't exist → entry returns unchanged."""
        entry = {"soul": "nonexistent_soul"}
        with patch(
            "gateway.extensions.channel_binding._load_soul_with_meta",
            return_value=None,
        ):
            result = _enrich_from_soul_frontmatter(entry)
        self.assertEqual(result, entry)

    def test_empty_frontmatter_fills_missing_fields(self):
        """Soul frontmatter fills in missing fields - existing fields NOT overwritten."""
        # Entry already has 'model' and 'provider' → preserved.
        # Missing 'api_key', 'base_url', 'memory_scope', 'skills' → filled from soul.
        entry = {
            "soul": "test_soul",
            "model": "existing:override",
            "provider": "existing_provider",
        }
        soul_meta = {
            "model": "should_not_override",
            "provider": "should_not_override",
            "api_key": "soul_api_key",
            "base_url": "https://soul.example.com",
            "memory_scope": "soul_scope",
            "skills": ["skill1", "skill2"],
        }

        with patch(
            "gateway.extensions.channel_binding._load_soul_with_meta",
            return_value=("# Test Soul", soul_meta),
        ):
            result = _enrich_from_soul_frontmatter(entry)

        # Existing fields preserved
        self.assertEqual(result["model"], "existing:override")
        self.assertEqual(result["provider"], "existing_provider")
        # Missing fields filled from soul
        self.assertEqual(result["api_key"], "soul_api_key")
        self.assertEqual(result["base_url"], "https://soul.example.com")
        self.assertEqual(result["memory_scope"], "soul_scope")
        self.assertEqual(result["skills"], ["skill1", "skill2"])

    def test_invalid_frontmatter_returns_unchanged(self):
        """Empty/None meta returns entry unchanged."""
        entry = {"soul": "test_soul"}
        with patch(
            "gateway.extensions.channel_binding._load_soul_with_meta",
            return_value=("# Soul Test", None),
        ):
            result = _enrich_from_soul_frontmatter(entry)
        self.assertEqual(result, entry)

        entry = {"soul": "test_soul"}
        with patch(
            "gateway.extensions.channel_binding._load_soul_with_meta",
            return_value=("# Soul Test", {}),
        ):
            result = _enrich_from_soul_frontmatter(entry)
        # Still the same entry
        self.assertEqual(result, {"soul": "test_soul"})


class TestDynamicBindingPersistence(unittest.TestCase):
    def setUp(self):
        with _state_lock:
            _session_bindings.clear()

    def tearDown(self):
        with _state_lock:
            _session_bindings.clear()

    def test_on_new_session_returns_early_when_binding_already_exists(self):
        """If binding already exists (dynamic from /bind), don't overwrite from config."""
        session_key = "agent:main:discord:group:12345"
        existing_binding = {"soul": "dynamic_bound", "model": "dynamic_model"}

        with _state_lock:
            _session_bindings[session_key] = existing_binding

        with patch(
            "gateway.extensions.channel_binding._resolve_binding_from_session_key",
            return_value=None,
        ) as mock_resolve:
            with patch(
                "gateway.extensions.channel_binding._apply_binding"
            ) as mock_apply:
                _on_new_session(session_key, None)

        # Should not call resolve or apply because binding already exists
        mock_resolve.assert_not_called()
        mock_apply.assert_not_called()

    def test_on_session_reset_preserves_existing_dynamic_binding(self):
        """_on_session_reset does NOT clear _session_bindings."""
        session_key = "test:session:123"
        binding = {"soul": "test"}
        with _state_lock:
            _session_bindings[session_key] = binding

        _on_session_reset(session_key)

        with _state_lock:
            self.assertIn(session_key, _session_bindings)
            self.assertEqual(_session_bindings[session_key], binding)

    def test_on_session_cleanup_clears_binding(self):
        """_on_session_cleanup removes binding from _session_bindings."""
        session_key = "test:cleanup:123"
        with _state_lock:
            _session_bindings[session_key] = {"soul": "test"}

        _on_session_cleanup(session_key)

        with _state_lock:
            self.assertNotIn(session_key, _session_bindings)


if __name__ == "__main__":
    unittest.main()
