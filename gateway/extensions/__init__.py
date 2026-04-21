"""
Gateway Extensions — Lightweight Hook System
=============================================

Self-contained extensions that plug into the gateway via a minimal hook
dispatcher.  Each extension registers callbacks for named events; run.py
fires those events at fixed locations.

This keeps upstream merge conflicts near zero: run.py has exactly 5 fixed
``fire_hooks()`` calls that never change, and all extension logic lives in
this package.

Available hooks
---------------
- ``on_new_session(session_key, event)``       — new conversation started
- ``on_session_reset(session_key)``            — /reset command
- ``on_session_cleanup(session_key)``          — session expired / evicted
- ``get_model_override(session_key)``          — return dict or None
- ``get_ephemeral(session_key)``               — return str or None

Adding a new extension
----------------------
1. Create ``gateway/extensions/your_feature.py``
2. Call ``register_hook("on_new_session", your_handler)`` at module level
3. That's it — no changes needed in run.py or any other file.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hook registry
# ---------------------------------------------------------------------------

_HOOKS: Dict[str, List[Callable]] = {}
_hooks_lock = threading.Lock()


def register_hook(event: str, fn: Callable) -> None:
    """Register *fn* to be called when *event* fires."""
    with _hooks_lock:
        _HOOKS.setdefault(event, []).append(fn)


def fire_hooks(event: str, *args: Any, **kwargs: Any) -> List[Any]:
    """Fire all registered callbacks for *event*, collecting non-None results.

    Exceptions are caught and logged per-callback so one broken extension
    cannot take down the gateway.
    """
    with _hooks_lock:
        hooks = list(_HOOKS.get(event, []))
    results: List[Any] = []
    for fn in hooks:
        try:
            r = fn(*args, **kwargs)
            if r is not None:
                results.append(r)
        except Exception:
            logger.debug("[Extensions] Error in hook %s/%s", event, getattr(fn, "__name__", fn), exc_info=True)
    return results


def fire_hooks_first(event: str, *args: Any, **kwargs: Any) -> Any:
    """Fire hooks and return the **first** non-None result, or None."""
    with _hooks_lock:
        hooks = list(_HOOKS.get(event, []))
    for fn in hooks:
        try:
            r = fn(*args, **kwargs)
            if r is not None:
                return r
        except Exception:
            logger.debug("[Extensions] Error in hook %s/%s", event, getattr(fn, "__name__", fn), exc_info=True)
    return None


def list_hooks() -> Dict[str, List[str]]:
    """Return a summary of registered hooks (for diagnostics)."""
    with _hooks_lock:
        return {event: [getattr(fn, "__name__", str(fn)) for fn in fns]
                for event, fns in _HOOKS.items()}


# ---------------------------------------------------------------------------
# Auto-discover extensions in this package
# ---------------------------------------------------------------------------

def _discover_extensions() -> None:
    """Import all ``.py`` modules in ``gateway/extensions/`` (except __init__).

    Each module's top-level ``register_hook()`` calls register themselves.
    """
    import importlib
    import pkgutil
    from pathlib import Path

    pkg_dir = Path(__file__).parent
    for info in pkgutil.iter_modules([str(pkg_dir)]):
        if info.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"gateway.extensions.{info.name}")
            logger.debug("[Extensions] Loaded: %s", info.name)
        except Exception:
            logger.warning("[Extensions] Failed to load %s", info.name, exc_info=True)


# Discover on first import
_discover_extensions()
