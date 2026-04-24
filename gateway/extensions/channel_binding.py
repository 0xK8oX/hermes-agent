"""
Channel Personality Binding Extension
======================================

Unified binding: soul + skills + model + memory_scope per channel.

Self-contained: owns its own state, loads soul files, manages per-session
overrides, restores after /reset, and provides scoped memory isolation.

Config format (in config.yaml, under {platform}.extra):

    channel_personality_bindings:
      - id: "123456"                            # channel/thread ID
        soul: "dev"                              # ~/.hermes/souls/dev.md
        skills: ["systematic-debugging", "github-pr-workflow"]  # auto-loaded
        model: "anthropic/claude-sonnet-4"       # optional model override
        provider: "openai"                       # optional provider
        base_url: "https://..."                  # optional base URL
        api_key: "${API_KEY}"                    # optional, env-var expanded
        memory_scope: "dev"                      # scoped memory dir
        # Special values: "*" = all scopes, "-" = skip _global/ (isolation)

Supported platforms: Discord, Telegram, WhatsApp (and any future platform
that routes through the gateway command dispatcher).

Commands:
    /bind              — show current binding
    /bind <soul_name>  — switch soul for this session (session-only)
    /bind save         — persist current binding to config.yaml
    /bind list         — list all persisted bindings across all platforms
    /bind unbind       — remove persisted binding for current channel + clear runtime
    /bind --clear      — clear session binding only (no config change)

Memory architecture:
    ~/.hermes/memories/
    ├── _global/     ← shared across all channels
    ├── dev/         ← only #dev channel reads/writes
    ├── pm/          ← only #pm channel reads/writes
    └── ...

    memory_scope: "*"  → God mode, reads ALL scopes
    memory_scope: "-"  → skip _global/ (isolation mode — no shared context)
    memory_scope omitted → default (unscoped, backward compatible)

Resolution:
    1. Side-channel ``set_event_binding()`` — called by adapter before event dispatch
    2. Session-key → config match — works for any platform, zero adapter changes

This module registers itself via ``register_hook()`` on import.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.extensions import register_hook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-session state (module-level, keyed by session_key)
# Protected by _state_lock for thread safety across async gateway + hooks.
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()

# soul content per session: {session_key: str}
_session_souls: Dict[str, str] = {}

# model overrides per session: {session_key: {model, provider, api_key, base_url}}
_session_model_overrides: Dict[str, Dict[str, Optional[str]]] = {}

# soul name per session (for /reset re-load): {session_key: str}
_session_soul_names: Dict[str, str] = {}

# binding config per session (for /reset restore): {session_key: dict}
_session_bindings: Dict[str, dict] = {}

# skills override per session: {session_key: [skill_names]}
_session_skills: Dict[str, List[str]] = {}

# memory scope per session: {session_key: str}
_session_memory_scopes: Dict[str, str] = {}

# allow_config_write per session: {session_key: bool}
_session_allow_config_write: Dict[str, bool] = {}

# Config cache for cross-platform binding resolution (invalidated on /reset)
_CONFIG_BINDINGS_CACHE: Optional[Dict[str, list]] = None

# O(1) index for bound-channel lookup: {(platform, chat_id): True}
_bound_channel_index: Dict[tuple, bool] = {}

# ---------------------------------------------------------------------------
# Side-channel for passing binding info from adapters to extensions.
# Keyed by (platform, chat_id), value is binding dict.
# This avoids modifying the MessageEvent dataclass (upstream code).
# ---------------------------------------------------------------------------
_event_binding_sidechannel: Dict[tuple, dict] = {}

# Signal flag for /bind reset (set by extension, consumed by gateway wrapper).
_bind_reset_flag: Dict[str, bool] = {}


def set_event_binding(platform: str, chat_id: str, binding: dict) -> None:
    """Store binding info for a specific (platform, chat_id) pair."""
    _event_binding_sidechannel[(platform, str(chat_id))] = binding


def get_event_binding(platform: str, chat_id: str) -> Optional[dict]:
    """Retrieve and consume binding info for a (platform, chat_id) pair."""
    return _event_binding_sidechannel.pop((platform, str(chat_id)), None)


def set_bind_reset(channel_id: str) -> None:
    """Signal that /bind needs a full session reset for the given channel."""
    _bind_reset_flag[str(channel_id)] = True


def consume_bind_reset(channel_id: str) -> bool:
    """Check and consume the bind reset flag for the given channel."""
    return _bind_reset_flag.pop(str(channel_id), False)


# ---------------------------------------------------------------------------
# Soul loading
# ---------------------------------------------------------------------------

def _load_soul_content(soul_name: str) -> Optional[str]:
    """Load soul content from ~/.hermes/souls/<soul_name>.md.

    Returns only the body text (frontmatter stripped).
    Use ``_load_soul_with_meta()`` if you also need the metadata.
    """
    result = _load_soul_with_meta(soul_name)
    return result[0] if result else None


def _load_soul_with_meta(soul_name: str) -> Optional[tuple]:
    """Load soul file and return (content, metadata_dict).

    Soul files may include YAML frontmatter between ``---`` delimiters::

        ---
        memory_scope: accountant
        skills:
          - productivity/professional-pdf-generation
          - email/himalaya
        ---

        (soul body follows)

    If no frontmatter is present, returns ``(content, {})``.
    Returns ``None`` if the file does not exist or is empty.
    """
    if "\x00" in soul_name or "/" in soul_name or "\\" in soul_name or ".." in soul_name:
        logger.warning("[ChannelBinding] Invalid soul name (path traversal): %s", soul_name)
        return None

    try:
        from hermes_constants import get_hermes_home
        soul_path = get_hermes_home() / "souls" / f"{soul_name}.md"
    except ImportError:
        soul_path = Path.home() / ".hermes" / "souls" / f"{soul_name}.md"

    if not soul_path.exists():
        logger.warning("[ChannelBinding] Soul file not found: %s", soul_path)
        return None

    try:
        raw = soul_path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
    except Exception as e:
        logger.warning("[ChannelBinding] Failed to load soul '%s': %s", soul_name, e)
        return None

    # Parse optional YAML frontmatter
    metadata: Dict[str, Any] = {}
    content = raw

    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            yaml_block = parts[1].strip()
            content = parts[2].strip()
            if yaml_block:
                if len(yaml_block) > 65536:
                    logger.warning(
                        "[ChannelBinding] Soul frontmatter exceeds 64KB limit (%d bytes), skipping",
                        len(yaml_block),
                    )
                else:
                    try:
                        import yaml
                        parsed = yaml.safe_load(yaml_block)
                        if isinstance(parsed, dict):
                            metadata = parsed
                    except Exception:
                        logger.warning(
                            "[ChannelBinding] Invalid YAML frontmatter in soul '%s', treating as no frontmatter",
                            soul_name,
                        )

    return (content if content else None, metadata)


def _expand_api_key(raw: Optional[str]) -> Optional[str]:
    """Expand ${ENV_VAR} references in API key strings."""
    if not raw or not isinstance(raw, str):
        return None
    return os.path.expandvars(raw)


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def _on_new_session(session_key: str, event: Any) -> None:
    """Apply channel binding overrides for a new session.

    Resolution order:
    1. Side-channel lookup by (platform, chat_id) — set by platform adapter
    2. Session-key → config match — works for any platform without adapter changes

    If a dynamic binding already exists (e.g. set via ``/bind`` and preserved
    through ``_on_session_reset``), we skip re-applying from config so that the
    user's ephemeral choice is not overwritten.
    """

    # Guard: if a dynamic binding was already applied (e.g. /bind → reset →
    # new-session), don't overwrite it with the config-level default.
    with _state_lock:
        existing = _session_bindings.get(session_key)
    if existing:
        logger.debug(
            "[ChannelBinding] _on_new_session: keeping existing dynamic binding for %s",
            session_key[:30],
        )
        return

    binding = None

    # Path 1: adapter-provided binding via side-channel (set by Discord adapter)
    parsed_sk = _parse_session_key(session_key)
    if parsed_sk and parsed_sk.get("platform") and parsed_sk.get("channel_id"):
        binding = get_event_binding(parsed_sk["platform"], parsed_sk["channel_id"])

    # Path 2: resolve from session_key + config (works for any platform)
    if not binding:
        binding = _resolve_binding_from_session_key(session_key)

    if not binding or not isinstance(binding, dict):
        return

    _apply_binding(session_key, binding)


def _apply_binding(session_key: str, binding: dict, preloaded_soul: Optional[tuple] = None) -> None:
    """Store soul + skills + model + memory_scope from a resolved binding dict.

    Args:
        session_key: The session key to apply the binding to.
        binding: The binding configuration dict.
        preloaded_soul: Optional (content, metadata) tuple pre-loaded from
            disk to avoid file I/O while holding ``_state_lock`` (H4).
    """
    preloaded_meta = None

    # Pre-load soul content from disk OUTSIDE the lock to avoid I/O under
    # lock (H4).  Pass it into the locked section via _content key.
    soul_name = binding.get("soul")
    if soul_name and isinstance(soul_name, str) and "_content" not in binding:
        if preloaded_soul is not None:
            binding = dict(binding)  # shallow copy to avoid mutating caller
            binding["_content"] = preloaded_soul[0] if preloaded_soul[0] else None
            preloaded_meta = preloaded_soul[1] if len(preloaded_soul) > 1 else None
        else:
            loaded = _load_soul_with_meta(soul_name)
            if loaded and loaded[0]:
                binding = dict(binding)
                binding["_content"] = loaded[0]
                preloaded_meta = loaded[1]

    with _state_lock:
        _apply_binding_unlocked(session_key, binding, preloaded_meta=preloaded_meta)


def _apply_binding_unlocked(session_key: str, binding: dict, preloaded_meta: Optional[dict] = None) -> None:
    """Internal: mutate session state (caller must hold _state_lock).

    Args:
        session_key: The session key to apply the binding to.
        binding: The binding configuration dict.
        preloaded_meta: Optional metadata dict pre-loaded from soul frontmatter
            to avoid file I/O while holding ``_state_lock`` (H4).
    """
    soul_name = binding.get("soul")
    model = binding.get("model")

    skills = binding.get("skills")
    memory_scope = binding.get("memory_scope")

    # Store the raw binding for /reset restore (strip ephemeral _content to avoid
    # polluting the dict with potentially large soul text).
    _session_bindings[session_key] = {k: v for k, v in binding.items() if k != "_content"}

    # Maintain O(1) bound-channel index for is_channel_bound()
    parsed = _parse_session_key(session_key)
    if parsed and parsed.get("platform") and parsed.get("channel_id"):
        _bound_channel_index[(parsed["platform"], parsed["channel_id"])] = True

    # Load soul content (use pre-loaded content if caller already read the file)
    if soul_name and isinstance(soul_name, str):
        content = binding.get("_content") or _load_soul_content(soul_name)
        if content:
            _session_souls[session_key] = content
            _session_soul_names[session_key] = soul_name
            logger.info(
                "[ChannelBinding] Soul '%s' → session %s",
                soul_name, session_key[:30],
            )

    # Store skills override
    if skills:
        if isinstance(skills, str):
            skills = [skills]
        if isinstance(skills, list) and skills:
            _session_skills[session_key] = list(dict.fromkeys(skills))
            logger.info(
                "[ChannelBinding] Skills %s → session %s",
                _session_skills[session_key], session_key[:30],
            )

    # Store model override (keep raw api_key for safe /bind save)
    model = binding.get("model")
    raw_api_key = binding.get("api_key")
    resolved_base_url = binding.get("base_url")
    if resolved_base_url:
        resolved_base_url = os.path.expandvars(resolved_base_url)
    resolved_provider = binding.get("provider")
    resolved_api_mode = None

    # Always expand api_key regardless of whether we have a model override
    # This fixes 401 when soul has api_key env var but no model override
    expanded_api_key = _expand_api_key(raw_api_key)

    # Auto-resolve base_url from custom_providers config when provider
    # uses the ``custom:<name>`` format but no explicit base_url is set.
    if not resolved_base_url and resolved_provider and str(resolved_provider).startswith("custom:"):
        try:
            from hermes_cli.runtime_provider import _get_named_custom_provider
            cp = _get_named_custom_provider(str(resolved_provider))
            if cp:
                resolved_base_url = resolved_base_url or cp.get("base_url")
                if resolved_base_url:
                    resolved_base_url = os.path.expandvars(resolved_base_url)
                resolved_api_mode = resolved_api_mode or cp.get("api_mode")
                if not expanded_api_key:
                    raw_api_key = raw_api_key or cp.get("api_key")
                    expanded_api_key = _expand_api_key(raw_api_key)
        except Exception as _cp_err:
            logger.debug("[ChannelBinding] custom provider resolve failed: %s", _cp_err)

    if model or expanded_api_key or resolved_provider or resolved_base_url:
        _session_model_overrides[session_key] = {
            "model": model,
            "provider": resolved_provider,
            "api_key": expanded_api_key,
            "api_key_raw": raw_api_key,
            "base_url": resolved_base_url,
            "api_mode": resolved_api_mode,
        }
        logger.info(
            "[ChannelBinding] Model '%s' (provider=%s, base_url=%s) → session %s",
            model, resolved_provider, resolved_base_url, session_key[:30],
        )
    else:
        # New soul has no model — clear any stale override from the previous soul
        cleared = _session_model_overrides.pop(session_key, None)
        if cleared:
            logger.info(
                "[ChannelBinding] Cleared stale model override '%s' for session %s",
                cleared.get("model"), session_key[:30],
            )

    # Store memory scope
    if memory_scope and isinstance(memory_scope, str):
        _session_memory_scopes[session_key] = memory_scope
        logger.info(
            "[ChannelBinding] Memory scope '%s' → session %s",
            memory_scope, session_key[:30],
        )

    # Parse allow_config_write from soul frontmatter (use preloaded metadata
    # to avoid I/O under lock — H4).  If not preloaded, skip the check —
    # callers should ensure metadata is passed via _apply_binding's preloaded_soul.
    allow_config_write = False
    if soul_name and isinstance(soul_name, str) and isinstance(preloaded_meta, dict):
        allow_config_write = bool(preloaded_meta.get("allow_config_write", False))
    _session_allow_config_write[session_key] = allow_config_write
    if allow_config_write:
        logger.info(
            "[ChannelBinding] allow_config_write=True → session %s",
            session_key[:30],
        )


def _enrich_from_soul_frontmatter(entry: dict) -> dict:
    """Enrich a config binding entry with fields from its soul's frontmatter.

    Soul frontmatter is the single source of truth for model, provider,
    base_url, api_key, skills, and memory_scope.  Config bindings only need
    ``id`` + ``soul`` — everything else is resolved here.

    Fields already present in *entry* (e.g. temporarily set by /bind save)
    are **not** overwritten, so an explicit runtime override still wins.
    """
    soul_name = entry.get("soul")
    if not soul_name or not isinstance(soul_name, str):
        return entry

    result = _load_soul_with_meta(soul_name)
    if not result:
        return entry

    _, meta = result
    if not isinstance(meta, dict) or not meta:
        return entry

    # Enrich: soul frontmatter fills in anything the config entry lacks
    for key in ("model", "provider", "base_url", "api_key", "memory_scope"):
        if key not in entry and meta.get(key):
            entry[key] = meta[key]

    if "skills" not in entry and meta.get("skills"):
        entry["skills"] = meta["skills"]

    return entry


def _resolve_binding_from_session_key(session_key: str) -> Optional[dict]:
    """Resolve a channel binding from session_key + config.

    Parses ``agent:main:{platform}:{chat_type}:{chat_id}[:{thread_id}[:...]]``
    and matches against all configured platform bindings.

    Matching strategy:
      1. Exact match on chat_id (position 4)
      2. For threads, also try the thread_id (position 5) against config IDs
      3. For threads, also try to match the parent channel's bindings
         by stripping the thread_id and checking the parent chat_id

    After matching, the entry is enriched from the soul's frontmatter so
    that model/provider/skills/memory_scope come from a single source of
    truth (the soul .md file) rather than duplicated in config.yaml.
    """
    if not session_key:
        return None

    parts = session_key.split(":")
    if len(parts) < 5 or parts[0] != "agent":
        return None

    platform = parts[2]
    chat_type = parts[3] if len(parts) > 3 else ""
    chat_id = parts[4] if len(parts) > 4 else None
    thread_id = parts[5] if len(parts) > 5 else None
    if not chat_id:
        return None

    all_bindings = _get_all_platform_bindings()
    platform_bindings = all_bindings.get(platform, [])

    matched = None

    # Strategy 1: exact match on chat_id
    for entry in platform_bindings:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id", ""))
        if entry_id == chat_id:
            matched = entry
            break

    # Strategy 2: for threads, also try thread_id and consider that
    # the session key may use the thread's own ID as chat_id.
    if not matched and chat_type == "thread" and thread_id:
        for entry in platform_bindings:
            if not isinstance(entry, dict):
                continue
            entry_threads = entry.get("threads", [])
            if isinstance(entry_threads, list) and thread_id in [str(t) for t in entry_threads]:
                matched = entry
                break

    if not matched:
        return None

    # Enrich from soul frontmatter (single source of truth)
    return _enrich_from_soul_frontmatter(matched)


def _get_all_platform_bindings() -> Dict[str, list]:
    """Load channel_personality_bindings for all platforms from config.yaml.

    Returns {platform_name: [binding_dicts]}.
    Result is cached on first call.  All reads and writes of
    ``_CONFIG_BINDINGS_CACHE`` are protected by ``_state_lock``.
    """
    global _CONFIG_BINDINGS_CACHE

    with _state_lock:
        if _CONFIG_BINDINGS_CACHE is not None:
            return _CONFIG_BINDINGS_CACHE

    import yaml

    try:
        from hermes_constants import get_hermes_home
        config_path = get_hermes_home() / "config.yaml"
    except ImportError:
        config_path = Path.home() / ".hermes" / "config.yaml"

    result: Dict[str, list] = {}

    if not config_path.exists():
        with _state_lock:
            _CONFIG_BINDINGS_CACHE = result
        return result

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        with _state_lock:
            _CONFIG_BINDINGS_CACHE = result
        return result

    if not isinstance(raw, dict):
        with _state_lock:
            _CONFIG_BINDINGS_CACHE = result
        return result

    # Scan top-level platform keys (discord, telegram, whatsapp, etc.)
    for platform_name, platform_cfg in raw.items():
        if not isinstance(platform_cfg, dict):
            continue
        extra = platform_cfg.get("extra", {})
        if not isinstance(extra, dict):
            continue
        bindings = extra.get("channel_personality_bindings", [])
        if isinstance(bindings, list) and bindings:
            result[platform_name] = bindings

    # Also scan platforms.* (alternative layout)
    platforms = raw.get("platforms", {})
    if isinstance(platforms, dict):
        for platform_name, platform_cfg in platforms.items():
            if platform_name in result:
                continue
            if not isinstance(platform_cfg, dict):
                continue
            extra = platform_cfg.get("extra", {})
            if not isinstance(extra, dict):
                continue
            bindings = extra.get("channel_personality_bindings", [])
            if isinstance(bindings, list) and bindings:
                result[platform_name] = bindings

    with _state_lock:
        _CONFIG_BINDINGS_CACHE = result
    logger.debug("[ChannelBinding] Loaded bindings for platforms: %s", list(result.keys()))
    return result


def _on_session_reset(session_key: str) -> None:
    """Re-apply channel binding after /reset.

    Re-loads the soul content from disk (in case the file changed),
    restores overrides, and invalidates config cache for hot-reload.
    """
    global _CONFIG_BINDINGS_CACHE
    with _state_lock:
        _CONFIG_BINDINGS_CACHE = None

    # L1: Invalidate soul name cache so new/removed souls are discovered
    invalidate_soul_cache()

    with _state_lock:
        binding = _session_bindings.get(session_key)
        if not binding:

            return

        # Re-load soul from disk
        soul_name = binding.get("soul")
        if soul_name and isinstance(soul_name, str):
            content = _load_soul_content(soul_name)
            if content:
                _session_souls[session_key] = content
                logger.info(
                    "[ChannelBinding] /reset: restored soul '%s' for %s",
                    soul_name, session_key[:30],
                )

        model_override = _session_model_overrides.get(session_key)
        if model_override:
            logger.info(
                "[ChannelBinding] /reset: keeping model '%s' for %s",
                model_override.get("model"), session_key[:30],
            )


def _on_session_cleanup(session_key: str) -> None:
    """Remove all per-session state for an expired session."""
    with _state_lock:
        parsed = _parse_session_key(session_key)
        if parsed and parsed.get("platform") and parsed.get("channel_id"):
            _bound_channel_index.pop((parsed["platform"], parsed["channel_id"]), None)
        _session_souls.pop(session_key, None)
        _session_model_overrides.pop(session_key, None)
        _session_soul_names.pop(session_key, None)
        _session_bindings.pop(session_key, None)
        _session_skills.pop(session_key, None)
        _session_memory_scopes.pop(session_key, None)
        _session_allow_config_write.pop(session_key, None)


def _ensure_binding_loaded(session_key: str) -> None:
    """Lazy-init: if session_key has no cached binding, resolve from config.

    After a gateway restart, the in-memory dicts (_session_souls, etc.) are
    empty.  For *existing* sessions ``on_new_session`` never fires, so this
    fallback ensures the binding is loaded on first access.
    """
    with _state_lock:
        if session_key in _session_bindings:
            return  # already populated

    binding = _resolve_binding_from_session_key(session_key)
    if binding and isinstance(binding, dict):
        _apply_binding(session_key, binding)


def _get_model_override(session_key: str) -> Optional[Dict[str, Optional[str]]]:
    """Return the channel binding model override for a session, or None."""
    _ensure_binding_loaded(session_key)
    with _state_lock:
        result = _session_model_overrides.get(session_key)
        if not result:
            logger.debug("[ChannelBinding] get_model_override MISS: key=%s", session_key[:80])
        return result


def _get_ephemeral(session_key: str) -> Optional[str]:
    """Return the per-session soul override content, or None.

    Includes a lazy-resolution fallback: if the in-memory dict has no entry
    (e.g. after a gateway restart where ``on_new_session`` never fires for
    an existing session), we resolve the binding from config on the spot,
    populate the dict, and return the content.
    """
    _ensure_binding_loaded(session_key)
    with _state_lock:
        return _session_souls.get(session_key)


def _get_skills_override(session_key: str) -> Optional[List[str]]:
    """Return the per-session skills override, or None.

    Used by gateway/run.py to inject skills into new sessions.
    An empty list means "all skills" (God mode).
    """
    _ensure_binding_loaded(session_key)
    with _state_lock:
        return _session_skills.get(session_key)


def _get_memory_scope(session_key: str) -> Optional[str]:
    """Return the per-session memory scope, or None.

    Values:
      - None: default (unscoped, backward compatible)
      - "dev", "pm", etc.: read _global/ + {scope}/
      - "*": God mode, read _global/ + ALL subdirectories

    Falls back to re-resolving from config if not cached (e.g. after
    process restart, where in-memory state is lost for existing sessions).
    """
    _ensure_binding_loaded(session_key)
    with _state_lock:
        return _session_memory_scopes.get(session_key)


# ---------------------------------------------------------------------------
# Scoped memory resolution (used by MemoryStore via run_agent.py)
# ---------------------------------------------------------------------------

_known_soul_names_cache = None


def invalidate_soul_cache() -> None:
    """Clear the cached set of known soul names.

    Called from reload/reset hooks so that newly added or removed soul files
    are picked up on next access.
    """
    global _known_soul_names_cache
    _known_soul_names_cache = None


def _get_known_soul_names() -> Optional[set]:
    """Return set of known soul names from ~/.hermes/souls/ directory."""
    global _known_soul_names_cache
    if _known_soul_names_cache is not None:
        return _known_soul_names_cache
    try:
        from hermes_constants import get_hermes_home
        souls_dir = get_hermes_home() / "souls"
        if souls_dir.exists():
            _known_soul_names_cache = {
                p.stem for p in souls_dir.glob("*.md")
            }
            return _known_soul_names_cache
    except Exception:
        pass
    return None


def resolve_memory_dirs(scope: Optional[str]) -> List[Path]:
    """Resolve memory directories for a given scope.

    Returns a list of directories to read, in priority order:

    ========  =============================================
    scope     Directories returned
    ========  =============================================
    ``*``     ``_global/`` + ALL subdirectories
    empty     ``_global/`` + base dir
    ``None``  ``_global/`` + base dir
    shared    ``_global/`` + base dir
    named     ONLY the named subdirectory, NO ``_global/``
    ``-``     ``_isolated/`` (empty dir, no memory)
    ========  =============================================

    Named scopes (e.g. ``lenx``, ``alpha``) intentionally exclude
    ``_global/`` so their memory context stays isolated.

    This function is the public API used by run_agent.py to pass
    memory directories to MemoryStore.
    """
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home() / "memories"
    except ImportError:
        base = Path.home() / ".hermes" / "memories"
    dirs: List[Path] = []

    # --- Isolation mode ---
    if scope == "-":
        isolation_dir = base / "_isolated"
        isolation_dir.mkdir(parents=True, exist_ok=True)
        return [isolation_dir]

    # --- Wildcard: _global + all subdirs ---
    if scope == "*":
        global_dir = base / "_global"
        if global_dir.exists():
            dirs.append(global_dir)
        if base.exists():
            for child in sorted(base.iterdir()):
                if child.is_dir() and child.name != "_global" and not child.name.startswith("."):
                    dirs.append(child)
        return dirs if dirs else [base]

    # --- Shared / empty / None: _global + base dir ---
    if not scope or not scope.strip() or scope == "shared":
        if not scope:
            logger.warning(
                "[MemoryScope] Soul has no memory_scope set — defaulting to 'shared'. "
                "Add `memory_scope: shared` (or `memory_scope: <name>` for isolation) "
                "to the soul frontmatter to suppress this warning."
            )
        global_dir = base / "_global"
        if global_dir.exists():
            dirs.append(global_dir)
        return dirs if dirs else [base]

    # --- Named scope: ONLY the named dir, NO _global/ ---
    # Validate against path traversal and null bytes
    if "\x00" in scope or "/" in scope or "\\" in scope or ".." in scope:
        logger.error("Invalid memory scope (path traversal): %s", scope)
        return [base]

    # Validate scope name matches a known soul
    known_souls = _get_known_soul_names()
    if known_souls is not None and scope not in known_souls:
        logger.warning(
            "[MemoryScope] Scope '%s' does not match any known soul. "
            "Known: %s. Creating anyway for forward compatibility.",
            scope, known_souls,
        )

    scoped_dir = base / scope
    if scoped_dir.exists():
        dirs.append(scoped_dir)
    else:
        # Auto-create on first access (only if scope looks valid)
        if scope.isidentifier() or scope.replace("-", "_").isidentifier():
            scoped_dir.mkdir(parents=True, exist_ok=True)
            dirs.append(scoped_dir)
        else:
            logger.error(
                "[MemoryScope] Refusing to create directory for invalid scope name '%s'",
                scope,
            )
            return [base]

    return dirs


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def _validate_bindings() -> None:
    """Check that all configured bindings are valid at startup.

    Validates:
    - Soul files exist
    - Model names are non-empty strings
    - API key env var references are set
    - Skills referenced exist in ~/.hermes/skills/
    - Memory scope directories are valid paths
    """
    import yaml

    try:
        from hermes_constants import get_hermes_home
        config_path = get_hermes_home() / "config.yaml"
        souls_dir = get_hermes_home() / "souls"
        skills_dir = get_hermes_home() / "skills"
    except ImportError:
        config_path = Path.home() / ".hermes" / "config.yaml"
        souls_dir = Path.home() / ".hermes" / "souls"
        skills_dir = Path.home() / ".hermes" / "skills"

    if not config_path.exists():
        return

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[ChannelBinding] Startup: failed to read config.yaml: %s", e)
        return

    if not isinstance(raw, dict):
        return

    # Collect all bindings across all platforms
    all_bindings: list = []
    for _platform_name, platform_cfg in raw.items():
        if not isinstance(platform_cfg, dict):
            continue
        extra = platform_cfg.get("extra", {})
        if not isinstance(extra, dict):
            continue
        bindings = extra.get("channel_personality_bindings", [])
        if isinstance(bindings, list):
            all_bindings.extend(bindings)

    # Also check platforms.* layout
    platforms = raw.get("platforms", {})
    if isinstance(platforms, dict):
        for _platform_name, platform_cfg in platforms.items():
            if not isinstance(platform_cfg, dict):
                continue
            extra = platform_cfg.get("extra", {})
            if not isinstance(extra, dict):
                continue
            bindings = extra.get("channel_personality_bindings", [])
            if isinstance(bindings, list):
                all_bindings.extend(bindings)

    if not all_bindings:
        return

    errors = 0
    validated_skills: set = set()

    for i, entry in enumerate(all_bindings):
        if not isinstance(entry, dict):
            continue
        chan_id = entry.get("id", f"#{i}")
        soul_name = entry.get("soul")
        model = entry.get("model")
        skills = entry.get("skills")
        memory_scope = entry.get("memory_scope")

        # Validate soul file
        if soul_name:
            soul_path = souls_dir / f"{soul_name}.md"
            if not soul_path.exists():
                logger.error(
                    "[ChannelBinding] Startup FAIL: channel %s — soul '%s' not found at %s",
                    chan_id, soul_name, soul_path,
                )
                errors += 1
            else:
                logger.info("[ChannelBinding] Startup OK: channel %s — soul '%s'", chan_id, soul_name)
        else:
            logger.warning("[ChannelBinding] Startup WARN: channel %s — no soul configured", chan_id)

        # Validate model name
        if model is not None and (not isinstance(model, str) or not model.strip()):
            logger.error(
                "[ChannelBinding] Startup FAIL: channel %s — invalid model '%s'",
                chan_id, model,
            )
            errors += 1

        # Validate skills exist
        if skills:
            skill_list = [skills] if isinstance(skills, str) else skills
            for sname in skill_list:
                if sname not in validated_skills:
                    validated_skills.add(sname)
                    skill_path = skills_dir / sname / "SKILL.md"
                    if not skill_path.exists():
                        # Also try flat file
                        flat_path = skills_dir / f"{sname}.md"
                        if not flat_path.exists():
                            logger.warning(
                                "[ChannelBinding] Startup WARN: channel %s — skill '%s' not found",
                                chan_id, sname,
                            )

        # Validate memory scope name (basic path safety)
        if memory_scope:
            if not isinstance(memory_scope, str):
                logger.error(
                    "[ChannelBinding] Startup FAIL: channel %s — memory_scope must be a string",
                    chan_id,
                )
                errors += 1
            elif "/" in memory_scope or "\\" in memory_scope or ".." in memory_scope:
                logger.error(
                    "[ChannelBinding] Startup FAIL: channel %s — invalid memory_scope '%s'",
                    chan_id, memory_scope,
                )
                errors += 1
            else:
                logger.info(
                    "[ChannelBinding] Startup OK: channel %s — memory_scope '%s'",
                    chan_id, memory_scope,
                )

        # Validate api_key env var reference
        api_key_raw = entry.get("api_key")
        if api_key_raw and isinstance(api_key_raw, str) and api_key_raw.startswith("${"):
            import re
            m = re.match(r"^\$\{(\w+)\}$", api_key_raw)
            if m:
                var_name = m.group(1)
                if not os.environ.get(var_name):
                    logger.warning(
                        "[ChannelBinding] Startup WARN: channel %s — env var '%s' not set",
                        chan_id, var_name,
                    )

    if errors:
        logger.error("[ChannelBinding] Startup: %d error(s) found", errors)
    else:
        logger.info("[ChannelBinding] Startup: all %d binding(s) validated OK", len(all_bindings))


def _get_cron_binding(origin: dict) -> Optional[dict]:
    """Resolve channel binding overrides for a cron job from its origin.

    Called by ``cron/scheduler.py:run_job()`` to inherit the soul, model,
    and provider of the bound channel where the cron job was created.

    Args:
        origin: dict with keys ``platform``, ``chat_id`` (and optionally
                ``thread_id``, ``chat_name``).

    Returns:
        dict with any of: ``model``, ``provider``, ``api_key``, ``base_url``,
        ``soul_content``, ``skills``, ``memory_scope`` — or ``None`` if no
        binding exists for this channel.
    """
    if not origin or not isinstance(origin, dict):
        return None

    platform = origin.get("platform")
    chat_id = str(origin.get("chat_id", ""))
    thread_id = origin.get("thread_id")
    if not platform or not chat_id:
        return None

    # Build a session key that _resolve_binding_from_session_key can parse.
    # For groups: agent:main:{platform}:group:{chat_id}
    # For threads: agent:main:{platform}:thread:{thread_id}:{thread_id}
    chat_type = "thread" if thread_id else "group"
    effective_id = str(thread_id) if thread_id else chat_id
    session_key = f"agent:main:{platform}:{chat_type}:{effective_id}"
    if thread_id:
        session_key += f":{thread_id}"

    _ensure_binding_loaded(session_key)

    result: Dict[str, Any] = {}

    def _read_binding_state(key: str) -> None:
        """Read binding state from in-memory dicts into result."""
        mo = _session_model_overrides.get(key)
        if mo:
            if mo.get("model"):
                result["model"] = mo["model"]
            if mo.get("provider"):
                result["provider"] = mo["provider"]
            # Prefer expanded key; fall back to raw + expand at read time.
            # api_key_raw stores the original ${VAR} for safe /bind save,
            # but callers (Anthropic SDK etc.) need the real key.
            expanded = mo.get("api_key")
            if expanded:
                result["api_key"] = expanded
            else:
                raw_key = mo.get("api_key_raw")
                if raw_key:
                    result["api_key"] = _expand_api_key(raw_key) or raw_key
            if mo.get("base_url"):
                result["base_url"] = os.path.expandvars(mo["base_url"])
        soul = _session_souls.get(key)
        if soul:
            result["soul_content"] = soul
        soul_name = _session_soul_names.get(key)
        if soul_name:
            result["soul_name"] = soul_name
        skills = _session_skills.get(key)
        if skills is not None:
            result["skills"] = skills
        mem = _session_memory_scopes.get(key)
        if mem:
            result["memory_scope"] = mem

    with _state_lock:
        _read_binding_state(session_key)

    if not result:
        # Fallback: for threads, try the parent group channel binding
        if thread_id:
            parent_key = f"agent:main:{platform}:group:{chat_id}"
            _ensure_binding_loaded(parent_key)
            with _state_lock:
                _read_binding_state(parent_key)

    return result if result else None


# ---------------------------------------------------------------------------
# Consolidated session overrides (single lock acquisition)
# ---------------------------------------------------------------------------

def _get_session_overrides(session_key: str) -> Optional[dict]:
    """Consolidated hook: gather ALL binding overrides in a single lock pass.

    Calls ``_ensure_binding_loaded`` once, then reads all state dicts under
    a single ``_state_lock`` acquisition — far cheaper than calling the five
    individual hooks (each of which acquires the lock independently).

    Returns a dict with any of these keys (only non-None values are included):

    * ``model``, ``provider``, ``api_key``, ``base_url``, ``api_mode``
    * ``soul_content``, ``soul_name``
    * ``skills``
    * ``memory_scope``
    * ``personality`` — alias for ``soul_name`` (used by session_search isolation)

    Returns ``None`` if no binding exists for *session_key*.
    """
    _ensure_binding_loaded(session_key)

    result: Dict[str, Any] = {}

    with _state_lock:
        mo = _session_model_overrides.get(session_key)
        if mo:
            if mo.get("model"):
                result["model"] = mo["model"]
            if mo.get("provider"):
                result["provider"] = mo["provider"]
            # Prefer expanded key; fall back to raw + expand at read time.
            # api_key_raw stores the original ${VAR} for safe /bind save,
            # but callers (Anthropic SDK etc.) need the real key.
            expanded = mo.get("api_key")
            if expanded:
                result["api_key"] = expanded
            else:
                raw_key = mo.get("api_key_raw")
                if raw_key:
                    result["api_key"] = _expand_api_key(raw_key) or raw_key
            if mo.get("base_url"):
                result["base_url"] = os.path.expandvars(mo["base_url"])
            if mo.get("api_mode"):
                result["api_mode"] = mo["api_mode"]

        soul = _session_souls.get(session_key)
        if soul:
            result["soul_content"] = soul

        soul_name = _session_soul_names.get(session_key)
        if soul_name:
            result["soul_name"] = soul_name
            result["personality"] = soul_name  # alias for session_search isolation

        skills = _session_skills.get(session_key)
        if skills is not None:
            result["skills"] = skills

        mem = _session_memory_scopes.get(session_key)
        if mem:
            result["memory_scope"] = mem

    return result if result else None


def get_session_overrides(session_key: str) -> Optional[dict]:
    """Public API: get all session overrides from channel binding.

    External callers can import and call this directly, bypassing the hook
    system entirely.
    """
    return _get_session_overrides(session_key)


# ---------------------------------------------------------------------------
# Register all hooks
# ---------------------------------------------------------------------------

register_hook("on_new_session", _on_new_session)
register_hook("on_session_reset", _on_session_reset)
register_hook("on_session_cleanup", _on_session_cleanup)
register_hook("get_model_override", _get_model_override)
register_hook("get_ephemeral", _get_ephemeral)
register_hook("get_skills_override", _get_skills_override)
register_hook("get_memory_scope", _get_memory_scope)
register_hook("get_cron_binding", _get_cron_binding)
register_hook("get_session_overrides", _get_session_overrides)

# Run startup validation
_validate_bindings()


# ---------------------------------------------------------------------------
# Session key parsing helpers
# ---------------------------------------------------------------------------

def _parse_session_key(session_key: str) -> Optional[Dict[str, str]]:
    """Extract platform and channel_id from a session key.

    Session key format: ``agent:main:{platform}:{chat_type}:{chat_id}[:...]``

    Returns dict with keys ``platform``, ``chat_type``, ``channel_id``, and
    optionally ``thread_id`` (for ``dm`` and ``thread`` chat types),
    or ``None`` if the key cannot be parsed.

    Consistent with ``run.py:_parse_session_key`` — the 6th element is only
    returned as ``thread_id`` for chat types where it is unambiguous (``dm``
    and ``thread``).
    """
    if not session_key:
        return None
    parts = session_key.split(":")
    if len(parts) < 5 or parts[0] != "agent":
        return None
    result = {
        "platform": parts[2],
        "chat_type": parts[3],
        "channel_id": parts[4],
    }
    if len(parts) > 5 and parts[3] in ("dm", "thread"):
        result["thread_id"] = parts[5]
    return result


def _get_config_path() -> Path:
    """Resolve the path to config.yaml."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "config.yaml"
    except ImportError:
        return Path.home() / ".hermes" / "config.yaml"


def _load_config_yaml() -> dict:
    """Load config.yaml, returning empty dict on any error."""
    import yaml
    config_path = _get_config_path()
    if not config_path.exists():
        return {}
    try:
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_binding_to_config(platform: str, channel_id: str, binding: dict) -> str | None:
    """Persist a channel binding to config.yaml under the platform's extra section.

    Writes atomically via temp-file + os.replace.  Returns an error message
    on failure, or ``None`` on success.
    """
    import yaml
    import shutil
    from utils import atomic_yaml_write

    config_path = _get_config_path()

    # Load current config
    config = _load_config_yaml()
    if not config:
        return "⚠️ config.yaml not found or empty — cannot save binding."

    # Ensure platform section exists
    if platform not in config or not isinstance(config.get(platform), dict):
        config[platform] = {}

    # Ensure extra section exists
    if "extra" not in config[platform] or not isinstance(config[platform].get("extra"), dict):
        config[platform]["extra"] = {}

    bindings = config[platform]["extra"].get("channel_personality_bindings", [])
    if not isinstance(bindings, list):
        bindings = []

    # Build the binding entry to save — id + soul + optional extras.
    # memory_scope and skills are explicitly persisted for visibility
    # and forward-compatibility; everything else (model, provider, etc.)
    # is still resolved from the soul's frontmatter at load time.
    entry: Dict[str, Any] = {"id": channel_id, "soul": binding.get("soul")}
    for extra_key in ("memory_scope", "skills"):
        if extra_key in binding and binding[extra_key]:
            entry[extra_key] = binding[extra_key]

    # Replace existing entry for same channel_id, or append
    replaced = False
    for i, existing in enumerate(bindings):
        if isinstance(existing, dict) and str(existing.get("id", "")) == channel_id:
            bindings[i] = entry
            replaced = True
            break
    if not replaced:
        bindings.append(entry)

    config[platform]["extra"]["channel_personality_bindings"] = bindings

    # Atomic write
    try:
        atomic_yaml_write(config_path, config)
    except Exception as e:
        logger.error("[ChannelBinding] Failed to write config: %s", e)
        return f"⚠️ Failed to save binding to config.yaml: {e}"

    # Invalidate config cache so the new binding is picked up
    global _CONFIG_BINDINGS_CACHE
    with _state_lock:
        _CONFIG_BINDINGS_CACHE = None

    action = "Updated" if replaced else "Saved"
    logger.info("[ChannelBinding] %s binding for %s:%s → %s", action, platform, channel_id, entry.get("soul"))
    return None  # success


def _remove_binding_from_config(platform: str, channel_id: str) -> str | None:
    """Remove a persisted channel binding from config.yaml.

    Returns an error message on failure, or ``None`` on success.
    """
    from utils import atomic_yaml_write

    config_path = _get_config_path()
    config = _load_config_yaml()
    if not config:
        return "⚠️ config.yaml not found or empty."

    platform_cfg = config.get(platform)
    if not isinstance(platform_cfg, dict):
        return f"⚠️ No config section found for platform '{platform}'."

    extra = platform_cfg.get("extra")
    if not isinstance(extra, dict):
        return f"⚠️ No extra section found for platform '{platform}'."

    bindings = extra.get("channel_personality_bindings", [])
    if not isinstance(bindings, list) or not bindings:
        return f"⚠️ No persisted bindings found for platform '{platform}'."

    # Find and remove the matching entry
    original_len = len(bindings)
    bindings = [b for b in bindings if not (isinstance(b, dict) and str(b.get("id", "")) == channel_id)]

    if len(bindings) == original_len:
        return f"⚠️ No persisted binding found for channel `{channel_id}` on `{platform}`."

    config[platform]["extra"]["channel_personality_bindings"] = bindings

    try:
        atomic_yaml_write(config_path, config)
    except Exception as e:
        logger.error("[ChannelBinding] Failed to write config during unbind: %s", e)
        return f"⚠️ Failed to update config.yaml: {e}"

    # Invalidate cache
    global _CONFIG_BINDINGS_CACHE
    with _state_lock:
        _CONFIG_BINDINGS_CACHE = None

    logger.info("[ChannelBinding] Removed binding for %s:%s", platform, channel_id)
    return None  # success


def _list_all_bindings() -> str:
    """Return a formatted string listing all persisted bindings across all platforms."""
    all_bindings = _get_all_platform_bindings()

    if not all_bindings:
        return "📭 No persisted channel bindings found in config.yaml.\n\nUse `/bind <soul>` then `/bind save` to create one."

    lines = ["📋 **Persisted Channel Bindings:**\n"]
    for platform, bindings in sorted(all_bindings.items()):
        lines.append(f"**{platform.title()}:**")
        for entry in bindings:
            if not isinstance(entry, dict):
                continue
            # Enrich from soul frontmatter for display
            enriched = _enrich_from_soul_frontmatter(dict(entry))
            chan_id = enriched.get("id", "?")
            soul = enriched.get("soul", "—")
            model = enriched.get("model", "")
            scope = enriched.get("memory_scope", "")
            skills = enriched.get("skills", [])
            detail = f"  • `{chan_id}` → soul: `{soul}`"
            if model:
                detail += f" | model: `{model}`"
            if scope:
                detail += f" | scope: `{scope}`"
            if skills:
                skill_names = skills if isinstance(skills, list) else [skills]
                detail += f" | skills: {len(skill_names)}"
            lines.append(detail)
        lines.append("")

    lines.append("_Use `/bind save` to persist the current session binding, `/bind unbind` to remove one._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /bind command handler — extracted from gateway/run.py to reduce pollution
# ---------------------------------------------------------------------------

async def handle_bind_command(session_store, event) -> str:
    """Handle /bind command — dynamically switch soul for current channel.

    Supports:
      /bind              — show current binding
      /bind <soul_name>  — switch soul for this session (session-only)
      /bind --clear      — remove dynamic binding (session-only)
      /bind save         — persist current binding to config.yaml
      /bind list         — list all persisted bindings across all platforms
      /bind unbind       — remove persisted binding for current channel + clear runtime
    """
    from gateway.extensions import fire_hooks_first

    args = event.get_command_args().strip()
    source = event.source
    session_entry = session_store.get_or_create_session(source)
    session_key = session_entry.session_key

    if not session_key:
        return "⚠️ Session key unavailable. Please try again."
    parsed = _parse_session_key(session_key)

    # ── Subcommands ──────────────────────────────────────────────────────

    # /bind list — list all persisted bindings across all platforms
    if args == "list":
        return _list_all_bindings()

    # /bind save — persist current dynamic binding to config.yaml
    if args == "save":
        if not parsed or not parsed.get("channel_id"):
            return "⚠️ Cannot determine channel ID from session — save is not available here."

        platform = parsed["platform"]
        channel_id = parsed["channel_id"]

        with _state_lock:
            current_binding = _session_bindings.get(session_key)
            current_soul = _session_soul_names.get(session_key)
            current_model_override = _session_model_overrides.get(session_key)
            current_skills = _session_skills.get(session_key)
            current_memory_scope = _session_memory_scopes.get(session_key)

        if not current_soul:
            return (
                "⚠️ No active binding to save.\n\n"
                "First set a soul with `/bind <soul_name>`, then use `/bind save` to persist it."
            )

        # Build the config entry: id + soul + memory_scope.
        # memory_scope is explicitly persisted so config.yaml reflects the
        # soul's frontmatter value — makes debugging / inspection easier
        # and preserves scope even if the soul file is renamed or edited.
        entry: Dict[str, Any] = {"soul": current_soul}
        if current_memory_scope:
            entry["memory_scope"] = current_memory_scope
        if current_skills:
            entry["skills"] = current_skills

        error = _save_binding_to_config(platform, channel_id, entry)
        if error:
            return error

        # Signal to the gateway wrapper that a full session reset is needed
        # via the side-channel (avoids modifying MessageEvent).
        set_bind_reset(channel_id)

        _extras = []
        if current_memory_scope:
            _extras.append(f"scope: `{current_memory_scope}`")
        if current_skills:
            _extras.append(f"skills: {len(current_skills)}")
        _extra_str = f" ({', '.join(_extras)})" if _extras else ""

        return (
            f"✅ Binding saved to config.yaml!\n"
            f"Platform: `{platform}` | Channel: `{channel_id}` | Soul: `{current_soul}`{_extra_str}\n"
            f"🧹 Session cleared — fresh context for the new soul.\n"
            f"_(persists across gateway restarts)_"
        )

    # /bind unbind — remove persisted binding + clear runtime
    if args == "unbind":
        if not parsed or not parsed.get("channel_id"):
            return "⚠️ Cannot determine channel ID from session — unbind is not available here."

        platform = parsed["platform"]
        channel_id = parsed["channel_id"]

        # Remove from config
        error = _remove_binding_from_config(platform, channel_id)

        # Always clear runtime state regardless of config result
        with _state_lock:
            _session_souls.pop(session_key, None)
            _session_bindings.pop(session_key, None)
            _session_soul_names.pop(session_key, None)
            _session_model_overrides.pop(session_key, None)
            _session_skills.pop(session_key, None)
            _session_memory_scopes.pop(session_key, None)
            _session_allow_config_write.pop(session_key, None)
            _bound_channel_index.pop((platform, channel_id), None)

        if error:
            return f"{error}\n🔓 Runtime binding cleared anyway — using default personality."

        return (
            f"🔓 Binding removed from config.yaml and cleared from session.\n"
            f"Platform: `{platform}` | Channel: `{channel_id}`\n"
            f"_(using default personality on next message)_"
        )

    # ── Legacy subcommands ───────────────────────────────────────────────

    # /bind --clear
    if args == "--clear":
        with _state_lock:
            _session_souls.pop(session_key, None)
            _session_bindings.pop(session_key, None)
            _session_soul_names.pop(session_key, None)
            _session_model_overrides.pop(session_key, None)
            _session_skills.pop(session_key, None)
            _session_memory_scopes.pop(session_key, None)
            _session_allow_config_write.pop(session_key, None)
            if parsed:
                _bound_channel_index.pop(
                    (parsed.get("platform", ""), parsed.get("channel_id", "")), None
                )
        return "🔓 Soul binding cleared — using default personality.\n_(takes effect on next message)_"

    # /bind <soul_name>
    if args:
        # Reject subcommand-like args to prevent confusion
        if args in ("save", "list", "unbind", "--clear"):
            return "⚠️ Unexpected state — please retry."  # safety net
        soul_name = args.lower().strip()
        result = _load_soul_with_meta(soul_name)
        if not result or not result[0]:
            # List available souls as hint
            try:
                from hermes_constants import get_hermes_home
                souls_dir = get_hermes_home() / "souls"
            except ImportError:
                souls_dir = Path.home() / ".hermes" / "souls"
            available = []
            if souls_dir.exists():
                available = sorted(p.stem for p in souls_dir.glob("*.md"))
            soul_list = ", ".join(f"`{s}`" for s in available) if available else "(none found)"
            return f"⚠️ Soul `{soul_name}` not found in ~/.hermes/souls/\n\nAvailable: {soul_list}"

        # Build binding from soul file frontmatter + soul name
        _content, meta = result
        binding: Dict[str, Any] = {"soul": soul_name, "_content": _content}
        if meta.get("memory_scope"):
            binding["memory_scope"] = meta["memory_scope"]
        if meta.get("skills"):
            binding["skills"] = meta["skills"]
        if meta.get("model"):
            binding["model"] = meta["model"]
        if meta.get("provider"):
            binding["provider"] = meta["provider"]
        if meta.get("base_url"):
            binding["base_url"] = os.path.expandvars(meta["base_url"])
        if meta.get("api_key"):
            binding["api_key"] = meta["api_key"]

        _apply_binding(session_key, binding)

        # Signal to the gateway wrapper that a full session reset is needed
        # (evict cached agent, clear model overrides, flush memories, etc.)
        # We cannot do this here because we don't have access to the gateway
        # instance — the gateway's _handle_bind_command wrapper will pick this
        # up and call _perform_session_reset().
        # Uses side-channel instead of event.extra to avoid modifying MessageEvent.
        if parsed and parsed.get("channel_id"):
            set_bind_reset(parsed["channel_id"])

        extras = []
        if binding.get("memory_scope"):
            extras.append(f"memory: `{binding['memory_scope']}`")
        if binding.get("skills"):
            extras.append(f"skills: {len(binding['skills'])}")
        if binding.get("model"):
            extras.append(f"model: `{binding['model']}`")
        extra_line = f" ({', '.join(extras)})" if extras else ""

        return (
            f"🎭 Soul set to **{soul_name}** for this session{extra_line}.\n"
            f"🧹 Session cleared — fresh context.\n"
            f"_(use `/bind save` to persist)_"
        )

    # /bind (no args) — show current binding
    _ensure_binding_loaded(session_key)
    current_soul = _session_soul_names.get(session_key)
    current_model = fire_hooks_first("get_model_override", session_key)

    if not current_soul:
        return (
            "No soul binding active for this session.\n\n"
            "Usage:\n"
            "  `/bind <soul_name>` — switch soul (session-only)\n"
            "  `/bind save` — persist current binding to config\n"
            "  `/bind list` — show all persisted bindings\n"
            "  `/bind unbind` — remove persisted + runtime binding\n"
            "  `/bind --clear` — clear session binding only"
        )

    lines = [f"🎭 **Current binding:** `{current_soul}`"]
    if current_model:
        lines.append(f"🤖 Model: `{current_model.get('model', 'default')}`")
    with _state_lock:
        scope = _session_memory_scopes.get(session_key)
        skills = _session_skills.get(session_key)
    if scope:
        lines.append(f"📂 Memory scope: `{scope}`")
    if skills:
        lines.append(f"🛠️ Skills: {', '.join(f'`{s}`' for s in skills)}")
    lines.append(
        "\nUsage: `/bind <soul>` switch | `/bind save` persist | "
        "`/bind unbind` remove | `/bind --clear` session-only clear"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public helpers for authorization
# ---------------------------------------------------------------------------

def is_channel_bound(platform: str, chat_id: str) -> bool:
    """Check if a group/channel has any binding (config or dynamic).

    Used by ``_is_user_authorized()`` to auto-open bound groups —
    if an admin has ``/bind``-ed a soul in a group, all group members
    are authorized to chat (no need for explicit allowlisting).

    Uses an O(1) index maintained alongside ``_session_bindings``
    for runtime bindings, then falls back to config-based lookup.

    Args:
        platform: lowercase platform name (e.g. "whatsapp", "telegram")
        chat_id: the chat/channel/group ID to check

    Returns:
        True if the channel has a config binding or a live runtime binding.
    """
    if not platform or not chat_id:
        return False

    # O(1) index check (covers runtime bindings)
    with _state_lock:
        if (platform, str(chat_id)) in _bound_channel_index:
            return True

    # Config-based (persisted /bind save) bindings fallback
    all_bindings = _get_all_platform_bindings()
    platform_bindings = all_bindings.get(platform, [])
    for entry in platform_bindings:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id", "")) == str(chat_id):
            return True

    return False


def has_config_write_permission(session_key: str) -> bool:
    """Check if the session's bound soul allows writing to Hermes config paths.

    Returns True if the session's soul has ``allow_config_write: true`` in
    its frontmatter, or if the session has no binding (unbound sessions
    default to allowed for backward compatibility).

    Used by ``tools/file_tools.py`` to gate writes to config.yaml, souls/,
    and other protected Hermes paths from bound channel agents.
    """
    _ensure_binding_loaded(session_key)
    with _state_lock:
        # No binding = not a bound channel, allow by default
        if session_key not in _session_bindings:
            return True
        return _session_allow_config_write.get(session_key, False)
