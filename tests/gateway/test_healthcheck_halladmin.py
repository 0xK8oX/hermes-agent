"""
Tests for healthcheck script and Hall admin check.

Covers commits:
  - b55ae91a2: fix(scripts): healthcheck mirrors gateway provider resolution
  - 8604ae7df: feat(scripts): add channel binding E2E health check
  - 96f11df89: feat: Hall auto-dispatch with pending file + gateway watcher
    → added _is_admin_soul function
  - 45dae4004: feat: Hall extension — inter-soul messaging board
    → Already covered by tests/gateway/test_hall.py
"""
import unittest
import os
from unittest.mock import patch, MagicMock

from gateway.extensions.hall import _is_admin_soul


class TestIsAdminSoul(unittest.TestCase):
    def test_empty_soul_name_returns_false(self):
        """Empty soul name returns False."""
        self.assertFalse(_is_admin_soul(""))
        self.assertFalse(_is_admin_soul(None))

    def test_legacy_alpha_returns_true(self):
        """Legacy admin soul name 'alpha' returns True."""
        self.assertTrue(_is_admin_soul("alpha"))
        self.assertTrue(_is_admin_soul("Alpha"))
        self.assertTrue(_is_admin_soul(" ALPHA "))

    def test_legacy_pm_returns_true(self):
        """Legacy admin soul name 'pm' returns True."""
        self.assertTrue(_is_admin_soul("pm"))
        self.assertTrue(_is_admin_soul("PM"))

    def test_no_env_var_returns_false(self):
        """When GATEWAY_ADMIN_USERS not set, returns False for non-legacy."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_is_admin_soul("dev"))

    def test_soul_not_bound_returns_false(self):
        """Soul not found in _lookup_soul_channel returns False."""
        with patch.dict(os.environ, {"GATEWAY_ADMIN_USERS": "12345"}):
            with patch("gateway.extensions.hall._lookup_soul_channel", return_value=None):
                self.assertFalse(_is_admin_soul("unknown_soul"))


class TestHealthCheckScriptImportable(unittest.TestCase):
    def test_script_imports_without_error(self):
        """The healthcheck script can be imported without errors."""
        # Add project root to path
        import sys
        from pathlib import Path
        project_root = Path(__file__).parent.parent.parent
        sys.path.insert(0, str(project_root))
        import scripts.channel_binding_healthcheck
        # Should import successfully
        self.assertTrue(hasattr(scripts.channel_binding_healthcheck, "main"))
        self.assertTrue(hasattr(scripts.channel_binding_healthcheck, "expand_api_key"))
        self.assertTrue(hasattr(scripts.channel_binding_healthcheck, "resolve_binding_runtime"))


if __name__ == "__main__":
    unittest.main()
