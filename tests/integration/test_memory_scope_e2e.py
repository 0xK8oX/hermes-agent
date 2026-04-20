"""
Integration test: verify memory scope end-to-end for all souls.

Covers:
1. Soul frontmatter has correct memory_scope
2. resolve_memory_dirs() returns correct dirs per scope
3. MemPalace provider resolves correct _wing and _wing_mode per scope
4. Session personality persistence (new + existing session)
5. search_messages respects personality_filter (FTS5 + CJK LIKE fallback)
"""

import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERMES_HOME = Path.home() / ".hermes"
SOULS_DIR = HERMES_HOME / "souls"
AGENT_DIR = HERMES_HOME / "hermes-agent"

# ---------------------------------------------------------------------------
# sys.path bootstrap — do once at module level
# ---------------------------------------------------------------------------
import sys as _sys
_agent_dir = str(AGENT_DIR)
if _agent_dir not in _sys.path:
    _sys.path.insert(0, _agent_dir)


# ---------------------------------------------------------------------------
# Expected configuration
# ---------------------------------------------------------------------------
EXPECTED_SCOPES = {
    "pm": "shared",
    "dev": "shared",
    "research": "shared",
    "operation": "shared",
    "accountant": "shared",
    "alpha": "alpha",
    "lenx": "lenx",
    "doctor": "doctor",
    "fed": "-",
}


def _parse_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a soul .md file (lightweight, no yaml dep)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm = text[3:end].strip()
    result = {}
    for line in fm.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            val = val.strip()
            # Strip surrounding quotes (YAML quoted strings)
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            elif len(val) >= 2 and val[0] == "'" and val[-1] == "'":
                val = val[1:-1]
            result[key.strip()] = val
    return result


def _resolve(scope, fake_memories_base):
    """Call resolve_memory_dirs with patched get_hermes_home."""
    from gateway.extensions.channel_binding import resolve_memory_dirs
    with patch("hermes_constants.get_hermes_home", return_value=fake_memories_base):
        return resolve_memory_dirs(scope)


def _make_provider(memory_scope, palace_path=None):
    """Create a MemPalace provider with mocked ChromaDB.

    Args:
        memory_scope: The memory scope string (e.g. "lenx", "shared", "-", "*")
        palace_path: Override the data_path (defaults to /tmp/fake_palace)
    """
    import os
    from plugins.memory.mempalace import MemPalaceProvider
    provider = MemPalaceProvider()
    _path = palace_path or "/tmp/fake_palace"
    with patch.object(provider, '_init_chroma'), \
         patch("plugins.memory.mempalace.MemPalaceConfig") as mock_cfg:
        mock_cfg.return_value.data_path = _path
        mock_cfg.return_value.search_n_results = 5
        mock_cfg.return_value.search_max_distance = 2.0
        provider.initialize(
            session_id="test",
            hermes_home="/tmp/fake_hermes",
            platform="telegram",
            agent_context="chat",
            memory_scope=memory_scope,
        )
    # Clean up env var leak from initialize()
    os.environ.pop("MEMPALACE_PALACE_PATH", None)
    return provider


# =========================================================================
# 1. Soul frontmatter verification
# =========================================================================
class TestSoulMemoryScope:
    """Verify every soul file has the expected memory_scope."""

    @pytest.mark.parametrize("soul_name,expected", list(EXPECTED_SCOPES.items()))
    def test_soul_has_correct_memory_scope(self, soul_name, expected):
        soul_path = SOULS_DIR / f"{soul_name}.md"
        assert soul_path.exists(), f"Soul file not found: {soul_path}"
        fm = _parse_frontmatter(soul_path)
        actual = fm.get("memory_scope", "<not set>")
        assert actual == expected, (
            f"Soul '{soul_name}': expected memory_scope='{expected}', got '{actual}'"
        )


# =========================================================================
# 2. resolve_memory_dirs integration
# =========================================================================
class TestResolveMemoryDirsIntegration:
    """Test resolve_memory_dirs with each scope value."""

    @pytest.fixture
    def fake_memories(self, tmp_path):
        """Create a fake memories directory structure."""
        mem = tmp_path / "memories"
        mem.mkdir()
        gdir = mem / "_global"
        gdir.mkdir()
        (gdir / "MEMORY.md").write_text("- global fact\n")
        for name in ["pm", "dev", "research", "operation", "accountant",
                      "alpha", "lenx", "doctor", "fed"]:
            d = mem / name
            d.mkdir()
            (d / "MEMORY.md").write_text(f"- {name} fact\n")
        return tmp_path  # parent of memories/

    # --- Per-soul scope resolution (not tautological) ---
    @pytest.mark.parametrize("soul_name,scope", [
        ("pm", "shared"), ("dev", "shared"), ("research", "shared"),
        ("operation", "shared"), ("accountant", "shared"),
    ])
    def test_shared_soul_includes_global(self, fake_memories, soul_name, scope):
        """Each shared soul resolves scope='shared' → _global/ included."""
        dirs = _resolve(scope, fake_memories)
        dir_names = [d.name for d in dirs]
        assert "_global" in dir_names, (
            f"Soul '{soul_name}' (scope={scope}): _global not in dirs: {dir_names}"
        )

    @pytest.mark.parametrize("soul_name,scope", [
        ("alpha", "alpha"), ("lenx", "lenx"), ("doctor", "doctor"),
    ])
    def test_isolated_soul_excludes_global(self, fake_memories, soul_name, scope):
        """Each isolated soul resolves own scope → NO _global/, only own dir."""
        dirs = _resolve(scope, fake_memories)
        dir_names = [d.name for d in dirs]
        assert "_global" not in dir_names, (
            f"Soul '{soul_name}' (scope={scope}): _global should NOT be in dirs: {dir_names}"
        )
        assert scope in dir_names, (
            f"Soul '{soul_name}' (scope={scope}): own dir not in results: {dir_names}"
        )

    # --- Special scopes ---
    def test_disabled_scope_returns_isolated(self, fake_memories):
        """fed (scope='-') → _isolated dir, no other dirs."""
        dirs = _resolve("-", fake_memories)
        dir_names = [d.name for d in dirs]
        assert "_isolated" in dir_names
        assert "_global" not in dir_names

    def test_star_scope_includes_all(self, fake_memories):
        """'*' → _global + all named subdirs."""
        dirs = _resolve("*", fake_memories)
        dir_names = [d.name for d in dirs]
        assert "_global" in dir_names
        for soul in ["alpha", "lenx", "doctor", "pm", "dev"]:
            assert soul in dir_names, f"'*': expected '{soul}' in dirs: {dir_names}"

    # --- Edge cases ---
    def test_none_scope_includes_global(self, fake_memories):
        """None (no soul bound) → _global/ + base."""
        dirs = _resolve(None, fake_memories)
        dir_names = [d.name for d in dirs]
        assert "_global" in dir_names, f"scope=None: _global not in dirs: {dir_names}"

    def test_empty_scope_includes_global(self, fake_memories):
        """Empty string → same as None / shared."""
        dirs = _resolve("", fake_memories)
        dir_names = [d.name for d in dirs]
        assert "_global" in dir_names, f"scope='': _global not in dirs: {dir_names}"

    def test_whitespace_scope_includes_global(self, fake_memories):
        """Whitespace-only string → same as None / shared."""
        dirs = _resolve("   ", fake_memories)
        dir_names = [d.name for d in dirs]
        assert "_global" in dir_names, f"scope='   ': _global not in dirs: {dir_names}"

    def test_global_dir_missing_returns_base(self, tmp_path):
        """shared scope but _global/ doesn't exist → returns [base]."""
        base = tmp_path
        (base / "memories").mkdir()  # no _global subdir
        dirs = _resolve("shared", base)
        assert len(dirs) == 1
        assert dirs[0].name == "memories"

    def test_named_scope_auto_creates_dir(self, tmp_path):
        """Named scope where directory doesn't exist → auto-creates it."""
        base = tmp_path
        (base / "memories").mkdir()
        assert not (base / "memories" / "newscope").exists()
        dirs = _resolve("newscope", base)
        assert len(dirs) == 1
        assert dirs[0].name == "newscope"
        assert dirs[0].exists()  # auto-created

    def test_path_traversal_scope_returns_base(self, tmp_path):
        """Malicious scope with '..' → returns [base] (safe fallback)."""
        base = tmp_path
        (base / "memories").mkdir()
        dirs = _resolve("../../../etc", base)
        assert len(dirs) == 1
        assert dirs[0].name == "memories"


# =========================================================================
# 3. MemPalace wing mode integration
# =========================================================================
class TestMemPalaceWingModeIntegration:
    """Verify MemPalace resolves correct wing_mode per scope."""

    @pytest.mark.parametrize("soul_name,scope", [
        ("pm", "shared"), ("dev", "shared"), ("research", "shared"),
        ("operation", "shared"), ("accountant", "shared"),
    ])
    def test_shared_souls_use_global_wing(self, soul_name, scope):
        p = _make_provider(scope)
        assert p._wing_mode == "shared", f"Soul '{soul_name}': expected _wing_mode='shared'"
        assert p._wing == "_global", f"Soul '{soul_name}': expected _wing='_global'"

    @pytest.mark.parametrize("soul_name,scope", [
        ("alpha", "alpha"), ("lenx", "lenx"), ("doctor", "doctor"),
    ])
    def test_isolated_souls_use_own_wing(self, soul_name, scope):
        p = _make_provider(scope)
        assert p._wing_mode == "isolated", f"Soul '{soul_name}': expected _wing_mode='isolated'"
        assert p._wing == scope, f"Soul '{soul_name}': expected _wing='{scope}'"

    def test_fed_disabled(self):
        p = _make_provider("-")
        assert p._wing_mode == "disabled", "Soul 'fed': expected _wing_mode='disabled'"

    def test_star_scope_uses_all_mode(self):
        """scope='*' → _wing_mode='all', _wing=None."""
        p = _make_provider("*")
        assert p._wing_mode == "all", "scope='*': expected _wing_mode='all'"
        assert p._wing is None, "scope='*': expected _wing=None"

    def test_disabled_returns_empty_search(self):
        p = _make_provider("-")
        result = json.loads(p._tool_search({"query": "anything"}))
        assert result["total"] == 0, "disabled scope should return empty search"

    def test_disabled_blocks_add(self):
        p = _make_provider("-")
        result = json.loads(p._tool_add({"content": "test", "room": "test"}))
        assert result.get("success") is False, "disabled scope should block add"

    def test_disabled_returns_empty_prompt(self):
        p = _make_provider("-")
        p._initialized = True
        p._layers_baked = False
        assert p.system_prompt_block() == "", "disabled scope should return empty prompt"


# =========================================================================
# 4. Session personality persistence integration
# =========================================================================
class TestSessionPersonalityIntegration:
    """Verify personality is persisted and searchable."""

    @pytest.fixture
    def db(self, tmp_path):
        """Create a real SessionDB with temp SQLite."""
        from hermes_state import SessionDB
        db_path = tmp_path / "test.db"
        database = SessionDB(db_path)
        yield database
        database.close()

    def test_create_and_update_personality(self, db):
        """New session gets personality, and it can be updated."""
        sid = db.create_session(session_id="s1", source="telegram", personality="pm")
        session = db.get_session("s1")
        assert session["personality"] == "pm"

        # Update
        db.update_session_personality("s1", "dev")
        session = db.get_session("s1")
        assert session["personality"] == "dev"

    def test_update_personality_on_empty(self, db):
        """Session with empty personality can be updated (gateway restart case)."""
        sid = db.create_session(session_id="s2", source="telegram", personality=None)
        session = db.get_session("s2")
        assert session["personality"] is None

        # Simulate gateway restart → binding loads → personality now known
        db.update_session_personality("s2", "alpha")
        session = db.get_session("s2")
        assert session["personality"] == "alpha"

    def test_update_personality_nonexistent_session(self, db):
        """Updating a non-existent session silently succeeds (0 rows)."""
        db.update_session_personality("ghost_session", "pm")
        # Should not raise; session still doesn't exist
        assert db.get_session("ghost_session") is None

    def test_personality_filter_in_search(self, db):
        """search_messages filters by personality (FTS5 path).

        All messages contain 'discussion' so FTS5 finds them all — the filter
        must then exclude non-matching personalities.
        """
        db.create_session(session_id="s_pm", source="telegram", personality="pm")
        db.create_session(session_id="s_alpha", source="telegram", personality="alpha")
        db.create_session(session_id="s_dev", source="telegram", personality="dev")

        db.append_message("s_pm", "user", "PM discussion about roadmap")
        db.append_message("s_alpha", "user", "Alpha discussion about strategy")
        db.append_message("s_dev", "user", "Dev discussion about architecture")

        results = db.search_messages("discussion", personality_filter="pm")
        session_ids = [r.get("session_id") for r in results]
        assert "s_pm" in session_ids
        assert "s_alpha" not in session_ids
        assert "s_dev" not in session_ids

    def test_cjk_search_with_personality_filter(self, db):
        """CJK LIKE fallback also applies personality_filter (was a bug)."""
        db.create_session(session_id="s_pm", source="telegram", personality="pm")
        db.create_session(session_id="s_alpha", source="telegram", personality="alpha")

        db.append_message("s_pm", "user", "產品經理討論路線圖")
        db.append_message("s_alpha", "user", "Alpha客戶請求討論")

        results = db.search_messages("討論", personality_filter="pm")
        session_ids = [r.get("session_id") for r in results]
        assert "s_pm" in session_ids, "CJK search should find pm session"
        assert "s_alpha" not in session_ids, (
            "CJK search with personality_filter should NOT return alpha session"
        )

    def test_search_excludes_null_personality(self, db):
        """search_messages with personality_filter excludes sessions with NULL personality."""
        db.create_session(session_id="s_bound", source="telegram", personality="pm")
        db.create_session(session_id="s_unbound", source="telegram", personality=None)

        db.append_message("s_bound", "user", "PM discussion about roadmap")
        db.append_message("s_unbound", "user", "Unbound discussion about things")

        results = db.search_messages("discussion", personality_filter="pm")
        session_ids = [r.get("session_id") for r in results]
        assert "s_bound" in session_ids
        assert "s_unbound" not in session_ids, (
            "Session with NULL personality should be excluded by personality_filter"
        )

    def test_no_personality_filter_returns_all(self, db):
        """search_messages without filter returns all."""
        db.create_session(session_id="s_a", source="telegram", personality="pm")
        db.create_session(session_id="s_b", source="telegram", personality="dev")

        db.append_message("s_a", "user", "PM discussion about roadmap")
        db.append_message("s_b", "user", "Dev discussion about code")

        results = db.search_messages("discussion", personality_filter=None)
        session_ids = [r.get("session_id") for r in results]
        assert "s_a" in session_ids
        assert "s_b" in session_ids

    def test_create_duplicate_session_ignored(self, db):
        """Creating session with same session_id is idempotent (INSERT OR IGNORE)."""
        db.create_session(session_id="s_dup", source="telegram", personality="pm")
        # Second create should not raise and should not change personality
        db.create_session(session_id="s_dup", source="telegram", personality="dev")
        session = db.get_session("s_dup")
        assert session["personality"] == "pm", (
            "Duplicate create_session should keep original personality"
        )


# =========================================================================
# 5. Security: _enforce_wing attack simulation
# =========================================================================
class TestWingEnforcementSecurity:
    """Simulate LLM passing malicious wing parameters to bypass isolation.

    This tests the P0 fix: _enforce_wing() must force isolated souls to
    always use their own wing, regardless of what the LLM supplies.
    """

    # --- _enforce_wing unit-level tests ---

    @pytest.mark.parametrize("scope,own_wing", [
        ("lenx", "lenx"),
        ("alpha", "alpha"),
        ("doctor", "doctor"),
    ])
    def test_isolated_ignores_malicious_wing(self, scope, own_wing):
        """Isolated soul: _enforce_wing("other_wing") returns own wing."""
        p = _make_provider(scope)
        # Simulate LLM trying to read another soul's memories
        assert p._enforce_wing("_global") == own_wing
        assert p._enforce_wing("lenx") == own_wing
        assert p._enforce_wing("alpha") == own_wing
        assert p._enforce_wing("doctor") == own_wing
        assert p._enforce_wing(None) == own_wing

    def test_shared_allows_none_fallback_to_global(self):
        """Shared soul: _enforce_wing(None) returns _global."""
        p = _make_provider("shared")
        assert p._enforce_wing(None) == "_global"

    def test_shared_ignores_cross_wing_attempt(self):
        """Shared soul: _enforce_wing("lenx") returns "lenx" (shared allows override).

        NOTE: shared mode trusts the LLM — this is by design because shared
        souls (pm/dev/etc) all share _global. If we want to restrict shared
        souls from accessing named wings, that would be a separate feature.
        """
        p = _make_provider("shared")
        assert p._enforce_wing("lenx") == "lenx"

    def test_all_mode_passes_through(self):
        """All mode: _enforce_wing("anything") returns "anything"."""
        p = _make_provider("*")
        assert p._enforce_wing("lenx") == "lenx"
        assert p._enforce_wing("_global") == "_global"
        assert p._enforce_wing(None) is None

    def test_disabled_mode_not_reached(self):
        """Disabled mode: callers early-return before _enforce_wing is called."""
        p = _make_provider("-")
        # _tool_search/_tool_add/_tool_delete all early-return for disabled
        search_result = json.loads(p._tool_search({"query": "anything", "wing": "lenx"}))
        assert search_result["total"] == 0

    # --- _tool_search with malicious wing param (mocked ChromaDB) ---

    @pytest.mark.parametrize("scope,expected_wing", [
        ("lenx", "lenx"),
        ("alpha", "alpha"),
        ("doctor", "doctor"),
    ])
    def test_search_isolated_uses_own_wing_not_llm_supplied(self, scope, expected_wing):
        """_tool_search with wing="stolen_wing" → ChromaDB query uses own wing."""
        p = _make_provider(scope)
        p._initialized = True

        captured_where = {}
        def mock_query(self_obj, **kwargs):
            captured_where.update(kwargs.get("where", {}))
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        mock_col = type("FakeCol", (), {"query": mock_query})()
        p._get_collection = lambda: mock_col

        # LLM tries to search "lenx" memories while being "alpha"
        p._tool_search({"query": "secret data", "wing": "stolen_wing"})

        assert captured_where.get("wing") == expected_wing, (
            f"scope={scope}: ChromaDB query used wing='{captured_where.get('wing')}', "
            f"expected '{expected_wing}' — LLM wing override NOT blocked!"
        )

    def test_search_shared_with_no_wing_uses_global(self):
        """Shared soul: _tool_search without wing → uses _global."""
        p = _make_provider("shared")
        p._initialized = True

        captured_where = {}
        def mock_query(self_obj, **kwargs):
            captured_where.update(kwargs.get("where", {}))
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        mock_col = type("FakeCol", (), {"query": mock_query})()
        p._get_collection = lambda: mock_col

        p._tool_search({"query": "shared query"})
        assert captured_where.get("wing") == "_global"

    # --- _tool_add with malicious wing param ---

    @pytest.mark.parametrize("scope,expected_wing", [
        ("lenx", "lenx"),
        ("alpha", "alpha"),
    ])
    def test_add_isolated_ignores_malicious_wing(self, scope, expected_wing):
        """_tool_add with wing="stolen_wing" → drawer created in own wing."""
        p = _make_provider(scope)

        captured_wing = {}
        def mock_add_drawer(content, wing, room, importance):
            captured_wing["wing"] = wing
            captured_wing["room"] = room
            return ("drawer-123", "added")

        p._add_drawer = mock_add_drawer
        p._ensure_kg = lambda: None

        # LLM tries to write to another soul's wing
        result = json.loads(p._tool_add({
            "content": "poisoned memory",
            "wing": "stolen_wing",
            "room": "secrets",
        }))

        assert result.get("success") is True
        assert captured_wing["wing"] == expected_wing, (
            f"scope={scope}: _add_drawer called with wing='{captured_wing['wing']}', "
            f"expected '{expected_wing}' — LLM wing override NOT blocked!"
        )

    # --- _tool_delete with cross-wing drawer ---

    def test_delete_isolated_blocks_cross_wing_drawer(self):
        """Isolated soul cannot delete a drawer belonging to another wing."""
        p = _make_provider("lenx")
        p._initialized = True

        # Mock ChromaDB collection with a drawer from "alpha" wing
        def mock_get(self_obj, **kwargs):
            return {
                "ids": ["drawer-alpha-1"],
                "metadatas": [{"wing": "alpha", "room": "secrets"}],
            }
        mock_col = type("FakeCol", (), {
            "get": mock_get,
            "delete": lambda self_obj, **kwargs: None,
        })()
        p._get_collection = lambda: mock_col

        result = json.loads(p._tool_delete({"drawer_id": "drawer-alpha-1"}))

        assert result.get("success") is False
        assert "isolated" in result.get("error", "").lower(), (
            f"Expected 'isolated' in error message, got: {result.get('error')}"
        )

    def test_delete_isolated_allows_own_wing_drawer(self):
        """Isolated soul CAN delete a drawer from its own wing."""
        p = _make_provider("lenx")
        p._initialized = True

        def mock_get(self_obj, **kwargs):
            return {
                "ids": ["drawer-lenx-1"],
                "metadatas": [{"wing": "lenx", "room": "notes"}],
            }
        deleted = []
        mock_col = type("FakeCol", (), {
            "get": mock_get,
            "delete": lambda self_obj, **kw: deleted.extend(kw.get("ids", [])),
        })()
        p._get_collection = lambda: mock_col

        result = json.loads(p._tool_delete({"drawer_id": "drawer-lenx-1"}))

        assert result.get("success") is True
        assert "drawer-lenx-1" in deleted

    # --- _tool_status isolation ---

    def test_status_isolated_only_shows_own_wing(self):
        """Isolated soul: _tool_status only reports own wing's drawer count."""
        p = _make_provider("lenx")
        p._initialized = True

        # Mock collection with drawers in multiple wings
        def mock_get(self_obj, **kwargs):
            return {
                "ids": ["d1", "d2", "d3"],
                "metadatas": [
                    {"wing": "lenx", "room": "notes"},
                    {"wing": "alpha", "room": "secrets"},
                    {"wing": "lenx", "room": "tasks"},
                ],
            }
        mock_col = type("FakeCol", (), {"get": mock_get})()
        p._get_collection = lambda: mock_col
        p._ensure_kg = lambda: None

        result = json.loads(p._tool_status({}))

        # Should only count lenx drawers (2), not alpha (1)
        assert result["total_drawers"] == 2, (
            f"Expected 2 (lenx only), got {result['total_drawers']} — leaking other wings!"
        )
        assert "alpha" not in result.get("wings", {}), (
            f"Status should not contain alpha wing: {result.get('wings')}"
        )

    def test_status_shared_shows_all_wings(self):
        """Shared soul: _tool_status shows all wings (full visibility)."""
        p = _make_provider("shared")
        p._initialized = True

        def mock_get(self_obj, **kwargs):
            return {
                "ids": ["d1", "d2", "d3"],
                "metadatas": [
                    {"wing": "_global", "room": "notes"},
                    {"wing": "alpha", "room": "secrets"},
                    {"wing": "_global", "room": "tasks"},
                ],
            }
        mock_col = type("FakeCol", (), {"get": mock_get})()
        p._get_collection = lambda: mock_col
        p._ensure_kg = lambda: None

        result = json.loads(p._tool_status({}))

        assert result["total_drawers"] == 3
        assert "_global" in result.get("wings", {})
        assert "alpha" in result.get("wings", {})


# =========================================================================
# 6. Session search personality isolation
# =========================================================================
class TestSessionSearchIsolation:
    """Verify session_search respects personality_filter across all paths.

    The call chain is:
      run_agent.py → session_search(query, personality_filter=self._personality)
        → db.search_messages(query, personality_filter=...)
        → FTS5 or LIKE fallback with personality WHERE clause

    We test at the db.search_messages level (the actual isolation gate).
    """

    @pytest.fixture
    def multi_soul_db(self, tmp_path):
        """Create a SessionDB with messages from multiple souls."""
        from hermes_state import SessionDB
        db = SessionDB(tmp_path / "test.db")

        # lenx sessions
        db.create_session(session_id="lenx_1", source="telegram", personality="lenx")
        db.append_message("lenx_1", "user", "Lenx 小紅書爬蟲設定")
        db.append_message("lenx_1", "assistant", "已設定爬蟲關鍵字：香港美食")

        db.create_session(session_id="lenx_2", source="telegram", personality="lenx")
        db.append_message("lenx_2", "user", "Lenx 分析報告完成")
        db.append_message("lenx_2", "assistant", "報告已匯出 PDF")

        # alpha sessions (should NOT be visible to lenx)
        db.create_session(session_id="alpha_1", source="discord", personality="alpha")
        db.append_message("alpha_1", "user", "Alpha 客戶專案進度更新")
        db.append_message("alpha_1", "assistant", "Shopilala 項目已完成部署")

        db.create_session(session_id="alpha_2", source="discord", personality="alpha")
        db.append_message("alpha_2", "user", "Alpha 財務報告準備")
        db.append_message("alpha_2", "assistant", "季度收入增長 15%")

        # dev sessions (shared soul, should NOT be visible to lenx)
        db.create_session(session_id="dev_1", source="telegram", personality="dev")
        db.append_message("dev_1", "user", "Dev 修復了 gateway bug")
        db.append_message("dev_1", "assistant", "hermes_state.py 已 patch")

        # Session with no personality (unbound)
        db.create_session(session_id="unbound_1", source="telegram", personality=None)
        db.append_message("unbound_1", "user", "未綁定靈魂的對話")

        yield db
        db.close()

    # --- FTS5 path isolation ---

    def test_lenx_cannot_see_alpha_sessions_fts5(self, multi_soul_db):
        """lenx searching common terms cannot see alpha/dev sessions."""
        results = multi_soul_db.search_messages(
            "報告", personality_filter="lenx"
        )
        session_ids = [r.get("session_id") for r in results]
        assert "lenx_2" in session_ids, "lenx should find own session about 報告"
        assert "alpha_2" not in session_ids, (
            "lenx should NOT see alpha's session about 報告"
        )

    def test_lenx_cannot_see_dev_sessions_fts5(self, multi_soul_db):
        """lenx searching broad terms cannot see dev sessions."""
        results = multi_soul_db.search_messages(
            "設定", personality_filter="lenx"
        )
        session_ids = [r.get("session_id") for r in results]
        assert "lenx_1" in session_ids
        # dev_1 talks about gateway fix — no "設定" but let's check no cross-leak
        assert "alpha_1" not in session_ids
        assert "dev_1" not in session_ids

    def test_alpha_cannot_see_lenx_sessions_fts5(self, multi_soul_db):
        """alpha searching common terms cannot see lenx sessions."""
        results = multi_soul_db.search_messages(
            "報告", personality_filter="alpha"
        )
        session_ids = [r.get("session_id") for r in results]
        assert "alpha_2" in session_ids, "alpha should find own session about 報告"
        assert "lenx_2" not in session_ids, (
            "alpha should NOT see lenx's session about 報告"
        )

    def test_unbound_session_excluded_by_filter(self, multi_soul_db):
        """Sessions with NULL personality are excluded when filter is active."""
        results = multi_soul_db.search_messages(
            "對話", personality_filter="lenx"
        )
        session_ids = [r.get("session_id") for r in results]
        assert "unbound_1" not in session_ids, (
            "Unbound session (NULL personality) should be excluded"
        )

    def test_no_filter_returns_all_sessions(self, multi_soul_db):
        """Without personality_filter, all sessions are returned."""
        results = multi_soul_db.search_messages(
            "報告", personality_filter=None
        )
        session_ids = [r.get("session_id") for r in results]
        assert "lenx_2" in session_ids
        assert "alpha_2" in session_ids

    # --- session_search tool level ---

    def test_session_search_tool_respects_filter(self, multi_soul_db):
        """session_search() function filters by personality — no cross-soul leak."""
        from tools.session_search_tool import session_search

        # lenx searches for "報告"
        result = json.loads(session_search(
            query="報告",
            db=multi_soul_db,
            current_session_id="lenx_current",
            personality_filter="lenx",
        ))
        assert result.get("success") is True
        # Parse actual results — check no alpha/dev session IDs leaked
        result_text = json.dumps(result, ensure_ascii=False)
        assert "alpha_1" not in result_text, "lenx search should not leak alpha_1"
        assert "alpha_2" not in result_text, "lenx search should not leak alpha_2"
        assert "dev_1" not in result_text, "lenx search should not leak dev_1"

    def test_session_search_recent_mode_filters(self, multi_soul_db):
        """session_search with empty query (recent mode) filters by personality."""
        from tools.session_search_tool import session_search

        result = json.loads(session_search(
            query="",
            db=multi_soul_db,
            current_session_id="lenx_current",
            personality_filter="lenx",
            limit=10,
        ))
        assert result.get("success") is True
        assert result.get("mode") == "recent"
        session_ids = [r.get("session_id") for r in result.get("results", [])]
        # lenx should only see lenx sessions
        for sid in session_ids:
            assert sid.startswith("lenx") or sid == "lenx_current", (
                f"Recent mode leaked non-lensex session: {sid}"
            )
        assert "alpha_1" not in session_ids
        assert "alpha_2" not in session_ids
        assert "dev_1" not in session_ids
        assert "unbound_1" not in session_ids

    # --- Cross-soul attack simulation ---

    def test_lenx_cannot_find_alpha_client_data(self, multi_soul_db):
        """Simulate: lenx tries to find alpha's client project info."""
        results = multi_soul_db.search_messages(
            "專案", personality_filter="lenx"
        )
        session_ids = [r.get("session_id") for r in results]
        assert "alpha_1" not in session_ids, (
            "lenx should NOT find alpha's client project data"
        )

    def test_lenx_cannot_find_alpha_financial_data(self, multi_soul_db):
        """Simulate: lenx tries to find alpha's financial reports."""
        results = multi_soul_db.search_messages(
            "收入", personality_filter="lenx"
        )
        session_ids = [r.get("session_id") for r in results]
        assert "alpha_2" not in session_ids, (
            "lenx should NOT find alpha's financial data"
        )


# =========================================================================
# 7. Write + Read round-trip: personality column correctness
# =========================================================================
class TestPersonalityColumnRoundTrip:
    """Verify personality is correctly written to sessions table
    and correctly used in all read/search paths.

    This tests the actual DB schema:
      sessions.personality TEXT  ← set at create_session()
      messages.session_id → JOIN sessions → s.personality
    """

    @pytest.fixture
    def db(self, tmp_path):
        from hermes_state import SessionDB
        database = SessionDB(tmp_path / "roundtrip.db")
        yield database
        database.close()

    @pytest.fixture
    def search_db(self, tmp_path):
        """Create a SessionDB for search isolation tests."""
        from hermes_state import SessionDB
        database = SessionDB(tmp_path / "search.db")
        yield database
        database.close()

    # --- Write path: create_session stores personality correctly ---

    def test_create_session_stores_personality(self, db):
        """create_session(personality='lenx') → DB row has personality='lenx'."""
        db.create_session(session_id="s1", source="telegram", personality="lenx")
        row = db.get_session("s1")
        assert row is not None, "Session should exist"
        assert row["personality"] == "lenx", (
            f"Expected personality='lenx', got '{row['personality']}'"
        )

    def test_create_session_stores_none_personality(self, db):
        """create_session(personality=None) → DB row has personality=NULL."""
        db.create_session(session_id="s2", source="telegram", personality=None)
        row = db.get_session("s2")
        assert row is not None
        assert row["personality"] is None

    def test_create_session_stores_empty_string_personality(self, db):
        """create_session(personality='') → DB row has personality=''."""
        db.create_session(session_id="s3", source="telegram", personality="")
        row = db.get_session("s3")
        assert row is not None
        assert row["personality"] == ""

    @pytest.mark.parametrize("soul_name", [
        "pm", "dev", "research", "operation", "accountant",
        "alpha", "lenx", "doctor",
    ])
    def test_all_soul_names_stored_correctly(self, db, soul_name):
        """Each real soul name is stored and retrievable without corruption."""
        sid = f"session_{soul_name}"
        db.create_session(session_id=sid, source="telegram", personality=soul_name)
        row = db.get_session(sid)
        assert row["personality"] == soul_name, (
            f"Soul '{soul_name}': stored personality='{row['personality']}'"
        )

    # --- Write path: update_session_personality ---

    def test_update_personality_overwrites_correctly(self, db):
        """update_session_personality changes the stored value."""
        db.create_session(session_id="s1", source="telegram", personality="lenx")
        assert db.get_session("s1")["personality"] == "lenx"

        db.update_session_personality("s1", "alpha")
        assert db.get_session("s1")["personality"] == "alpha"

    def test_update_personality_from_none_to_soul(self, db):
        """Gateway restart scenario: NULL → actual soul name."""
        db.create_session(session_id="s2", source="telegram", personality=None)
        assert db.get_session("s2")["personality"] is None

        db.update_session_personality("s2", "doctor")
        assert db.get_session("s2")["personality"] == "doctor"

    # --- Read path: list_sessions_rich includes personality ---

    def test_list_sessions_rich_returns_personality(self, db):
        """list_sessions_rich() includes personality column in results."""
        db.create_session(session_id="s1", source="telegram", personality="lenx")
        db.create_session(session_id="s2", source="discord", personality="alpha")
        db.create_session(session_id="s3", source="telegram", personality=None)

        sessions = db.list_sessions_rich(limit=10)
        personalities = {s["id"]: s.get("personality") for s in sessions}

        assert personalities.get("s1") == "lenx"
        assert personalities.get("s2") == "alpha"
        assert personalities.get("s3") is None

    # --- Read path: search_messages JOINs correctly ---

    def test_messages_inherit_session_personality(self, db):
        """Messages written under lenx session are found by personality_filter='lenx'."""
        db.create_session(session_id="s_lenx", source="telegram", personality="lenx")
        db.create_session(session_id="s_alpha", source="discord", personality="alpha")

        db.append_message("s_lenx", "user", "lenx 專屬資料 ABC")
        db.append_message("s_alpha", "user", "alpha 專屬資料 ABC")

        # Search with "ABC" — both have it
        lenx_results = db.search_messages("ABC", personality_filter="lenx")
        alpha_results = db.search_messages("ABC", personality_filter="alpha")

        lenx_ids = [r["session_id"] for r in lenx_results]
        alpha_ids = [r["session_id"] for r in alpha_results]

        assert "s_lenx" in lenx_ids, "lenx should find own message"
        assert "s_alpha" not in lenx_ids, "lenx should NOT find alpha's message"
        assert "s_alpha" in alpha_ids, "alpha should find own message"
        assert "s_lenx" not in alpha_ids, "alpha should NOT find lenx's message"

    def test_messages_without_personality_excluded_when_filtered(self, db):
        """Messages in NULL-personality sessions are excluded by personality_filter."""
        db.create_session(session_id="s_lenx", source="telegram", personality="lenx")
        db.create_session(session_id="s_orphan", source="telegram", personality=None)

        db.append_message("s_lenx", "user", "lenx shared keyword XYZ")
        db.append_message("s_orphan", "user", "orphan shared keyword XYZ")

        results = db.search_messages("XYZ", personality_filter="lenx")
        session_ids = [r["session_id"] for r in results]

        assert "s_lenx" in session_ids
        assert "s_orphan" not in session_ids, (
            "NULL personality session should be excluded when filter is active"
        )

    def test_cjk_messages_isolated_by_personality(self, db):
        """CJK content in different personality sessions is properly isolated."""
        db.create_session(session_id="s_lenx", source="telegram", personality="lenx")
        db.create_session(session_id="s_alpha", source="discord", personality="alpha")

        db.append_message("s_lenx", "user", "lenx客戶的香港美食報告")
        db.append_message("s_alpha", "user", "alpha客戶的財務報告")

        lenx_results = db.search_messages("報告", personality_filter="lenx")
        alpha_results = db.search_messages("報告", personality_filter="alpha")

        lenx_ids = [r["session_id"] for r in lenx_results]
        alpha_ids = [r["session_id"] for r in alpha_results]

        assert "s_lenx" in lenx_ids
        assert "s_alpha" not in lenx_ids
        assert "s_alpha" in alpha_ids
        assert "s_lenx" not in alpha_ids

    # --- Write + Read: multiple messages per session ---

    def test_all_messages_in_session_share_personality(self, db):
        """All messages in one session inherit the same personality."""
        db.create_session(session_id="s_multi", source="telegram", personality="lenx")

        db.append_message("s_multi", "user", "第一條訊息")
        db.append_message("s_multi", "assistant", "第二條回覆")
        db.append_message("s_multi", "user", "第三條訊息 unique_keyword")

        # All 3 messages should be found with personality_filter="lenx"
        results = db.search_messages("unique_keyword", personality_filter="lenx")
        assert len(results) >= 1, "Should find the message with unique_keyword"

        # Verify the session_id is correct
        assert results[0]["session_id"] == "s_multi"

    # --- Edge case: same content, different personalities ---

    def test_identical_content_isolated_by_personality(self, db):
        """Same message content in different sessions is correctly isolated."""
        db.create_session(session_id="s_lenx", source="telegram", personality="lenx")
        db.create_session(session_id="s_alpha", source="discord", personality="alpha")

        # Exact same content
        identical_text = "這是一段完全相同的測試內容"
        db.append_message("s_lenx", "user", identical_text)
        db.append_message("s_alpha", "user", identical_text)

        lenx_results = db.search_messages("完全相同", personality_filter="lenx")
        alpha_results = db.search_messages("完全相同", personality_filter="alpha")

        lenx_ids = [r["session_id"] for r in lenx_results]
        alpha_ids = [r["session_id"] for r in alpha_results]

        assert lenx_ids == ["s_lenx"], f"lenx should only find s_lenx, got {lenx_ids}"
        assert alpha_ids == ["s_alpha"], f"alpha should only find s_alpha, got {alpha_ids}"


# =========================================================================
# 6. End-to-end scope matrix
# =========================================================================
class TestScopeMatrix:
    """Full matrix: scope → expected behavior across all subsystems."""

    MATRIX = {
        "shared": {
            "dirs_include_global": True,
            "wing_mode": "shared",
            "wing": "_global",
            "label": "pm/dev/research/operation/accountant",
        },
        "alpha": {
            "dirs_include_global": False,
            "wing_mode": "isolated",
            "wing": "alpha",
            "label": "alpha",
        },
        "lenx": {
            "dirs_include_global": False,
            "wing_mode": "isolated",
            "wing": "lenx",
            "label": "lenx",
        },
        "doctor": {
            "dirs_include_global": False,
            "wing_mode": "isolated",
            "wing": "doctor",
            "label": "doctor",
        },
        "-": {
            "dirs_include_global": False,
            "wing_mode": "disabled",
            "wing": None,
            "label": "fed",
        },
    }

    @pytest.mark.parametrize("scope,expected", list(MATRIX.items()),
                             ids=[v["label"] for v in MATRIX.values()])
    def test_scope_matrix(self, scope, expected):
        # --- resolve_memory_dirs ---
        from gateway.extensions.channel_binding import resolve_memory_dirs
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "memories"
            base.mkdir()
            (base / "_global").mkdir()
            if scope not in ("-", "shared"):
                (base / scope).mkdir()

            with patch("hermes_constants.get_hermes_home", return_value=Path(td)):
                dirs = resolve_memory_dirs(scope)
                dir_names = [d.name for d in dirs]

                if expected["dirs_include_global"]:
                    assert "_global" in dir_names, f"scope={scope}: expected _global in dirs"
                else:
                    assert "_global" not in dir_names, f"scope={scope}: _global should not be in dirs"

        # --- MemPalace wing mode ---
        provider = _make_provider(scope)

        assert provider._wing_mode == expected["wing_mode"], (
            f"scope={scope}: wing_mode={provider._wing_mode}, expected={expected['wing_mode']}"
        )
        assert provider._wing == expected["wing"], (
            f"scope={scope}: wing={provider._wing}, expected={expected['wing']}"
        )


# =========================================================================
# 8. Additional coverage — missing paths from code review
# =========================================================================
class TestCoverageGaps:
    """Tests covering gaps identified in code review:
    - ChromaDB unavailable paths
    - _enforce_wing for disabled mode (direct call)
    - Shared mode delete
    - Disabled/all mode status
    - Empty content rejection
    - Nonexistent personality filter
    """

    # --- ChromaDB unavailable (P1: error path coverage) ---

    def test_search_returns_error_when_chromadb_unavailable(self):
        """_tool_search with no collection returns error, not crash."""
        p = _make_provider("lenx")
        p._initialized = True
        p._get_collection = lambda: None

        result = json.loads(p._tool_search({"query": "anything"}))
        # Production returns {"error": "ChromaDB not initialized", "results": []}
        assert "error" in result or result.get("total") == 0, (
            f"Expected graceful error when ChromaDB unavailable, got: {result}"
        )

    def test_delete_returns_error_when_chromadb_unavailable(self):
        """_tool_delete with no collection returns error, not crash."""
        p = _make_provider("lenx")
        p._initialized = True
        p._get_collection = lambda: None

        result = json.loads(p._tool_delete({"drawer_id": "any-id"}))
        # Production returns {"error": "..."} when collection unavailable
        assert "error" in result or result.get("success") is False, (
            f"Expected error when ChromaDB unavailable, got: {result}"
        )

    # --- _enforce_wing direct: disabled mode (P2: coverage) ---

    def test_enforce_wing_disabled_mode_returns_none_or_blocked(self):
        """_enforce_wing in disabled mode should return None (blocked)."""
        p = _make_provider("-")
        # Disabled mode: _wing_mode="disabled", _wing=None
        # Direct call — what happens if early-return is bypassed?
        result = p._enforce_wing("_global")
        # In disabled mode, _wing is None — enforce_wing returns None
        # (because self._wing is None and mode doesn't match shared/isolated/all)
        assert result is None, (
            f"Disabled mode _enforce_wing should return None, got: {result}"
        )

    # --- Shared mode delete (P1: no wing ownership check) ---

    def test_delete_shared_allows_any_wing_drawer(self):
        """Shared soul can delete drawers from ANY wing (by design)."""
        p = _make_provider("shared")
        p._initialized = True

        # Mock collection with a drawer from "lenx" wing
        def mock_get(self_obj, **kwargs):
            return {
                "ids": ["drawer-lenx-1"],
                "metadatas": [{"wing": "lenx", "room": "notes"}],
            }
        deleted = []
        mock_col = type("FakeCol", (), {
            "get": mock_get,
            "delete": lambda self_obj, **kw: deleted.extend(kw.get("ids", [])),
        })()
        p._get_collection = lambda: mock_col

        result = json.loads(p._tool_delete({"drawer_id": "drawer-lenx-1"}))

        assert result.get("success") is True, (
            f"Shared mode should allow deleting any wing drawer, got: {result}"
        )
        assert "drawer-lenx-1" in deleted

    # --- Status: disabled and all modes (P1) ---

    def test_status_disabled_returns_zero(self):
        """Disabled mode: _tool_status returns zero drawers."""
        p = _make_provider("-")
        p._initialized = True
        p._ensure_kg = lambda: None

        result = json.loads(p._tool_status({}))
        assert result.get("total_drawers") == 0, (
            f"Disabled mode status should show 0 drawers, got: {result}"
        )

    def test_status_all_mode_shows_all_wings(self):
        """All mode (*) status shows all wings without filtering."""
        p = _make_provider("*")
        p._initialized = True

        def mock_get(self_obj, **kwargs):
            return {
                "ids": ["d1", "d2", "d3"],
                "metadatas": [
                    {"wing": "lenx", "room": "notes"},
                    {"wing": "alpha", "room": "secrets"},
                    {"wing": "_global", "room": "tasks"},
                ],
            }
        mock_col = type("FakeCol", (), {"get": mock_get})()
        p._get_collection = lambda: mock_col
        p._ensure_kg = lambda: None

        result = json.loads(p._tool_status({}))
        assert result["total_drawers"] == 3, (
            f"All mode should show all 3 drawers, got {result['total_drawers']}"
        )
        assert "lenx" in result.get("wings", {})
        assert "alpha" in result.get("wings", {})

    # --- Empty content rejection (P1) ---

    def test_add_rejects_empty_content(self):
        """_tool_add with empty/whitespace content is rejected."""
        p = _make_provider("lenx")
        p._ensure_kg = lambda: None
        p._add_drawer = lambda *a, **kw: ("id", "added")  # shouldn't be called

        for content in ["", "   ", "\t\n"]:
            result = json.loads(p._tool_add({
                "content": content,
                "room": "test",
            }))
            # Production returns {"error": "Content cannot be empty"}
            has_error = "error" in result
            assert has_error, (
                f"Empty content '{repr(content)}' should be rejected, got: {result}"
            )

    # --- Nonexistent personality filter (P1) ---

    def test_personality_filter_nonexistent_returns_empty(self, tmp_path):
        """Searching with a nonexistent personality returns zero results."""
        from hermes_state import SessionDB
        db = SessionDB(tmp_path / "nonexist.db")
        try:
            db.create_session(session_id="s1", source="telegram", personality="lenx")
            db.append_message("s1", "user", "Some searchable content here")

            results = db.search_messages("searchable", personality_filter="nonexistent_soul")
            assert len(results) == 0, (
                f"Nonexistent personality filter should return 0, got {len(results)}: {results}"
            )
        finally:
            db.close()
