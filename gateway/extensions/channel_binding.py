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
    memory_scope omitted → default (unscoped, backward compatible)

Resolution:
    1. ``event.extra["channel_binding"]`` — set by adapter (Discord)
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

# Config cache for cross-platform binding resolution (invalidated on /reset)
_CONFIG_BINDINGS_CACHE: Optional[Dict[str, list]] = None


# ---------------------------------------------------------------------------
# Soul loading
# ---------------------------------------------------------------------------

def _load_soul_content(soul_name: str) -> Optional[str]:
    """Load soul content from ~/.hermes/souls/<soul_name>.md."""
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
        content = soul_path.read_text(encoding="utf-8").strip()
        return content if content else None
    except Exception as e:
        logger.warning("[ChannelBinding] Failed to load soul '%s': %s", soul_name, e)
        return None


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
    1. ``event.extra["channel_binding"]`` — set by platform adapter (Discord)
    2. Session-key → config match — works for any platform without adapter changes
    """
    binding = None

    # Path 1: adapter-provided binding (Discord sets event.extra)
    if hasattr(event, "extra") and isinstance(getattr(event, "extra", None), dict):
        binding = event.extra.get("channel_binding")

    # Path 2: resolve from session_key + config (works for any platform)
    if not binding:
        binding = _resolve_binding_from_session_key(session_key)

    if not binding or not isinstance(binding, dict):
        return

    _apply_binding(session_key, binding)


def _apply_binding(session_key: str, binding: dict) -> None:
    """Store soul + skills + model + memory_scope from a resolved binding dict."""
    with _state_lock:
        _apply_binding_unlocked(session_key, binding)


def _apply_binding_unlocked(session_key: str, binding: dict) -> None:
    """Internal: mutate session state (caller must hold _state_lock)."""
    soul_name = binding.get("soul")
    model = binding.get("model")
    skills = binding.get("skills")
    memory_scope = binding.get("memory_scope")

    # Store the raw binding for /reset restore
    _session_bindings[session_key] = binding

    # Load soul content
    if soul_name and isinstance(soul_name, str):
        content = _load_soul_content(soul_name)
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

    # Store model override
    if model:
        _session_model_overrides[session_key] = {
            "model": model,
            "provider": binding.get("provider"),
            "api_key": _expand_api_key(binding.get("api_key")),
            "base_url": binding.get("base_url"),
            "api_mode": None,
        }
        logger.info(
            "[ChannelBinding] Model '%s' (provider=%s) → session %s",
            model, binding.get("provider"), session_key[:30],
        )

    # Store memory scope
    if memory_scope and isinstance(memory_scope, str):
        _session_memory_scopes[session_key] = memory_scope
        logger.info(
            "[ChannelBinding] Memory scope '%s' → session %s",
            memory_scope, session_key[:30],
        )


def _resolve_binding_from_session_key(session_key: str) -> Optional[dict]:
    """Resolve a channel binding from session_key + config.

    Parses ``agent:main:{platform}:{chat_type}:{chat_id}[:{thread_id}[:...]]``
    and matches against all configured platform bindings.

    Matching strategy:
      1. Exact match on chat_id (position 4)
      2. For threads, also try the thread_id (position 5) against config IDs
      3. For threads, also try to match the parent channel's bindings
         by stripping the thread_id and checking the parent chat_id
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

    # Strategy 1: exact match on chat_id
    for entry in platform_bindings:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id", ""))
        if entry_id == chat_id:
            return entry

    # Strategy 2: for threads, also try thread_id and consider that
    # the session key may use the thread's own ID as chat_id.
    # In Discord, the parent channel ID is NOT in the session key for threads,
    # so we check if any binding's ID matches any part of the key.
    if chat_type == "thread" and thread_id:
        # The session key for a thread is agent:main:discord:thread:{thread_id}:{thread_id}
        # We need to check if any binding could be the parent channel.
        # Since we can't know the parent ID from the session key alone,
        # we check all bindings that have the same platform.
        # The Discord adapter handles this via event.extra["channel_binding"]
        # in Path 1 of _on_new_session, but this fallback helps for
        # cached/restarted sessions where on_new_session didn't fire.
        for entry in platform_bindings:
            if not isinstance(entry, dict):
                continue
            # If the entry has a threads list, check if our thread_id is in it
            entry_threads = entry.get("threads", [])
            if isinstance(entry_threads, list) and thread_id in [str(t) for t in entry_threads]:
                return entry

    return None


def _get_all_platform_bindings() -> Dict[str, list]:
    """Load channel_personality_bindings for all platforms from config.yaml.

    Returns {platform_name: [binding_dicts]}.
    Result is cached on first call.
    """
    global _CONFIG_BINDINGS_CACHE

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
        _CONFIG_BINDINGS_CACHE = result
        return result

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        _CONFIG_BINDINGS_CACHE = result
        return result

    if not isinstance(raw, dict):
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

    _CONFIG_BINDINGS_CACHE = result
    logger.debug("[ChannelBinding] Loaded bindings for platforms: %s", list(result.keys()))
    return result


def _on_session_reset(session_key: str) -> None:
    """Re-apply channel binding after /reset.

    Re-loads the soul content from disk (in case the file changed),
    restores overrides, and invalidates config cache for hot-reload.
    """
    global _CONFIG_BINDINGS_CACHE
    _CONFIG_BINDINGS_CACHE = None

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
        _session_souls.pop(session_key, None)
        _session_model_overrides.pop(session_key, None)
        _session_soul_names.pop(session_key, None)
        _session_bindings.pop(session_key, None)
        _session_skills.pop(session_key, None)
        _session_memory_scopes.pop(session_key, None)


def _get_model_override(session_key: str) -> Optional[Dict[str, Optional[str]]]:
    """Return the channel binding model override for a session, or None."""
    with _state_lock:
        return _session_model_overrides.get(session_key)


def _get_ephemeral(session_key: str) -> Optional[str]:
    """Return the per-session soul override content, or None."""
    with _state_lock:
        return _session_souls.get(session_key)


def _get_skills_override(session_key: str) -> Optional[List[str]]:
    """Return the per-session skills override, or None.

    Used by gateway/run.py to inject skills into new sessions.
    An empty list means "all skills" (God mode).
    """
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
    with _state_lock:
        scope = _session_memory_scopes.get(session_key)
        if scope is not None:
            return scope

    # Fallback: re-resolve binding from session key + config (outside lock)
    binding = _resolve_binding_from_session_key(session_key)
    if binding and isinstance(binding, dict):
        memory_scope = binding.get("memory_scope")
        if memory_scope and isinstance(memory_scope, str):
            with _state_lock:
                _session_memory_scopes[session_key] = memory_scope
            logger.info(
                "[ChannelBinding] Memory scope '%s' re-resolved for session %s",
                memory_scope, session_key[:30],
            )
            return memory_scope

    return None


# ---------------------------------------------------------------------------
# Scoped memory resolution (used by MemoryStore via run_agent.py)
# ---------------------------------------------------------------------------

def resolve_memory_dirs(scope: Optional[str]) -> List[Path]:
    """Resolve memory directories for a given scope.

    Returns a list of directories to read, in priority order:
      - ``_global/`` always first (shared context)
      - Scoped directory (if scope is a named scope like "dev")
      - ALL scope directories (if scope is "*")

    This function is the public API used by run_agent.py to pass
    memory directories to MemoryStore.
    """
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home() / "memories"
    except ImportError:
        base = Path.home() / ".hermes" / "memories"

    dirs: List[Path] = []

    # _global/ is always included
    global_dir = base / "_global"
    if global_dir.exists():
        dirs.append(global_dir)

    if not scope or not scope.strip():
        # No scope (or whitespace-only) — read from base dir (backward compatible)
        return dirs if dirs else [base]

    if scope == "*":
        # God mode — read all subdirectories except _global (already added)
        if base.exists():
            for child in sorted(base.iterdir()):
                if child.is_dir() and child.name != "_global" and not child.name.startswith("."):
                    dirs.append(child)
        return dirs

    # Named scope — read specific subdirectory
    # Validate against path traversal and null bytes
    if "\x00" in scope or "/" in scope or "\\" in scope or ".." in scope:
        logger.error("Invalid memory scope (path traversal): %s", scope)
        return dirs if dirs else [base]

    scoped_dir = base / scope
    if scoped_dir.exists():
        dirs.append(scoped_dir)
    else:
        # Auto-create on first access
        scoped_dir.mkdir(parents=True, exist_ok=True)
        dirs.append(scoped_dir)

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

# Run startup validation
_validate_bindings()


# ---------------------------------------------------------------------------
# Session key parsing helpers
# ---------------------------------------------------------------------------

def _parse_session_key(session_key: str) -> Optional[Dict[str, str]]:
    """Extract platform and channel_id from a session key.

    Session key format: ``agent:main:{platform}:{chat_type}:{chat_id}[:...]``

    Returns dict with keys ``platform``, ``chat_type``, ``channel_id``,
    or ``None`` if the key cannot be parsed.
    """
    if not session_key:
        return None
    parts = session_key.split(":")
    if len(parts) < 5 or parts[0] != "agent":
        return None
    return {
        "platform": parts[2],
        "chat_type": parts[3],
        "channel_id": parts[4] if len(parts) > 4 else None,
    }


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

    # Build the binding entry to save — only include non-None fields
    entry: Dict[str, Any] = {"id": channel_id}
    for key in ("soul", "model", "provider", "base_url", "api_key", "memory_scope"):
        val = binding.get(key)
        if val is not None:
            entry[key] = val
    skills = binding.get("skills")
    if skills:
        entry["skills"] = skills if isinstance(skills, list) else [skills]

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
        return "⚠️ No config section found for platform '{platform}'."

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
            chan_id = entry.get("id", "?")
            soul = entry.get("soul", "—")
            model = entry.get("model", "")
            scope = entry.get("memory_scope", "")
            skills = entry.get("skills", [])
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
    session_key = session_entry.session_id or source
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

        # Build the config entry from current session state
        entry: Dict[str, Any] = {"soul": current_soul}
        if current_model_override:
            for key in ("model", "provider", "base_url", "api_key"):
                val = current_model_override.get(key)
                if val:
                    entry[key] = val
        if current_skills:
            entry["skills"] = current_skills
        if current_memory_scope:
            entry["memory_scope"] = current_memory_scope

        error = _save_binding_to_config(platform, channel_id, entry)
        if error:
            return error

        return (
            f"✅ Binding saved to config.yaml!\n"
            f"Platform: `{platform}` | Channel: `{channel_id}` | Soul: `{current_soul}`\n"
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
        return "🔓 Soul binding cleared — using default personality.\n_(takes effect on next message)_"

    # /bind <soul_name>
    if args:
        # Reject subcommand-like args to prevent confusion
        if args in ("save", "list", "unbind", "--clear"):
            pass  # already handled above; shouldn't reach here
        soul_name = args.lower().strip()
        content = _load_soul_content(soul_name)
        if not content:
            # List available souls as hint
            try:
                from hermes_constants import get_hermes_home
                souls_dir = get_hermes_home() / "souls"
            except ImportError:
                from pathlib import Path
                souls_dir = Path.home() / ".hermes" / "souls"
            available = []
            if souls_dir.exists():
                available = sorted(p.stem for p in souls_dir.glob("*.md"))
            soul_list = ", ".join(f"`{s}`" for s in available) if available else "(none found)"
            return f"⚠️ Soul `{soul_name}` not found in ~/.hermes/souls/\n\nAvailable: {soul_list}"

        # Apply the binding (soul only, no model override for dynamic bind)
        binding = {"soul": soul_name}
        _apply_binding(session_key, binding)
        return (
            f"🎭 Soul set to **{soul_name}** for this session.\n"
            f"_(takes effect on next message — use `/bind save` to persist)_"
        )

    # /bind (no args) — show current binding
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
