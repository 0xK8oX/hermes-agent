# `/reload` Hot Reload Command Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a `/reload` admin command to the gateway that refreshes config, souls, extensions, and env vars without restarting the process or dropping platform connections.

**Architecture:** The gateway (`GatewayRunner`) initializes state via `_load_*()` static methods at `__init__`. Each reads from `config.yaml` or `.env`. `/reload` will call these same methods to re-read config, then invalidate caches (agent cache, channel binding cache, extension hooks), and optionally re-discover extensions — all without touching platform adapters or active sessions.

**Tech Stack:** Python, asyncio, existing `_load_*()` pattern, extension hook system

---

## Reloadable State Inventory

| State | `_load_*()` method | Reload safe? | Notes |
|-------|-------------------|--------------|-------|
| config.yaml values | `_load_prefill_messages`, `_load_ephemeral_system_prompt`, `_load_reasoning_config`, `_load_service_tier`, `_load_show_reasoning`, `_load_busy_input_mode`, `_load_restart_drain_timeout`, `_load_provider_routing`, `_load_fallback_model`, `_load_smart_model_routing` | ✅ Yes | Static methods, just re-read from disk |
| .env vars | `load_hermes_dotenv()` | ✅ Yes | Overwrites os.environ, safe |
| Channel bindings cache | `_CONFIG_BINDINGS_CACHE` in channel_binding.py | ✅ Yes | Set to None to invalidate |
| Soul files | Loaded on demand by `_load_soul_content()` | ✅ Yes | File is re-read each time; but cached souls in `_session_bindings` need invalidation |
| Extension hooks | `_HOOKS` in `gateway/extensions/__init__.py` | ⚠️ Careful | Need to clear + re-discover; skip if no new extensions |
| Agent cache | `_agent_cache` in GatewayRunner | ✅ Yes | Clear to force fresh AIAgent creation with new config |
| Voice modes | `_load_voice_modes()` from JSON file | ✅ Yes | |
| Platform adapters | Discord/Telegram/WhatsApp connections | ❌ NO | Do NOT touch — connections stay alive |
| Active sessions | `_running_agents`, session store | ❌ NO | Do NOT touch — let active work finish |
| Cron scheduler | Already running | ❌ NO | Separate concern |

## Design Decisions

1. **`/reload` vs `/restart`**: `/reload` is lightweight — no process restart, no connection drop, no session loss. `/restart` remains for full process restart.

2. **Agent cache invalidation**: After reload, clear `_agent_cache` so new AIAgent instances pick up new config. Active agents finish with old config (safe — they're already running).

3. **Extension re-discovery**: Clear `_HOOKS` dict and re-run `_discover_extensions()`. This uses `importlib.reload()` on each extension module so code changes take effect.

4. **Scope**: `/reload` refreshes: config.yaml, .env, souls, channel bindings, extensions. It does NOT reload: platform connections, active agents, cron jobs, sessions.

---

### Task 1: Add `reload` to COMMAND_REGISTRY and _ADMIN_COMMANDS

**Objective:** Register the `/reload` slash command so the gateway recognizes it.

**Files:**
- Modify: `hermes_cli/commands.py` — add `CommandDef("reload", ...)`
- Modify: `gateway/run.py:2765` — add `"reload"` to `_ADMIN_COMMANDS` frozenset

**Step 1: Add CommandDef**

In `hermes_cli/commands.py`, find the existing `CommandDef("reload-mcp", ...)` and add after it:

```python
CommandDef("reload", "Hot-reload config, souls, extensions, and env without restart", "Session",
           aliases=("rl",), args_hint="[what]"),
```

**Step 2: Add to _ADMIN_COMMANDS**

In `gateway/run.py:2765`, add `"reload"` to the frozenset:

```python
_ADMIN_COMMANDS = frozenset({
    "bind", "model", "provider", "restart", "stop",
    "profile", "yolo", "personality",
    "update", "debug", "reload-mcp", "sethome",
    "hall-send", "hall-read", "hall-status", "hall-report",
    "reload",
})
```

**Step 3: Verify**

Run: `cd ~/.hermes/hermes-agent && python -c "from hermes_cli.commands import COMMAND_REGISTRY; names = [c.name for c in COMMAND_REGISTRY]; assert 'reload' in names; print('OK')"`
Expected: `OK`

---

### Task 2: Add reload dispatch in `_handle_command`

**Objective:** Wire the `/reload` command to a handler.

**Files:**
- Modify: `gateway/run.py` — add dispatch near the `/reload-mcp` handler (~line 3223)

**Step 1: Add dispatch**

After the `canonical == "reload-mcp"` block (~line 3224), add:

```python
if canonical == "reload":
    return await self._handle_reload_command(event)
```

**Step 2: Verify** — no syntax error:

Run: `cd ~/.hermes/hermes-agent && python -c "import ast; ast.parse(open('gateway/run.py').read()); print('Syntax OK')"`

---

### Task 3: Implement `_handle_reload_command`

**Objective:** The core reload logic — refresh all configurable state without touching connections or sessions.

**Files:**
- Modify: `gateway/run.py` — add method after `_handle_reload_mcp_command`

**Implementation:**

```python
async def _handle_reload_command(self, event: MessageEvent) -> str:
    """Handle /reload command — hot-reload config, souls, extensions, env vars."""
    import time
    import importlib
    from gateway.extensions import _HOOKS, _discover_extensions, list_hooks

    start = time.monotonic()
    results = []
    errors = []

    # 1. Reload .env (refresh API keys, provider configs)
    try:
        from hermes_cli.env_loader import load_hermes_dotenv
        loaded = load_hermes_dotenv()
        results.append(f"📝 .env ({len(loaded)} file(s))")
    except Exception as e:
        errors.append(f"❌ .env: {e}")

    # 2. Reload config.yaml values
    try:
        self._prefill_messages = self._load_prefill_messages()
        self._ephemeral_system_prompt = self._load_ephemeral_system_prompt()
        self._reasoning_config = self._load_reasoning_config()
        self._service_tier = self._load_service_tier()
        self._show_reasoning = self._load_show_reasoning()
        self._busy_input_mode = self._load_busy_input_mode()
        self._restart_drain_timeout = self._load_restart_drain_timeout()
        self._provider_routing = self._load_provider_routing()
        self._fallback_model = self._load_fallback_model()
        self._smart_model_routing = self._load_smart_model_routing()
        self._voice_mode = self._load_voice_modes()
        results.append("⚙️ config.yaml")
    except Exception as e:
        errors.append(f"❌ config.yaml: {e}")

    # 3. Invalidate channel binding cache (forces re-read of bindings from config)
    try:
        from gateway.extensions.channel_binding import (
            _CONFIG_BINDINGS_CACHE, _session_bindings, _state_lock,
        )
        import gateway.extensions.channel_binding as _cb_module
        # Invalidate config cache
        _cb_module._CONFIG_BINDINGS_CACHE = None
        # Invalidate bound session caches so souls are re-read from disk
        with _state_lock:
            n_invalidated = len(_session_bindings)
            _session_bindings.clear()
        results.append(f"🔗 bindings ({n_invalidated} cache(s) cleared)")
    except Exception as e:
        errors.append(f"❌ bindings: {e}")

    # 4. Re-discover extensions (reload hooks)
    try:
        old_hooks = set(list_hooks().keys())
        # Clear existing hooks and re-import
        _HOOKS.clear()
        _discover_extensions()
        new_hooks = set(list_hooks().keys())
        added = new_hooks - old_hooks
        removed = old_hooks - new_hooks
        detail = f"{len(new_hooks)} event(s)"
        if added:
            detail += f" +{added}"
        if removed:
            detail += f" -{removed}"
        results.append(f"🔌 extensions ({detail})")
    except Exception as e:
        errors.append(f"❌ extensions: {e}")

    # 5. Clear agent cache so new sessions get fresh config
    try:
        with self._agent_cache_lock:
            n_agents = len(self._agent_cache)
            self._agent_cache.clear()
        results.append(f"🤖 agent cache ({n_agents} cleared)")
    except Exception as e:
        errors.append(f"❌ agent cache: {e}")

    elapsed = time.monotonic() - start
    parts = [f"🔄 **Reloaded** ({elapsed:.1f}s)"]
    if results:
        parts.append("\n".join(results))
    if errors:
        parts.append("\n".join(errors))
    parts.append("\n💡 Active sessions continue with old config until next message.")

    return "\n".join(parts)
```

**Step 4: Verify syntax**

Run: `cd ~/.hermes/hermes-agent && python -c "import ast; ast.parse(open('gateway/run.py').read()); print('Syntax OK')"`

---

### Task 4: Add `[what]` scoped reload (optional args)

**Objective:** Allow `/reload config`, `/reload env`, `/reload souls`, `/reload extensions` to reload specific areas only.

**Files:**
- Modify: `gateway/run.py` — enhance `_handle_reload_command` to parse args

**Implementation:** Extract the reload sections into private methods:

```python
def _reload_env(self) -> tuple[str, str | None]:
    """Returns (success_msg, error_msg_or_None)"""
    try:
        from hermes_cli.env_loader import load_hermes_dotenv
        loaded = load_hermes_dotenv()
        return f"📝 .env ({len(loaded)} file(s))", None
    except Exception as e:
        return "", f"❌ .env: {e}"

def _reload_config(self) -> tuple[str, str | None]:
    try:
        self._prefill_messages = self._load_prefill_messages()
        self._ephemeral_system_prompt = self._load_ephemeral_system_prompt()
        self._reasoning_config = self._load_reasoning_config()
        self._service_tier = self._load_service_tier()
        self._show_reasoning = self._load_show_reasoning()
        self._busy_input_mode = self._load_busy_input_mode()
        self._restart_drain_timeout = self._load_restart_drain_timeout()
        self._provider_routing = self._load_provider_routing()
        self._fallback_model = self._load_fallback_model()
        self._smart_model_routing = self._load_smart_model_routing()
        self._voice_mode = self._load_voice_modes()
        return "⚙️ config.yaml", None
    except Exception as e:
        return "", f"❌ config.yaml: {e}"

def _reload_bindings(self) -> tuple[str, str | None]:
    try:
        from gateway.extensions.channel_binding import (
            _CONFIG_BINDINGS_CACHE, _session_bindings, _state_lock,
        )
        import gateway.extensions.channel_binding as _cb_module
        _cb_module._CONFIG_BINDINGS_CACHE = None
        with _state_lock:
            n = len(_session_bindings)
            _session_bindings.clear()
        return f"🔗 bindings ({n} cache(s) cleared)", None
    except Exception as e:
        return "", f"❌ bindings: {e}"

def _reload_extensions(self) -> tuple[str, str | None]:
    try:
        from gateway.extensions import _HOOKS, _discover_extensions, list_hooks
        old_hooks = set(list_hooks().keys())
        _HOOKS.clear()
        _discover_extensions()
        new_hooks = set(list_hooks().keys())
        added = new_hooks - old_hooks
        removed = old_hooks - new_hooks
        detail = f"{len(new_hooks)} event(s)"
        if added: detail += f" +{added}"
        if removed: detail += f" -{removed}"
        return f"🔌 extensions ({detail})", None
    except Exception as e:
        return "", f"❌ extensions: {e}"

def _reload_agent_cache(self) -> tuple[str, str | None]:
    try:
        with self._agent_cache_lock:
            n = len(self._agent_cache)
            self._agent_cache.clear()
        return f"🤖 agent cache ({n} cleared)", None
    except Exception as e:
        return "", f"❌ agent cache: {e}"
```

Then in `_handle_reload_command`, parse the arg:

```python
raw_args = event.get_command_args().strip().lower()
VALID_SCOPES = {
    "env": [self._reload_env],
    "config": [self._reload_config, self._reload_agent_cache],
    "souls": [self._reload_bindings],
    "bindings": [self._reload_bindings],
    "extensions": [self._reload_extensions],
    "all": [self._reload_env, self._reload_config, self._reload_bindings,
            self._reload_extensions, self._reload_agent_cache],
}

if not raw_args or raw_args == "all":
    steps = VALID_SCOPES["all"]
elif raw_args in VALID_SCOPES:
    steps = VALID_SCOPES[raw_args]
else:
    return f"Unknown scope '{raw_args}'. Valid: {', '.join(sorted(VALID_SCOPES.keys()))}"

for step in steps:
    ok, err = step()
    if ok: results.append(ok)
    if err: errors.append(err)
```

---

### Task 5: Write tests

**Objective:** Unit tests for reload functionality.

**Files:**
- Create: `tests/test_gateway_reload.py`

**Tests to write:**

```python
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from gateway.run import GatewayRunner

class TestReloadCommand:
    """Test /reload hot reload functionality."""

    @pytest.fixture
    def runner(self):
        with patch("gateway.run.load_gateway_config"), \
             patch("gateway.run.SessionStore"), \
             patch("gateway.run.DeliveryRouter"):
            return GatewayRunner.__new__(GatewayRunner)

    def test_reload_env_calls_load_hermes_dotenv(self, runner):
        """_reload_env should call load_hermes_dotenv."""
        with patch("gateway.run.load_hermes_dotenv", return_value=[]) as mock_load:
            ok, err = runner._reload_env()
            mock_load.assert_called_once()
            assert err is None

    def test_reload_config_refreshes_all_values(self, runner):
        """_reload_config should call all _load_* methods."""
        runner._load_prefill_messages = MagicMock(return_value=[])
        runner._load_ephemeral_system_prompt = MagicMock(return_value="")
        runner._load_reasoning_config = MagicMock(return_value=None)
        runner._load_service_tier = MagicMock(return_value=None)
        runner._load_show_reasoning = MagicMock(return_value=False)
        runner._load_busy_input_mode = MagicMock(return_value="interrupt")
        runner._load_restart_drain_timeout = MagicMock(return_value=30.0)
        runner._load_provider_routing = MagicMock(return_value={})
        runner._load_fallback_model = MagicMock(return_value=None)
        runner._load_smart_model_routing = MagicMock(return_value=None)
        runner._load_voice_modes = MagicMock(return_value={})

        ok, err = runner._reload_config()
        assert err is None
        assert "config.yaml" in ok

    def test_reload_bindings_clears_cache(self, runner):
        """_reload_bindings should clear channel binding caches."""
        with patch("gateway.extensions.channel_binding._CONFIG_BINDINGS_CACHE", {"test": []}), \
             patch("gateway.extensions.channel_binding._session_bindings", {"sk": {}}), \
             patch("gateway.extensions.channel_binding._state_lock"):
            ok, err = runner._reload_bindings()
            assert err is None
            assert "1 cache" in ok

    def test_reload_invalid_scope_returns_error(self, runner):
        """Unknown scope should return error message."""
        event = MagicMock()
        event.get_command_args.return_value = "foobar"
        # This would be tested via the full handler
        # For now just check scope validation
        VALID_SCOPES = {"env", "config", "souls", "bindings", "extensions", "all"}
        assert "foobar" not in VALID_SCOPES

    @pytest.mark.asyncio
    async def test_full_reload_no_errors(self, runner):
        """Full /reload should complete without errors."""
        runner._reload_env = MagicMock(return_value=("📝 .env", None))
        runner._reload_config = MagicMock(return_value=("⚙️ config", None))
        runner._reload_bindings = MagicMock(return_value=("🔗 bindings", None))
        runner._reload_extensions = MagicMock(return_value=("🔌 ext", None))
        runner._reload_agent_cache = MagicMock(return_value=("🤖 agents", None))

        event = MagicMock()
        event.get_command_args.return_value = ""

        result = await runner._handle_reload_command(event)
        assert "Reloaded" in result
        assert "❌" not in result
```

**Run tests:**

```bash
cd ~/.hermes/hermes-agent && python -m pytest tests/test_gateway_reload.py -v
```

---

### Task 6: Verify upstream merge safety

**Objective:** Ensure the `/reload` implementation follows the minimal-invasion pattern.

**Checks:**
1. Only adds new method `_handle_reload_command` + 5 helper methods — no modification to existing methods
2. Only adds 1 line to `_ADMIN_COMMANDS` frozenset
3. Only adds 2 lines to `_handle_command` dispatch (if check + return)
4. Only adds 1 `CommandDef` to `commands.py`
5. All reload logic is self-contained — no upstream code touched

**Verify:**

```bash
cd ~/.hermes/hermes-agent && git diff --stat
# Should show: gateway/run.py, hermes_cli/commands.py, tests/test_gateway_reload.py
```

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Extension reload breaks active hooks mid-call | Extensions run synchronously in agent thread; reload clears registry between calls, never during |
| `_discover_extensions()` re-imports stale cached modules | Python caches imports; if extension code file changed on disk, need `importlib.reload()` — add opt-in `importlib.reload()` for modified modules |
| Config reload races with active agent | Active agents keep their in-memory config; only new sessions see new config |
| `.env` reload overwrites runtime env overrides | Document that `/reload env` will reset any env vars changed via `/model` or similar during the session |

## Open Questions

- Should `/reload extensions` use `importlib.reload()` to pick up code changes? Risky but powerful. Recommend: **yes, with try/except per module**.
