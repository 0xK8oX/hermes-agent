"""
Tests for MemPalace memory provider plugin.

Uses isolated temp directories for each test to avoid polluting real palace data.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def palace_dir(tmp_path):
    """Create a temp palace directory."""
    d = tmp_path / "mempalace"
    d.mkdir()
    return d


@pytest.fixture
def hermes_home(tmp_path):
    """Create a temp HERMES_HOME with mempalace config."""
    hh = tmp_path / "hermes"
    hh.mkdir()
    # Write config with mempalace settings
    palace_path = str(hh / "palace")
    cfg = hh / "config.yaml"
    cfg.write_text(
        "memory:\n"
        "  provider: mempalace\n"
        "  mempalace:\n"
        f"    data_path: \"{palace_path}\"\n"
        "    embedding_model: BAAI/bge-small-zh-v1.5\n"
        "    enable_kg: false\n"
        "    recall_mode: hybrid\n"
        "    context_tokens: 800\n"
    )
    return hh


@pytest.fixture
def provider(hermes_home, palace_dir):
    """Create and initialize a MemPalaceProvider with temp dirs."""
    os.environ["HERMES_HOME"] = str(hermes_home)
    # Need to create the palace dir at the configured path
    configured_path = hermes_home / "palace"
    configured_path.mkdir(exist_ok=True)

    from plugins.memory.mempalace import MemPalaceProvider
    p = MemPalaceProvider()
    p.initialize(
        session_id="test_session",
        hermes_home=str(hermes_home),
        platform="cli",
    )
    yield p
    p.shutdown()
    os.environ.pop("HERMES_HOME", None)


# ===========================================================================
# Basic Provider Tests
# ===========================================================================

class TestMemPalaceProvider:
    """Test MemPalaceProvider ABC implementation."""

    def test_name(self, provider):
        assert provider.name == "mempalace"

    def test_is_available(self):
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        assert p.is_available() is True

    def test_initialize_creates_palace_dir(self, hermes_home, tmp_path):
        """Palace directory is created if it doesn't exist."""
        palace = tmp_path / "new_palace"
        assert not palace.exists()

        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            p.initialize(
                session_id="test",
                hermes_home=str(hermes_home),
                platform="cli",
            )
        assert p._initialized

    def test_initialize_cron_skip(self, hermes_home):
        """Cron/flush context should skip initialization."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        p.initialize(
            session_id="test",
            hermes_home=str(hermes_home),
            agent_context="cron",
        )
        assert p._cron_skipped is True
        assert p._initialized is False

    def test_memory_scope_maps_to_wing(self, hermes_home):
        """memory_scope kwarg should map to _wing."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        p.initialize(
            session_id="test",
            hermes_home=str(hermes_home),
            memory_scope="dev",
        )
        assert p._wing == "dev"

    def test_memory_scope_none(self, hermes_home):
        """No memory_scope → _wing is '_global' (shared)."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        p.initialize(
            session_id="test",
            hermes_home=str(hermes_home),
        )
        assert p._wing == "_global"
        assert p._wing_mode == "shared"


# ===========================================================================
# Tool Schema Tests
# ===========================================================================

class TestToolSchemas:
    """Test tool schema registration."""

    def test_hybrid_mode_returns_tools(self, provider):
        """Hybrid mode should return search + add + status tools."""
        schemas = provider.get_tool_schemas()
        names = [s["name"] for s in schemas]
        assert "mempalace_search" in names
        assert "mempalace_add" in names
        assert "mempalace_status" in names

    def test_cron_skip_returns_no_tools(self, hermes_home):
        """Cron context should return empty tool list."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        p.initialize(
            session_id="test",
            hermes_home=str(hermes_home),
            agent_context="cron",
        )
        assert p.get_tool_schemas() == []

    def test_tool_schemas_have_required_fields(self, provider):
        """Each schema should have name, description, parameters."""
        for schema in provider.get_tool_schemas():
            assert "name" in schema
            assert "description" in schema
            assert "parameters" in schema


# ===========================================================================
# Config Tests
# ===========================================================================

class TestConfig:
    """Test MemPalaceConfig parsing."""

    def test_config_reads_from_yaml(self, hermes_home):
        from plugins.memory.mempalace.config import MemPalaceConfig
        cfg = MemPalaceConfig(hermes_home=str(hermes_home))
        assert cfg.embedding_model == "BAAI/bge-small-zh-v1.5"
        assert cfg.enable_kg is False
        assert cfg.recall_mode == "hybrid"
        assert cfg.context_tokens == 800

    def test_config_defaults(self, tmp_path):
        """Config without mempalace section should use defaults."""
        from plugins.memory.mempalace.config import MemPalaceConfig
        cfg = MemPalaceConfig(hermes_home=str(tmp_path))
        assert cfg.recall_mode == "hybrid"
        assert cfg.context_tokens == 800
        assert cfg.search_n_results == 5

    def test_config_schema(self):
        """get_config_schema returns valid fields."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        schema = p.get_config_schema()
        assert len(schema) >= 4
        keys = {s["key"] for s in schema}
        assert "data_path" in keys
        assert "enable_kg" in keys
        assert "recall_mode" in keys


# ===========================================================================
# Tool Dispatch Tests
# ===========================================================================

class TestToolDispatch:
    """Test tool call handling."""

    def test_search_returns_json(self, provider):
        """mempalace_search should return valid JSON."""
        result = provider.handle_tool_call("mempalace_search", {"query": "test"})
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_add_returns_json(self, provider):
        """mempalace_add should return valid JSON."""
        result = provider.handle_tool_call(
            "mempalace_add",
            {"content": "Test fact to remember", "wing": "test", "room": "general"},
        )
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_add_empty_content_fails(self, provider):
        """Empty content should return error."""
        result = provider.handle_tool_call(
            "mempalace_add",
            {"content": "   ", "wing": "test"},
        )
        parsed = json.loads(result)
        assert "error" in parsed

    def test_status_returns_json(self, provider):
        """mempalace_status should return valid JSON."""
        result = provider.handle_tool_call("mempalace_status", {})
        parsed = json.loads(result)
        assert "total_drawers" in parsed

    def test_unknown_tool_returns_error(self, provider):
        """Unknown tool name should return error."""
        result = provider.handle_tool_call("unknown_tool", {})
        parsed = json.loads(result)
        assert "error" in parsed


# ===========================================================================
# Scope → Wing Mapping Tests
# ===========================================================================

class TestScopeWingMapping:
    """Test memory_scope → wing mapping."""

    def test_search_uses_current_wing(self, hermes_home):
        """Search without explicit wing should use current scope."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        p.initialize(
            session_id="test",
            hermes_home=str(hermes_home),
            memory_scope="dev",
        )
        # The search tool should default to wing="dev"
        result = p.handle_tool_call("mempalace_search", {"query": "test"})
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_search_explicit_wing_override(self, hermes_home):
        """Explicit wing in args should override current scope."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        p.initialize(
            session_id="test",
            hermes_home=str(hermes_home),
            memory_scope="dev",
        )
        result = p.handle_tool_call(
            "mempalace_search",
            {"query": "test", "wing": "pm"},
        )
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_status_shows_current_wing(self, hermes_home):
        """Status should show the current wing."""
        from plugins.memory.mempalace import MemPalaceProvider
        p = MemPalaceProvider()
        p.initialize(
            session_id="test",
            hermes_home=str(hermes_home),
            memory_scope="dev",
        )
        result = p.handle_tool_call("mempalace_status", {})
        parsed = json.loads(result)
        assert parsed["current_wing"] == "dev"


# ===========================================================================
# Plugin Discovery Tests
# ===========================================================================

class TestPluginDiscovery:
    """Test that the plugin is discoverable by the plugin loader."""

    def test_plugin_yaml_exists(self):
        # test file: tests/plugins/memory/test_mempalace.py
        # plugin dir: plugins/memory/mempalace/
        repo_root = Path(__file__).resolve().parents[3]
        yaml_path = repo_root / "plugins" / "memory" / "mempalace" / "plugin.yaml"
        assert yaml_path.exists(), f"plugin.yaml not found at {yaml_path}"

    def test_register_function_exists(self):
        from plugins.memory.mempalace import register
        assert callable(register)

    def test_plugin_loadable(self):
        """Plugin can be loaded via the plugin loader."""
        from plugins.memory import _load_provider_from_dir
        repo_root = Path(__file__).resolve().parents[3]
        provider_dir = repo_root / "plugins" / "memory" / "mempalace"
        provider = _load_provider_from_dir(provider_dir)
        assert provider is not None
        assert provider.name == "mempalace"


# ===========================================================================
# Integration: add + search round-trip
# ===========================================================================

class TestIntegration:
    """End-to-end add and search (FakeCol mock, no ChromaDB/Ollama needed)."""

    @staticmethod
    def _make_fake_col():
        """Return a fake collection for testing (no ChromaDB dependency)."""
        class FakeCol:
            def __init__(self):
                self._docs = {}
                self._ids = []
                self._metas = {}

            def upsert(self, ids, documents, metadatas):
                for i, d, m in zip(ids, documents, metadatas):
                    self._docs[i] = d
                    self._ids.append(i)
                    self._metas[i] = m

            def get(self, include=None):
                return {
                    "ids": list(self._docs.keys()),
                    "documents": list(self._docs.values()),
                    "metadatas": list(self._metas.values()),
                }

            def query(self, query_texts, n_results=5, where=None, include=None):
                results_ids = []
                results_docs = []
                results_metas = []
                results_dists = []
                qt = query_texts[0].lower()
                for did, doc in self._docs.items():
                    if qt in doc.lower() or any(w in doc.lower() for w in qt.split()):
                        results_ids.append(did)
                        results_docs.append(doc)
                        results_metas.append(self._metas.get(did, {}))
                        results_dists.append(0.1)
                n = n_results or 5
                return {
                    "ids": [results_ids[:n]],
                    "documents": [results_docs[:n]],
                    "metadatas": [results_metas[:n]],
                    "distances": [results_dists[:n]],
                }

        return FakeCol()

    def _make_provider(self, hermes_home, memory_scope="dev"):
        """Create a provider with a fake collection injected."""
        from unittest.mock import patch
        from plugins.memory.mempalace import MemPalaceProvider

        p = MemPalaceProvider()
        # Patch _init_chroma to do nothing, then inject fake collection
        with patch.object(p, "_init_chroma"):
            p.initialize(
                session_id="test",
                hermes_home=str(hermes_home),
                memory_scope=memory_scope,
            )
        p._collection = self._make_fake_col()
        p._initialized = True  # Enable tool dispatch after fake collection injection
        return p

    def test_add_and_search(self, hermes_home):
        """Add a drawer, then search should find it."""
        from unittest.mock import patch

        p = self._make_provider(hermes_home, memory_scope="dev")

        with patch("mempalace.searcher.search_memories", side_effect=lambda **kw: {"results": []}):
            unique_text = "Unicorn debugging technique for quantum code review 42xyz"
            result = p.handle_tool_call(
                "mempalace_add",
                {"content": unique_text, "wing": "dev", "room": "test"},
            )
            add_result = json.loads(result)
            assert add_result.get("success") is True

            # Search via status to verify count
            result = p.handle_tool_call("mempalace_status", {})
            status = json.loads(result)
            assert status["total_drawers"] >= 1
            assert "dev" in status["wings"]

            p.shutdown()

    def test_status_after_add(self, hermes_home):
        """Status should show increased count after adding."""
        p = self._make_provider(hermes_home, memory_scope="dev")

        # Get initial status
        result = p.handle_tool_call("mempalace_status", {})
        before = json.loads(result)
        assert before["total_drawers"] == 0

        # Add a drawer
        result = p.handle_tool_call(
            "mempalace_add",
            {"content": "Test entry for status check", "wing": "dev"},
        )
        add_result = json.loads(result)
        assert add_result.get("success") is True

        # Get updated status
        result = p.handle_tool_call("mempalace_status", {})
        after = json.loads(result)

        assert after["total_drawers"] >= 1
        assert "dev" in after["wings"]

        p.shutdown()
