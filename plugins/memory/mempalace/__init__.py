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
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory_provider import MemoryProvider
from plugins.memory.mempalace.config import MemPalaceConfig

logger = logging.getLogger(__name__)

# Module-level lock to prevent duplicate inserts from concurrent threads
_dedup_lock = threading.Lock()


class MemPalaceProvider(MemoryProvider):
    """MemPalace structured memory provider."""

    def __init__(self) -> None:
        self._cfg: Optional[MemPalaceConfig] = None
        self._wing: Optional[str] = None
        self._wing_mode: str = "shared"  # shared | isolated | all | disabled
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
        self._prefetch_running: bool = False
        self._prefetch_lock: threading.Lock = threading.Lock()
        self._kg = None  # KnowledgeGraph, lazy init
        self._chroma_client = None
        self._collection = None
        self._soul_content: Optional[str] = None  # Channel soul for cold start
        self._soul_embedding: Optional[List[float]] = None  # Cached embedding

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
        memory_scope = kwargs.get("memory_scope")
        if memory_scope == "*":
            self._wing = None  # None means no filter → all wings
            self._wing_mode = "all"
        elif memory_scope == "-":
            self._wing = None
            self._wing_mode = "disabled"
        elif not memory_scope or memory_scope == "shared":
            # Default: use _global wing for shared context
            self._wing = "_global"
            self._wing_mode = "shared"
        else:
            # Named scope: only own wing
            self._wing = memory_scope
            self._wing_mode = "isolated"

        # Soul content for cold start semantic gate
        self._soul_content = kwargs.get("soul_content") or None
        self._soul_embedding = None  # Reset — will be computed lazily

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
        if self._wing_mode == 'disabled':
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

        # Only mark baked if we got something useful (or empty is fine — don't retry endlessly)
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

    # -- Auto-mine personal facts ------------------------------------------
    # Lightweight regex-based detector for memorable personal statements.
    # Runs in sync_turn after each completed turn; matched facts are auto-saved
    # to MemPalace without requiring the LLM to call mempalace_add explicitly.

    _PERSONAL_FACT_PATTERNS: List[Tuple[str, re.Pattern]] = None  # lazy init

    @classmethod
    def _get_fact_patterns(cls) -> List[Tuple[str, re.Pattern]]:
        """Compile and cache personal-fact detection patterns (ZH + EN)."""
        if cls._PERSONAL_FACT_PATTERNS is not None:
            return cls._PERSONAL_FACT_PATTERNS

        raw = [
            # --- Location ---
            ("location", r"(?:我住|我住在|我住在|我住喺|我係住|我搬咗去|我搬去|我搬了|我搬到)"),
            ("location", r"(?:I live in|I'm based in|I'm from|I moved to|I reside in)"),
            # --- Name / Identity ---
            ("identity", r"(?:我叫|我的名字是|我的名是|我係)"),
            ("identity", r"(?:My name is|I'm called|Call me|I go by)"),
            # --- Company / Work ---
            ("work", r"(?:我的公司|我做|我在.*工作|我在.*上班|我經營|我的職業)"),
            ("work", r"(?:I work at|I work for|My company|I run|I founded|My role)"),
            # --- Preferences (strong) ---
            ("preference", r"(?:我prefer|我偏好|我鍾意|我喜歡|我討厭|我唔鍾意|我唔喜歡|我prefer)"),
            ("preference", r"(?:I prefer|I like to|I hate|I don't like|always use|never use)"),
            # --- Tech choices ---
            ("preference", r"(?:用\s*\w+\s*(?:寫|做|開發|build)|我用\w+|我的stack|我的tech stack)"),
            # --- Age / Family ---
            ("personal", r"(?:我\d+歲|我的年紀|我老婆|我老公|我的另一半|我的伴侶|我子女|我小朋友)"),
            ("personal", r"(?:I'm \d+|my wife|my husband|my partner|my kids|my children)"),
            # --- Constraints / Rules ---
            ("constraint", r"(?:我唔可以|我不能|我必須|我的要求是|一定要|唔好|不要)"),
            ("constraint", r"(?:I can't|I must|I need|I require|make sure|don't ever)"),
        ]

        cls._PERSONAL_FACT_PATTERNS = [
            (cat, re.compile(pat, re.IGNORECASE)) for cat, pat in raw
        ]
        return cls._PERSONAL_FACT_PATTERNS

    def _detect_personal_facts(self, text: str) -> List[Dict[str, str]]:
        """Detect memorable personal statements in user text.

        Returns list of dicts: {"content": str, "category": str}.
        Only returns the matching sentence, not the whole text.
        """
        if not text or len(text.strip()) < 4:
            return []

        # Split into sentences for more targeted extraction
        sentences = re.split(r'[。！？\n.!?]+', text)
        results = []
        seen_cats = set()

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 4:
                continue

            for category, pattern in self._get_fact_patterns():
                if category in seen_cats:
                    continue  # One fact per category per turn
                m = pattern.search(sentence)
                if m:
                    results.append({
                        "content": sentence,
                        "category": category,
                    })
                    seen_cats.add(category)
                    break  # One category per sentence

        return results

    # -- Semantic gate thresholds --
    # Core categories bypass the gate (always stored to "shared" wing)
    _CORE_CATEGORIES = frozenset({"location", "identity", "personal"})
    # Cold start: how many memories a wing needs before switching from soul→memory reference
    _COLD_START_THRESHOLD = 5
    # Cosine distance threshold for relevance (lower = more similar)
    _RELEVANCE_MAX_DISTANCE = 0.55

    def _get_wing_memory_count(self, wing: str) -> int:
        """Count existing drawers in a wing."""
        try:
            col = self._get_collection()
            if col is None:
                return 0
            result = col.get(where={"wing": wing}, include=[])
            return len(result.get("ids", []))
        except Exception:
            return 0

    def _compute_soul_embedding(self) -> Optional[List[float]]:
        """Compute and cache the embedding for soul content."""
        if self._soul_embedding is not None:
            return self._soul_embedding
        if not self._soul_content:
            return None
        try:
            col = self._get_collection()
            if col is None:
                return None
            # Use ChromaDB's embedding function to embed the soul
            ef = col._embedding_function
            if ef is None:
                return None
            embeddings = ef([self._soul_content])
            if embeddings and len(embeddings) > 0:
                self._soul_embedding = embeddings[0]
                return self._soul_embedding
        except Exception as e:
            logger.debug("Soul embedding failed: %s", e)
        return None

    def _is_fact_relevant(self, fact_content: str, wing: str) -> bool:
        """Check if a detected fact is semantically relevant to the wing.

        Two-phase approach:
        1. Mature wing (≥5 memories): compare against existing memories
        2. Cold start (<5 memories): compare against soul content

        Returns True if relevant (should store), False if irrelevant (skip).
        """
        col = self._get_collection()
        if col is None:
            return True  # No collection → allow everything (graceful degradation)

        wing_count = self._get_wing_memory_count(wing)

        if wing_count >= self._COLD_START_THRESHOLD:
            # Phase 1: Compare against existing wing memories
            try:
                result = col.query(
                    query_texts=[fact_content],
                    n_results=min(3, wing_count),
                    where={"wing": wing},
                    include=["distances"],
                )
                if result and result.get("distances") and result["distances"][0]:
                    min_distance = min(result["distances"][0])
                    if min_distance <= self._RELEVANCE_MAX_DISTANCE:
                        return True
                    logger.debug(
                        "Semantic gate: fact not relevant to wing '%s' (min_dist=%.3f): %s",
                        wing, min_distance, fact_content[:50],
                    )
                    return False
            except Exception as e:
                logger.debug("Semantic gate query failed: %s", e)
                return True  # Graceful degradation
        else:
            # Phase 2: Cold start — compare against soul content
            soul_emb = self._compute_soul_embedding()
            if soul_emb is None:
                return True  # No soul → allow everything (bootstrap)
            try:
                # Embed the fact and compute cosine distance to soul
                ef = col._embedding_function
                if ef is None:
                    return True
                fact_emb = ef([fact_content])
                if not fact_emb or len(fact_emb) == 0:
                    return True
                # Cosine distance = 1 - cosine similarity
                import numpy as np
                a = np.array(soul_emb)
                b = np.array(fact_emb[0])
                norm_a = np.linalg.norm(a)
                norm_b = np.linalg.norm(b)
                if norm_a == 0 or norm_b == 0:
                    return True
                cosine_sim = np.dot(a, b) / (norm_a * norm_b)
                cosine_dist = 1.0 - cosine_sim
                if cosine_dist <= self._RELEVANCE_MAX_DISTANCE:
                    return True
                logger.debug(
                    "Semantic gate (cold start): fact not relevant to soul (dist=%.3f): %s",
                    cosine_dist, fact_content[:50],
                )
                return False
            except Exception as e:
                logger.debug("Semantic gate cold start failed: %s", e)
                return True

        return True  # Default: allow

    def _auto_mine_facts(self, user_content: str) -> None:
        """Background thread: detect and save personal facts with semantic gate."""
        if self._wing_mode == 'disabled':
            return
        facts = self._detect_personal_facts(user_content)
        if not facts:
            return

        wing = self._wing or "_global"

        for fact in facts:
            try:
                fact_wing = wing
                fact_content = fact["content"]
                category = fact["category"]

                # Core facts (location, identity, personal) always go to shared
                if category in self._CORE_CATEGORIES:
                    fact_wing = "shared"
                else:
                    # Semantic gate: is this fact relevant to this wing?
                    if not self._is_fact_relevant(fact_content, wing):
                        continue  # Skip irrelevant fact

                drawer_id, action = self._add_drawer(
                    content=fact_content,
                    wing=fact_wing,
                    room=category,
                    importance=4.0,  # Personal facts are high-importance
                )
                if drawer_id:
                    logger.info(
                        "MemPalace auto-mine: %s '%s' as %s [%s] wing=%s",
                        action, fact_content[:60], category, drawer_id, fact_wing,
                    )
            except Exception as e:
                logger.debug("Auto-mine fact failed: %s", e)

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "") -> None:
        """Record turn and auto-mine personal facts. Non-blocking."""
        if self._cron_skipped or not self._initialized:
            return
        self._turn_number += 1
        # Auto-mine in background thread so it never blocks the response
        if user_content:
            t = threading.Thread(
                target=self._auto_mine_facts, args=(user_content,), daemon=True,
            )
            t.start()

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
            drawer_id, _ = self._add_drawer(
                content=content,
                wing=self._wing or "_global",
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
            schemas.append(_SCHEMA_MEMPALACE_DELETE)
            schemas.append(_SCHEMA_MEMPALACE_STATUS)

        return schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch tool calls."""
        if not self._initialized:
            return json.dumps({"success": False, "error": "MemPalace not initialized. Check Ollama is running."})
        if tool_name == "mempalace_search":
            return self._tool_search(args)
        elif tool_name == "mempalace_add":
            return self._tool_add(args)
        elif tool_name == "mempalace_delete":
            return self._tool_delete(args)
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
        """Clean shutdown — reset state for potential re-initialization."""
        if self._kg:
            try:
                self._kg.close()
            except Exception:
                pass
            self._kg = None

        self._initialized = False
        self._collection = None
        self._chroma_client = None
        self._layers_baked = False
        self._cached_system_block = ""
        self._cached_prefetch = ""
        with self._prefetch_lock:
            self._prefetch_running = False

    # -- Internal -------------------------------------------------------------

    def _init_chroma(self, palace_path: str) -> None:
        """Init ChromaDB client with Ollama embedding function."""
        try:
            import chromadb
            from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

            ef = OllamaEmbeddingFunction(
                url=os.environ.get("OLLAMA_HOST",
                    (self._cfg.ollama_url if self._cfg else None) or
                    "http://localhost:11434"
                ),
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

    # Distance threshold for auto-dedup (cosine distance; lower = more similar)
    # Default 0.35 ≈ cosine similarity of 0.65 — tuned for BGE-M3 zh+en mixed content
    # Configurable via memory.mempalace.dedup_threshold
    _DEDUP_MAX_DISTANCE = 0.35

    def _add_drawer(self, content: str, wing: str, room: str,
                    importance: float = 3.0) -> tuple:
        """Add a drawer to the palace with auto-dedup.

        Before adding, searches for semantically similar content in the same
        wing+room.  If found (cosine distance < _DEDUP_MAX_DISTANCE), the
        existing drawer is updated in place instead of creating a new one.

        Returns (drawer_id, action) where action is "added" or "updated".
        """
        try:
            col = self._get_collection()
            if col is None:
                logger.error("No ChromaDB collection available")
                return ("", "error")

            # --- Auto-dedup: search for similar content in same wing (any room) ---
            # Serialize dedup-query + upsert to prevent duplicate race conditions
            with _dedup_lock:
                replaced_id = None
                try:
                    threshold = self._cfg.dedup_threshold if self._cfg else self._DEDUP_MAX_DISTANCE
                    scope = self._cfg.dedup_scope if self._cfg else "wing"
                    where_filter = {"wing": wing}
                    if scope == "room":
                        where_filter = {"$and": [{"wing": wing}, {"room": room}]}
                    dup_result = col.query(
                        query_texts=[content],
                        n_results=1,
                        where=where_filter,
                        include=["documents", "metadatas", "distances"],
                    )
                    if (dup_result and dup_result.get("ids") and dup_result["ids"][0]
                            and dup_result["distances"][0][0] < threshold):
                        replaced_id = dup_result["ids"][0][0]
                        logger.info(
                            "MemPalace auto-dedup: replacing drawer %s (distance=%.4f)",
                            replaced_id, dup_result["distances"][0][0],
                        )
                except Exception as e:
                    logger.debug("Auto-dedup search failed (will add new): %s", e)

                if replaced_id:
                    # Update existing drawer in place
                    drawer_id = replaced_id
                    action = "updated"
                else:
                    drawer_id = hashlib.sha256(
                        f"{content[:200]}:{time.time()}".encode()
                    ).hexdigest()[:16]
                    action = "added"

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
            return (drawer_id, action)

        except Exception as e:
            logger.error("Failed to add drawer: %s", e)
            return ("", "error")

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

    def _enforce_wing(self, requested_wing: Optional[str]) -> Optional[str]:
        """Enforce wing isolation based on _wing_mode.

        - isolated: always returns self._wing (ignores LLM-supplied value)
        - shared: returns self._wing ("_global") or requested_wing if None
        - all: returns requested_wing (no filter)
        - disabled: returns None (blocks all access — defense in depth)
        """
        mode = self._wing_mode
        if mode == 'disabled':
            return None  # Block all memory access
        if mode == 'isolated':
            return self._wing  # Always enforce own wing, ignore LLM override
        if mode == 'all':
            return requested_wing  # Allow cross-wing queries (admin/wildcard)
        # shared: default to self._wing; only allow _global as an explicit override
        if requested_wing and requested_wing != "_global":
            logger.debug(f"Shared mode: ignoring requested_wing={requested_wing}, using {self._wing}")
        return requested_wing if requested_wing == "_global" else self._wing

    def _tool_search(self, args: Dict[str, Any]) -> str:
        """Handle mempalace_search tool call."""
        if self._wing_mode == 'disabled':
            return json.dumps({"results": [], "total": 0})
        query = args.get("query", "")
        wing = self._enforce_wing(args.get("wing"))
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
        """Handle mempalace_add tool call.

        Auto-dedup: if semantically similar content exists in the same
        wing+room (cosine distance < 0.15), the existing drawer is updated
        in place and the response indicates "updated" instead of "added".
        """
        if self._wing_mode == 'disabled':
            return json.dumps({"error": "Memory disabled for this scope", "success": False})
        content = args.get("content", "")
        wing = self._enforce_wing(args.get("wing")) or "_global"
        room = args.get("room", "general")
        importance = args.get("importance", 3.0)

        if not content.strip():
            return json.dumps({"error": "Content cannot be empty"})

        drawer_id, action = self._add_drawer(content, wing, room, importance)
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
                "action": action,
            })
        return json.dumps({"error": "Failed to add drawer"})

    def _tool_delete(self, args: Dict[str, Any]) -> str:
        """Handle mempalace_delete tool call.

        Delete a specific drawer by ID.  The LLM should search first to
        find the drawer ID, then call delete.
        """
        if self._wing_mode == 'disabled':
            return json.dumps({"error": "Memory disabled for this scope", "success": False})
        drawer_id = args.get("drawer_id", "").strip()
        if not drawer_id:
            return json.dumps({"error": "drawer_id is required. Use mempalace_search first to find the ID."})

        try:
            col = self._get_collection()
            if col is None:
                return json.dumps({"error": "ChromaDB not initialized"})

            # Verify drawer exists
            existing = col.get(ids=[drawer_id], include=["metadatas"])
            if not existing or not existing.get("ids") or drawer_id not in existing["ids"]:
                return json.dumps({"error": f"Drawer {drawer_id} not found"})

            # Get metadata for wing ownership check + confirmation
            meta = {}
            if existing.get("metadatas") and existing["metadatas"]:
                meta = existing["metadatas"][0] or {}

            # Wing ownership check: only delete from wings this session can access
            drawer_wing = meta.get("wing", "")
            if self._wing_mode == "shared":
                # Shared mode: only allow delete from own wing or _global
                if drawer_wing != self._wing and drawer_wing != "_global":
                    return json.dumps({
                        "error": f"Cannot delete from wing '{drawer_wing}' — only '{self._wing}' and '_global' allowed in shared mode",
                        "success": False,
                    })
            elif self._wing_mode == "isolated":
                # Isolated mode: strict wing enforcement
                enforced_wing = self._enforce_wing(None)
                if enforced_wing != drawer_wing:
                    return json.dumps({
                        "error": f"Cannot delete from wing '{drawer_wing}' — bound to '{enforced_wing}'",
                        "success": False,
                    })
            # "all" and "disabled" modes: no wing restriction on delete
            # (disabled is already guarded earlier — returns error before reaching here)

            # Delete
            col.delete(ids=[drawer_id])

            return json.dumps({
                "success": True,
                "deleted_drawer": drawer_id,
                "wing": meta.get("wing", ""),
                "room": meta.get("room", ""),
            })
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _tool_status(self, args: Dict[str, Any]) -> str:
        """Handle mempalace_status tool call."""
        if self._wing_mode == 'disabled':
            return json.dumps({
                "total_drawers": 0,
                "wings": {},
                "current_wing": None,
                "wing_mode": "disabled",
            })
        try:
            col = self._get_collection()
            if col is None:
                return json.dumps({
                    "total_drawers": 0,
                    "wings": {},
                    "current_wing": self._wing,
                    "wing_mode": self._wing_mode,
                    "error": "ChromaDB not initialized",
                })

            mode = self._wing_mode

            # Get total count
            all_data = col.get(include=["metadatas"])
            total = len(all_data["ids"])

            # Breakdown by wing — filter for isolated mode
            wings: Dict[str, int] = {}
            for meta in all_data.get("metadatas", []):
                if meta:
                    w = meta.get("wing", "none")
                    if mode == 'isolated' and w != self._wing:
                        continue  # Don't leak other wings to isolated souls
                    wings[w] = wings.get(w, 0) + 1

            # Isolated mode: only report own wing's total
            if mode == 'isolated':
                total = wings.get(self._wing, 0)

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
        "and becomes searchable. "
        "AUTO-DEDUP: if semantically similar content already exists in the same wing+room, "
        "the existing drawer is updated in place — no duplicates will be created."
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

_SCHEMA_MEMPALACE_DELETE = {
    "name": "mempalace_delete",
    "description": (
        "Delete a specific memory drawer by ID. Use mempalace_search first to find the "
        "drawer ID, then call mempalace_delete to remove it. Use this when the user "
        "explicitly asks to forget or remove a specific piece of information."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "drawer_id": {
                "type": "string",
                "description": "The ID of the drawer to delete. Get this from mempalace_search results.",
            },
        },
        "required": ["drawer_id"],
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
