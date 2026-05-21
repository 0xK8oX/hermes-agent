"""Tests for resolve_memory_dirs() scope logic.

Validates that named scopes (e.g. 'lenx', 'alpha') do NOT include _global/,
while shared/empty/None scopes do.
"""

from pathlib import Path
from unittest import mock

import pytest

# Import the function under test
from gateway.extensions.channel_binding import resolve_memory_dirs


@pytest.fixture
def mem_base(tmp_path: Path):
    """Create a fake memories/ directory tree with standard subdirs.

    Returns (tmp_path, memories_dir) where tmp_path acts as HERMES_HOME
    and memories_dir is tmp_path/memories.
    """
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "_global").mkdir()
    (memories / "_global" / "note.md").write_text("global note")
    (memories / "lenx").mkdir()
    (memories / "lenx" / "data.md").write_text("lenx data")
    (memories / "alpha").mkdir()
    (memories / "alpha" / "data.md").write_text("alpha data")
    (memories / "beta").mkdir()
    return tmp_path


def _resolve(scope, mem_base_tmp):
    """Call resolve_memory_dirs with hermes_constants patched to use mem_base_tmp."""
    with mock.patch(
        "gateway.extensions.channel_binding.get_hermes_home",
        return_value=mem_base_tmp,
        create=True,
    ):
        # Also patch the imported name inside the module
        import gateway.extensions.channel_binding as mod
        with mock.patch.object(mod, "Path", Path):
            # Patch the import inside the function
            return resolve_memory_dirs(scope)


def _resolve(scope, mem_base_tmp):
    """Call resolve_memory_dirs with get_hermes_home patched."""
    fake_hermes_home = mem_base_tmp
    with mock.patch(
        "hermes_constants.get_hermes_home",
        return_value=fake_hermes_home,
    ):
        return resolve_memory_dirs(scope)


# ── Wildcard scope ──────────────────────────────────────────────────────────

class TestWildcardScope:
    """scope='*' → includes _global + all subdirs."""

    def test_includes_global(self, mem_base):
        dirs = _resolve("*", mem_base)
        names = [d.name for d in dirs]
        assert "_global" in names

    def test_includes_all_subdirs(self, mem_base):
        dirs = _resolve("*", mem_base)
        names = [d.name for d in dirs]
        assert "lenx" in names
        assert "alpha" in names
        assert "beta" in names


# ── None scope ──────────────────────────────────────────────────────────────

class TestNoneScope:
    """scope=None → includes _global + base."""

    def test_includes_global(self, mem_base):
        dirs = _resolve(None, mem_base)
        names = [d.name for d in dirs]
        assert "_global" in names


# ── Empty scope ─────────────────────────────────────────────────────────────

class TestEmptyScope:
    """scope='' → includes _global + base."""

    def test_includes_global(self, mem_base):
        dirs = _resolve("", mem_base)
        names = [d.name for d in dirs]
        assert "_global" in names


# ── Shared scope ────────────────────────────────────────────────────────────

class TestSharedScope:
    """scope='shared' → includes _global + base."""

    def test_includes_global(self, mem_base):
        dirs = _resolve("shared", mem_base)
        names = [d.name for d in dirs]
        assert "_global" in names

    def test_no_named_dirs(self, mem_base):
        dirs = _resolve("shared", mem_base)
        names = [d.name for d in dirs]
        assert "lenx" not in names
        assert "alpha" not in names


# ── Named scopes ────────────────────────────────────────────────────────────

class TestNamedScopes:
    """Named scopes get ONLY their own dir, NO _global/."""

    def test_lenx_no_global(self, mem_base):
        dirs = _resolve("lenx", mem_base)
        names = [d.name for d in dirs]
        assert names == ["lenx"], f"Expected only ['lenx'], got {names}"

    def test_alpha_no_global(self, mem_base):
        dirs = _resolve("alpha", mem_base)
        names = [d.name for d in dirs]
        assert names == ["alpha"], f"Expected only ['alpha'], got {names}"

    def test_lenx_autocreate(self, tmp_path):
        """A named scope that doesn't exist yet is auto-created."""
        memories = tmp_path / "memories"
        memories.mkdir()
        dirs = _resolve("newscope", tmp_path)
        assert len(dirs) == 1
        assert dirs[0].name == "newscope"
        assert dirs[0].exists()
        assert "_global" not in [d.name for d in dirs]


# ── Isolated scope ─────────────────────────────────────────────────────────

class TestIsolatedScope:
    """scope='-' → _isolated dir only."""

    def test_isolated_only(self, mem_base):
        dirs = _resolve("-", mem_base)
        assert len(dirs) == 1
        assert dirs[0].name == "_isolated"

    def test_isolated_no_global(self, mem_base):
        dirs = _resolve("-", mem_base)
        names = [d.name for d in dirs]
        assert "_global" not in names


# ── Path traversal ─────────────────────────────────────────────────────────

class TestPathTraversal:
    """Malicious scope values are rejected safely."""

    def test_double_dot(self, mem_base):
        dirs = _resolve("..", mem_base)
        for d in dirs:
            assert ".." not in str(d)

    def test_slash(self, mem_base):
        dirs = _resolve("foo/bar", mem_base)
        for d in dirs:
            assert "foo" not in d.name

    def test_null_byte(self, mem_base):
        dirs = _resolve("lenx\x00evil", mem_base)
        for d in dirs:
            assert "evil" not in str(d)

    def test_backslash(self, mem_base):
        dirs = _resolve("foo\\bar", mem_base)
        for d in dirs:
            assert "foo" not in d.name
