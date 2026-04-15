"""
MemPalace Memory Provider for Hermes Agent
============================================

Integrates MemPalace's structured long-term memory as a Hermes MemoryProvider
plugin.  Provides:

- **Layer stack** (L0 identity + L1 essential story) injected into system prompt
  for ~600–900 token wake-up cost.
- **Semantic search** via ``mempalace_search`` tool.
- **Drawer filing** via ``mempalace_add`` tool (mirrors builtin ``memory add``).
- **Scope → Wing mapping**: ``memory_scope`` from channel binding maps directly
  to MemPalace ``wing`` parameter for per-channel memory isolation.
- **Temporal knowledge graph** (optional) for time-aware fact tracking.
- **on_memory_write hook**: mirrors builtin MEMORY.md writes as palace drawers.

Config (config.yaml):

    memory:
      provider: mempalace
      mempalace:
        data_path: "~/.hermes/mempalace"
        embedding_model: "BAAI/bge-small-zh-v1.5"
        enable_kg: true
        recall_mode: hybrid    # context | tools | hybrid
        context_tokens: 800

Registration::

    from plugins.memory.mempalace import register
    ctx.register_memory_provider(MemPalaceProvider())
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from plugins.memory.mempalace.config import MemPalaceConfig

logger = logging.getLogger(__name__)


class MemPalaceProvider(MemoryProvider):
    """MemPalace structured memory provider."""

    def __init__(self) -> None:
        self._cfg: Optional[MemPalaceConfig] = None
        self._wing: Optional[str] = None
        self._session_id: str = ""
        self._platform: str = "cli"
        self._hermes_home: str = ""
        self._agent_context: Optional[str] = None
        self._initialized: bool = False
        self._cron_skipped: bool = False
        self._turn_number: int = 0
        self._layers_baked: bool = False
        self._cached_system_block: str = ""
        self._cached_prefetch: str = ""
        self._prefetch_lock: threading.Lock = threading.Lock()
        self._kg = None  # KnowledgeGraph, lazy init
        self._chroma_client = None
        self._collection = None

    # -- MemoryProvider ABC ---------------------------------------------------

    @property
    def name(self) -> str:
        return "mempalace"

    def is_available(self) -> bool:
        """Check if mempalace package is installed and palace path exists or can be created."""
        try:
            import mempalace  # noqa: F401
            return True
        except ImportError:
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session."""
        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home", "")
        self._platform = kwargs.get("platform", "cli")
        self._agent_context = kwargs.get("agent_context")

        # Cron/flush guard — don't do memory work for system contexts
        if self._agent_context in ("cron", "flush"):
            self._cron_skipped = True
            return

        # Load config
        self._cfg = MemPalaceConfig(hermes_home=self._hermes_home)

        # Scope → Wing mapping from channel binding
        self._wing = kwargs.get("memory_scope") or None

        # Ensure palace directory exists
        palace_path = self._cfg.data_path
        os.makedirs(palace_path, exist_ok=True)

        # Set MemPalace env so its internal config resolves to our path
        os.environ["MEMPALACE_PALACE_PATH"] = palace_path

        # Init ChromaDB with Ollama embedding (bge-m3 for zh+en)
        self._init_chroma(palace_path)

        if self._collection is None:
            logger.error("MemPalace init failed: ChromaDB collection not available")
            return

        self._initialized = True
        self._layers_baked = False
        self._turn_number = 0

        logger.info(
            "MemPalace initialized: session=%s wing=%s palace=%s",
            session_id, self._wing, palace_path,
        )

    def system_prompt_block(self) -> str:
        """Return L0+L1 context for the system prompt (~600-900 tokens)."""
        if self._cron_skipped or not self._initialized:
            return ""

        if self._layers_baked:
            return self._cached_system_block

        try:
            parts = []

            # L0 — Identity
            try:
                from mempalace.layers import Layer0
                identity_path = os.path.join(self._cfg.data_path, "identity.txt")
                l0 = Layer0(identity_path=identity_path)
                l0_text = l0.render()
                if l0_text and not l0_text.startswith("## L0 — IDENTITY\nNo identity"):
                    parts.append(l0_text)
            except Exception as e:
                logger.debug("L0 render failed: %s", e)

            # L1 — Essential Story
            try:
                from mempalace.layers import Layer1
                l1 = Layer1(
                    palace_path=self._cfg.data_path,
                    wing=self._wing,
                )
                l1_text = l1.generate()
                if l1_text and not l1_text.startswith("## L1 — No"):
                    parts.append(l1_text)
            except Exception as e:
                logger.debug("L1 generate failed: %s", e)

            if parts:
                block = "\n\n".join(parts)
                # Truncate to token budget
                max_chars = self._cfg.context_tokens * 4
                if len(block) > max_chars:
                    block = block[:max_chars] + "\n...[truncated]"
                self._cached_system_block = block
            else:
                self._cached_system_block = ""

        except Exception as e:
            logger.warning("MemPalace system_prompt_block failed: %s", e)
            self._cached_system_block = ""

        self._layers_baked = True
        return self._cached_system_block

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return cached background prefetch result."""
        if self._cron_skipped or not self._initialized:
            return ""
        with self._prefetch_lock:
            return self._cached_prefetch

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire background semantic search for next turn (deduplicated)."""
        if self._cron_skipped or not self._initialized:
            return
        if self._cfg.recall_mode == "context":
            with self._prefetch_lock:
                if getattr(self, "_prefetch_running", False):
                    return  # Already prefetching, skip
                self._prefetch_running = True
            t = threading.Thread(
                target=self._bg_prefetch, args=(query,), daemon=True,
            )
            t.start()

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "") -> None:
        """Record turn for potential mining. Non-blocking."""
        if self._cron_skipped or not self._initialized:
            return
        # We don't auto-mine every turn (too expensive).  The user can
        # explicitly mine via the CLI or the mempalace_mine tool.
        # But we track the turn count.
        self._turn_number += 1

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Track turn count for cadence."""
        self._turn_number = turn_number

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Flush at session end."""
        pass  # No-op for now; mining is explicit

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror builtin memory writes to MemPalace as drawers."""
        if self._cron_skipped or not self._initialized:
            return
        if action != "add":
            return
        try:
            room = f"builtin_{target}"  # "builtin_memory" or "builtin_user"
            self._add_drawer(
                content=content,
                wing=self._wing or "shared",
                room=room,
                importance=3,
            )
        except Exception as e:
            logger.debug("Mirror memory write to MemPalace failed: %s", e)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas for MemPalace tools."""
        if self._cron_skipped or not self._initialized:
            return []

        mode = self._cfg.recall_mode
        if mode == "context":
            # Context-only mode — no tools, everything is auto-injected
            return []

        schemas = [_SCHEMA_MEMPALACE_SEARCH]

        if mode in ("tools", "hybrid"):
            schemas.append(_SCHEMA_MEMPALACE_ADD)
            schemas.append(_SCHEMA_MEMPALACE_STATUS)

        return schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch tool calls."""
        if tool_name == "mempalace_search":
            return self._tool_search(args)
        elif tool_name == "mempalace_add":
            return self._tool_add(args)
        elif tool_name == "mempalace_status":
            return self._tool_status(args)
        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Return config fields for 'hermes memory setup'."""
        return [
            {
                "key": "data_path",
                "description": "Path to MemPalace data directory",
                "default": "~/.hermes/mempalace",
                "required": False,
            },
            {
                "key": "embedding_model",
                "description": "Embedding model for semantic search",
                "default": "bge-m3",
                "required": False,
            },
            {
                "key": "enable_kg",
                "description": "Enable temporal knowledge graph",
                "default": True,
                "required": False,
                "choices": [True, False],
            },
            {
                "key": "recall_mode",
                "description": "How memories are surfaced: context (auto-inject), tools (agent calls), hybrid (both)",
                "default": "hybrid",
                "required": False,
                "choices": ["context", "tools", "hybrid"],
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Write config values into config.yaml's memory.mempalace section."""
        try:
            import yaml
            cfg_path = Path(hermes_home) / "config.yaml"
            root = {}
            if cfg_path.exists():
                with open(cfg_path) as f:
                    root = yaml.safe_load(f) or {}

            mem = root.setdefault("memory", {})
            mem["provider"] = "mempalace"
            mem["mempalace"] = values

            with open(cfg_path, "w") as f:
                yaml.dump(root, f, default_flow_style=False, sort_keys=False)

        except Exception as e:
            logger.error("Failed to save mempalace config: %s", e)

    def shutdown(self) -> None:
        """Clean shutdown."""
        if self._kg:
            try:
                self._kg.close()
            except Exception:
                pass
            self._kg = None

    # -- Internal -------------------------------------------------------------

    def _init_chroma(self, palace_path: str) -> None:
        """Init ChromaDB client with Ollama embedding function."""
        try:
            import chromadb
            from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

            ef = OllamaEmbeddingFunction(
                url="http://localhost:11434",
                model_name=self._cfg.embedding_model,
            )

            self._chroma_client = chromadb.PersistentClient(path=palace_path)
            self._collection = self._chroma_client.get_or_create_collection(
                name="mempalace_drawers",
                embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "ChromaDB initialized with Ollama/%s at %s",
                self._cfg.embedding_model, palace_path,
            )
        except Exception as e:
            logger.warning("ChromaDB init failed (will retry on access): %s", e)
            self._chroma_client = None
            self._collection = None

    def _get_collection(self):
        """Get collection, lazy-init if needed (thread-safe)."""
        if self._collection is not None:
            return self._collection
        with self._prefetch_lock:  # reuse existing lock for thread safety
            # Double-check after acquiring lock
            if self._collection is not None:
                return self._collection
            if self._cfg:
                self._init_chroma(self._cfg.data_path)
        return self._collection

    def _bg_prefetch(self, query: str) -> None:
        """Background thread: run semantic search and cache result."""
        try:
            from mempalace.layers import Layer3
            l3 = Layer3(palace_path=self._cfg.data_path)
            result = l3.search(
                query=query,
                wing=self._wing,
                n_results=self._cfg.search_n_results,
            )
            with self._prefetch_lock:
                self._cached_prefetch = result
        except Exception as e:
            logger.debug("Background prefetch failed: %s", e)
        finally:
            with self._prefetch_lock:
                self._prefetch_running = False

    def _add_drawer(self, content: str, wing: str, room: str,
                    importance: float = 3.0) -> str:
        """Add a drawer to the palace."""
        try:
            col = self._get_collection()
            if col is None:
                logger.error("No ChromaDB collection available")
                return ""

            drawer_id = hashlib.sha256(
                f"{content[:200]}:{time.time()}".encode()
            ).hexdigest()[:16]

            col.upsert(
                ids=[drawer_id],
                documents=[content],
                metadatas=[{
                    "wing": wing,
                    "room": room,
                    "importance": importance,
                    "filed_at": datetime.now().isoformat(),
                    "source": "hermes-agent",
                }],
            )
            return drawer_id

        except Exception as e:
            logger.error("Failed to add drawer: %s", e)
            return ""

    def _ensure_kg(self):
        """Lazy-init knowledge graph."""
        if self._kg is not None:
            return self._kg
        if not self._cfg.enable_kg:
            return None
        try:
            from mempalace.knowledge_graph import KnowledgeGraph
            kg_path = os.path.join(self._cfg.data_path, "knowledge_graph.sqlite3")
            self._kg = KnowledgeGraph(db_path=kg_path)
            return self._kg
        except Exception as e:
            logger.debug("KnowledgeGraph init failed: %s", e)
            return None

    def _tool_search(self, args: Dict[str, Any]) -> str:
        """Handle mempalace_search tool call."""
        query = args.get("query", "")
        wing = args.get("wing", self._wing)
        room = args.get("room")
        n_results = args.get("n_results", self._cfg.search_n_results)

        try:
            col = self._get_collection()
            if col is None:
                return json.dumps({"error": "ChromaDB not initialized", "results": []})

            where_filter = {}
            if wing:
                where_filter["wing"] = wing
            if room:
                where_filter["room"] = room

            query_kwargs = {
                "query_texts": [query],
                "n_results": n_results,
                "include": ["documents", "metadatas", "distances"],
            }
            if where_filter:
                query_kwargs["where"] = where_filter

            result = col.query(**query_kwargs)

            # Format results
            results = []
            if result and result.get("ids") and result["ids"][0]:
                for i, doc_id in enumerate(result["ids"][0]):
                    entry = {
                        "id": doc_id,
                        "content": result["documents"][0][i] if result["documents"] else "",
                        "distance": result["distances"][0][i] if result["distances"] else None,
                        "metadata": result["metadatas"][0][i] if result["metadatas"] else {},
                    }
                    # Filter by max_distance
                    if entry["distance"] is not None and entry["distance"] > self._cfg.search_max_distance:
                        continue
                    results.append(entry)

            return json.dumps({"results": results, "total": len(results)}, default=str)
        except Exception as e:
            return json.dumps({"error": str(e), "results": []})

    def _tool_add(self, args: Dict[str, Any]) -> str:
        """Handle mempalace_add tool call."""
        content = args.get("content", "")
        wing = args.get("wing", self._wing or "shared")
        room = args.get("room", "general")
        importance = args.get("importance", 3.0)

        if not content.strip():
            return json.dumps({"error": "Content cannot be empty"})

        drawer_id = self._add_drawer(content, wing, room, importance)
        if drawer_id:
            # Also add to knowledge graph if enabled
            kg = self._ensure_kg()
            if kg:
                try:
                    from mempalace.entity_detector import detect_entities
                    entities = detect_entities(content)
                    for ent in entities[:5]:  # cap at 5 to avoid overload
                        kg.add_entity(ent["name"], entity_type=ent.get("type", "unknown"))
                except Exception:
                    pass  # entity detection is best-effort

            return json.dumps({
                "success": True,
                "drawer_id": drawer_id,
                "wing": wing,
                "room": room,
            })
        return json.dumps({"error": "Failed to add drawer"})

    def _tool_status(self, args: Dict[str, Any]) -> str:
        """Handle mempalace_status tool call."""
        try:
            col = self._get_collection()
            if col is None:
                return json.dumps({
                    "total_drawers": 0,
                    "wings": {},
                    "current_wing": self._wing,
                    "error": "ChromaDB not initialized",
                })

            # Get total count
            all_data = col.get(include=["metadatas"])
            total = len(all_data["ids"])

            # Breakdown by wing
            wings: Dict[str, int] = {}
            for meta in all_data.get("metadatas", []):
                if meta:
                    w = meta.get("wing", "none")
                    wings[w] = wings.get(w, 0) + 1

            # Knowledge graph stats
            kg_stats = None
            kg = self._ensure_kg()
            if kg:
                try:
                    kg_stats = kg.stats()
                except Exception:
                    pass

            return json.dumps({
                "total_drawers": total,
                "wings": wings,
                "current_wing": self._wing,
                "knowledge_graph": kg_stats,
                "palace_path": self._cfg.data_path,
            }, default=str)

        except Exception as e:
            return json.dumps({
                "total_drawers": 0,
                "wings": {},
                "current_wing": self._wing,
                "error": str(e),
            })


# ---------------------------------------------------------------------------
# Tool Schemas
# ---------------------------------------------------------------------------

_SCHEMA_MEMPALACE_SEARCH = {
    "name": "mempalace_search",
    "description": (
        "Search long-term memories using semantic similarity. "
        "MemPalace stores structured memories organized by wings (scopes) and rooms. "
        "Use this to recall past conversations, learned facts, or user preferences. "
        "Optionally filter by wing (memory scope) and room."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query.",
            },
            "wing": {
                "type": "string",
                "description": "Filter by wing (memory scope). Defaults to current scope.",
            },
            "room": {
                "type": "string",
                "description": "Filter by room within a wing.",
            },
            "n_results": {
                "type": "integer",
                "description": "Number of results to return (default: 5).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

_SCHEMA_MEMPALACE_ADD = {
    "name": "mempalace_add",
    "description": (
        "File content into the MemPalace long-term memory as a drawer. "
        "Use this to remember important facts, user preferences, decisions, or key information "
        "that should persist across sessions. Content is stored with wing/room metadata "
        "and becomes searchable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The content to store in long-term memory.",
            },
            "wing": {
                "type": "string",
                "description": "Wing (scope) to store under. Defaults to current scope.",
            },
            "room": {
                "type": "string",
                "description": "Room within the wing (e.g. 'preferences', 'decisions'). Default: 'general'.",
                "default": "general",
            },
            "importance": {
                "type": "number",
                "description": "Importance score 1-5 (higher = more important, shown first in L1). Default: 3.",
                "default": 3.0,
            },
        },
        "required": ["content"],
    },
}

_SCHEMA_MEMPALACE_STATUS = {
    "name": "mempalace_status",
    "description": (
        "Show MemPalace memory status: total drawers, wing breakdown, "
        "and knowledge graph statistics."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


# ---------------------------------------------------------------------------
# Registration (called by plugin loader)
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register MemPalace memory provider."""
    ctx.register_memory_provider(MemPalaceProvider())
