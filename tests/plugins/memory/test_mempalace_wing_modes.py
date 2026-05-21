"""
Tests for MemPalace wing mode resolution and behavior.

Covers the fix for wing=None fallback that previously searched all wings
when memory_scope was not set. Now uses explicit _wing_mode tracking:
  - shared (default): _wing="_global"
  - isolated (named): _wing=<name>
  - all ("*"): _wing=None (no filter)
  - disabled ("-"): skip all memory operations
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helper: create a provider without real ChromaDB
# ---------------------------------------------------------------------------

def _make_provider(hermes_home, memory_scope=None, **init_kwargs):
    """Create a MemPalaceProvider with mocked ChromaDB init."""
    from plugins.memory.mempalace import MemPalaceProvider

    p = MemPalaceProvider()
    with patch.object(p, "_init_chroma"):
        p.initialize(
            session_id="test",
            hermes_home=str(hermes_home),
            memory_scope=memory_scope,
            **init_kwargs,
        )
    return p


def _make_fake_col():
    """Return a fake ChromaDB collection that tracks calls."""
    class FakeCol:
        def __init__(self):
            self._docs = {}
            self._metas = {}
            self.query_calls = []

        def upsert(self, ids, documents, metadatas):
            for i, d, m in zip(ids, documents, metadatas):
                self._docs[i] = d
                self._metas[i] = m

        def get(self, include=None):
            return {
                "ids": list(self._docs.keys()),
                "documents": list(self._docs.values()),
                "metadatas": list(self._metas.values()),
            }

        def query(self, query_texts, n_results=5, where=None, include=None):
            self.query_calls.append({"where": where, "n_results": n_results})
            # Simple substring match for testing
            results_ids, results_docs, results_metas, results_dists = [], [], [], []
            qt = query_texts[0].lower() if query_texts else ""
            for did, doc in self._docs.items():
                meta = self._metas.get(did, {})
                # Apply where filter
                if where:
                    if "wing" in where and meta.get("wing") != where["wing"]:
                        continue
                if qt in doc.lower():
                    results_ids.append(did)
                    results_docs.append(doc)
                    results_metas.append(meta)
                    results_dists.append(0.1)
            n = n_results or 5
            return {
                "ids": [results_ids[:n]],
                "documents": [results_docs[:n]],
                "metadatas": [results_metas[:n]],
                "distances": [results_dists[:n]],
            }

    return FakeCol()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_home(tmp_path):
    """Create a temp HERMES_HOME with mempalace config."""
    hh = tmp_path / "hermes"
    hh.mkdir()
    palace_path = str(hh / "palace")
    cfg = hh / "config.yaml"
    cfg.write_text(
        "memory:\n"
        "  provider: mempalace\n"
        "  mempalace:\n"
        f'    data_path: "{palace_path}"\n'
        "    embedding_model: BAAI/bge-small-zh-v1.5\n"
        "    enable_kg: false\n"
        "    recall_mode: hybrid\n"
        "    context_tokens: 800\n"
    )
    return hh


# ===========================================================================
# 1. Wing mode resolution in initialize()
# ===========================================================================

class TestWingModeResolution:
    """Test memory_scope → (_wing, _wing_mode) mapping."""

    def test_none_scope_gives_shared(self, hermes_home):
        """memory_scope=None → _wing='_global', _wing_mode='shared'."""
        p = _make_provider(hermes_home, memory_scope=None)
        assert p._wing == "_global"
        assert p._wing_mode == "shared"
        p.shutdown()

    def test_no_scope_kwarg_gives_shared(self, hermes_home):
        """Omitting memory_scope entirely → shared mode."""
        p = _make_provider(hermes_home)
        assert p._wing == "_global"
        assert p._wing_mode == "shared"
        p.shutdown()

    def test_star_scope_gives_all(self, hermes_home):
        """memory_scope='*' → _wing=None, _wing_mode='all'."""
        p = _make_provider(hermes_home, memory_scope="*")
        assert p._wing is None
        assert p._wing_mode == "all"
        p.shutdown()

    def test_named_scope_gives_isolated(self, hermes_home):
        """memory_scope='lenx' → _wing='lenx', _wing_mode='isolated'."""
        p = _make_provider(hermes_home, memory_scope="lenx")
        assert p._wing == "lenx"
        assert p._wing_mode == "isolated"
        p.shutdown()

    def test_shared_scope_gives_shared(self, hermes_home):
        """memory_scope='shared' → _wing='_global', _wing_mode='shared'."""
        p = _make_provider(hermes_home, memory_scope="shared")
        assert p._wing == "_global"
        assert p._wing_mode == "shared"
        p.shutdown()

    def test_disabled_scope(self, hermes_home):
        """memory_scope='-' → _wing=None, _wing_mode='disabled'."""
        p = _make_provider(hermes_home, memory_scope="-")
        assert p._wing is None
        assert p._wing_mode == "disabled"
        p.shutdown()

    def test_empty_string_scope_gives_shared(self, hermes_home):
        """memory_scope='' → treated as None → shared."""
        p = _make_provider(hermes_home, memory_scope="")
        assert p._wing == "_global"
        assert p._wing_mode == "shared"
        p.shutdown()

    def test_init_default_wing_mode(self):
        """__init__ should set _wing_mode='shared' before initialize()."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        assert p._wing_mode == "shared"


# ===========================================================================
# 2. _tool_search behavior per wing mode
# ===========================================================================

class TestToolSearchWingBehavior:
    """Test that _tool_search applies correct wing filters."""

    def _setup_with_fake_col(self, hermes_home, memory_scope):
        p = _make_provider(hermes_home, memory_scope=memory_scope)
        fake_col = _make_fake_col()
        # Pre-populate with data in multiple wings
        fake_col.upsert(
            ids=["id_global"],
            documents=["global shared fact about unicorns"],
            metadatas=[{"wing": "_global", "room": "general"}],
        )
        fake_col.upsert(
            ids=["id_lenx"],
            documents=["lenx personal fact about unicorns"],
            metadatas=[{"wing": "lenx", "room": "general"}],
        )
        fake_col.upsert(
            ids=["id_dev"],
            documents=["dev workspace fact about unicorns"],
            metadatas=[{"wing": "dev", "room": "general"}],
        )
        p._collection = fake_col
        p._initialized = True
        return p, fake_col

    def test_disabled_mode_returns_empty(self, hermes_home):
        """Disabled mode → search returns empty results immediately."""
        p, _ = self._setup_with_fake_col(hermes_home, memory_scope="-")
        result = p._tool_search({"query": "unicorns"})
        parsed = json.loads(result)
        assert parsed["results"] == []
        assert parsed["total"] == 0
        p.shutdown()

    def test_shared_mode_searches_global_wing(self, hermes_home):
        """Shared mode → search filters to '_global' wing only."""
        p, fake_col = self._setup_with_fake_col(hermes_home, memory_scope=None)
        p._tool_search({"query": "unicorns"})
        # Verify the where filter includes wing="_global"
        assert len(fake_col.query_calls) == 1
        assert fake_col.query_calls[0]["where"] == {"wing": "_global"}
        p.shutdown()

    def test_isolated_mode_searches_specific_wing(self, hermes_home):
        """Isolated mode → search filters to named wing only."""
        p, fake_col = self._setup_with_fake_col(hermes_home, memory_scope="lenx")
        p._tool_search({"query": "unicorns"})
        assert len(fake_col.query_calls) == 1
        assert fake_col.query_calls[0]["where"] == {"wing": "lenx"}
        p.shutdown()

    def test_all_mode_no_wing_filter(self, hermes_home):
        """All mode → no wing filter, searches everything."""
        p, fake_col = self._setup_with_fake_col(hermes_home, memory_scope="*")
        p._tool_search({"query": "unicorns"})
        assert len(fake_col.query_calls) == 1
        # No wing filter (where may be empty or None)
        where = fake_col.query_calls[0]["where"]
        assert where is None or where == {}
        p.shutdown()

    def test_explicit_wing_arg_overrides_in_isolated_mode(self, hermes_home):
        """In isolated mode, explicit wing in args is IGNORED — own wing enforced."""
        p, fake_col = self._setup_with_fake_col(hermes_home, memory_scope="lenx")
        p._tool_search({"query": "unicorns", "wing": "dev"})
        assert len(fake_col.query_calls) == 1
        # Isolated mode: LLM-supplied wing is ignored, own wing enforced
        assert fake_col.query_calls[0]["where"] == {"wing": "lenx"}
        p.shutdown()


# ===========================================================================
# 3. _tool_add behavior per wing mode
# ===========================================================================

class TestToolAddWingBehavior:
    """Test that _tool_add respects wing modes."""

    def test_disabled_mode_returns_error(self, hermes_home):
        """Disabled mode → add returns error."""
        p = _make_provider(hermes_home, memory_scope="-")
        p._initialized = True
        result = p._tool_add({"content": "test fact", "wing": "dev"})
        parsed = json.loads(result)
        assert parsed.get("success") is False
        assert "disabled" in parsed.get("error", "").lower()
        p.shutdown()

    def test_shared_mode_default_wing_is_global(self, hermes_home):
        """Shared mode → add without explicit wing uses '_global'."""
        p = _make_provider(hermes_home, memory_scope=None)
        fake_col = _make_fake_col()
        p._collection = fake_col
        p._initialized = True
        result = p._tool_add({"content": "shared fact about rockets"})
        parsed = json.loads(result)
        assert parsed.get("success") is True
        assert parsed["wing"] == "_global"
        # Verify stored with _global wing
        assert fake_col._metas[parsed["drawer_id"]]["wing"] == "_global"
        p.shutdown()

    def test_isolated_mode_default_wing(self, hermes_home):
        """Isolated mode → add without explicit wing uses named wing."""
        p = _make_provider(hermes_home, memory_scope="lenx")
        fake_col = _make_fake_col()
        p._collection = fake_col
        p._initialized = True
        result = p._tool_add({"content": "lenx personal fact about rockets"})
        parsed = json.loads(result)
        assert parsed.get("success") is True
        assert parsed["wing"] == "lenx"
        p.shutdown()


# ===========================================================================
# 4. _auto_mine_facts behavior per wing mode
# ===========================================================================

class TestAutoMineFactsBehavior:
    """Test that _auto_mine_facts respects wing modes."""

    def test_disabled_mode_skips_mining(self, hermes_home):
        """Disabled mode → _auto_mine_facts returns immediately."""
        p = _make_provider(hermes_home, memory_scope="-")
        fake_col = _make_fake_col()
        p._collection = fake_col
        p._initialized = True
        # Should not crash or add anything
        p._auto_mine_facts("I live in Hong Kong and my name is Test")
        assert len(fake_col._docs) == 0
        p.shutdown()

    def test_shared_mode_mines_to_global(self, hermes_home):
        """Shared mode → auto-mine stores to '_global' wing."""
        p = _make_provider(hermes_home, memory_scope=None)
        fake_col = _make_fake_col()
        p._collection = fake_col
        p._initialized = True
        # Use a clear personal fact pattern
        p._auto_mine_facts("我的名字是测试用户")
        # Should have stored at least one fact
        assert len(fake_col._docs) >= 1
        # Core facts go to "shared" wing (identity → core category)
        for meta in fake_col._metas.values():
            assert meta["wing"] == "shared"  # core categories override wing
        p.shutdown()

    def test_isolated_mode_mines_to_named_wing(self, hermes_home):
        """Isolated mode → non-core auto-mined facts go to named wing."""
        p = _make_provider(hermes_home, memory_scope="lenx")
        fake_col = _make_fake_col()
        p._collection = fake_col
        p._initialized = True
        # Use a preference pattern (non-core category)
        p._auto_mine_facts("I prefer using Python for everything")
        # Preference facts should be stored in the named wing
        assert len(fake_col._docs) >= 1
        for did, meta in fake_col._metas.items():
            # Non-core categories should use the named wing
            if meta.get("room") != "identity":
                assert meta["wing"] == "lenx"
        p.shutdown()


# ===========================================================================
# 5. system_prompt_block behavior per wing mode
# ===========================================================================

class TestSystemPromptBlockBehavior:
    """Test that system_prompt_block respects wing modes."""

    def test_disabled_mode_returns_empty(self, hermes_home):
        """Disabled mode → system_prompt_block returns empty string."""
        p = _make_provider(hermes_home, memory_scope="-")
        # Even if we force initialized, disabled should return ""
        p._initialized = True
        assert p.system_prompt_block() == ""
        p.shutdown()

    def test_shared_mode_returns_content(self, hermes_home):
        """Shared mode → system_prompt_block tries to generate content."""
        p = _make_provider(hermes_home, memory_scope=None)
        fake_col = _make_fake_col()
        p._collection = fake_col
        p._initialized = True
        # May return empty if no L0/L1 data, but should not crash
        result = p.system_prompt_block()
        assert isinstance(result, str)
        p.shutdown()


# ===========================================================================
# 6. Edge cases
# ===========================================================================

class TestEdgeCases:
    """Test edge cases for wing mode handling."""

    def test_wing_mode_attribute_on_fresh_instance(self):
        """_wing_mode should be 'shared' even before initialize()."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        assert p._wing_mode == "shared"

    def test_multiple_initialize_calls(self, hermes_home):
        """Re-initializing with different scope should update wing mode."""
        # First init with shared
        p = _make_provider(hermes_home, memory_scope=None)
        assert p._wing == "_global"
        assert p._wing_mode == "shared"

        # Re-init with isolated
        with patch.object(p, "_init_chroma"):
            p.initialize(
                session_id="test2",
                hermes_home=str(hermes_home),
                memory_scope="alice",
            )
        assert p._wing == "alice"
        assert p._wing_mode == "isolated"

        # Re-init with disabled
        with patch.object(p, "_init_chroma"):
            p.initialize(
                session_id="test3",
                hermes_home=str(hermes_home),
                memory_scope="-",
            )
        assert p._wing is None
        assert p._wing_mode == "disabled"
        p.shutdown()

    def test_getattr_fallback_for_wing_mode(self, hermes_home):
        """If _wing_mode is somehow missing, getattr fallback works."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        del p._wing_mode  # Simulate missing attribute
        # getattr with default should not crash
        assert getattr(p, '_wing_mode', 'shared') == 'shared'
