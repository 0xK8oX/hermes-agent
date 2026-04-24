# Claude Code Project Instructions

## Upstream Sync Rules (Critical)

This project is a fork of NousResearch/hermes-agent. We maintain a feature branch `feature/channel-binding-hook-system` that must stay sync'd with upstream `main`. The #1 failure mode is **silently dropping upstream functions during merge**.

### NEVER Use `git merge -X ours origin/main`

`-X ours` resolves all conflicts in favor of our branch, which also **discards upstream additions** to files we modified. Functions added to upstream after our branch diverged simply vanish. Past incidents:

- `_is_kimi_coding_endpoint` → runtime `NameError`
- `_iter_plugin_command_entries` → broken command registration
- `_wait_for_user_dbus_socket`, `_preflight_user_systemd` → systemd failures
- `_plugin_image_gen_providers`, `_coerce_statusbar` → TUI failures

### Pre-Merge Checklist

1. **Count commits behind:** `git log --oneline feature/channel-binding-hook-system..origin/main | wc -l`
2. **Find shared files (danger zone):**
   ```bash
   BASE=$(git merge-base HEAD origin/main)
   cat <(git diff --name-only $BASE origin/main) <(git diff --name-only $BASE HEAD) | sort | uniq -d
   ```
3. **List upstream functions added to shared files:**
   ```bash
   git diff $BASE origin/main -- hermes_cli/gateway.py | grep '^+def '
   git diff $BASE origin/main -- hermes_cli/tools_config.py | grep '^+def '
   git diff $BASE origin/main -- tui_gateway/server.py | grep '^+def '
   git diff $BASE origin/main -- agent/anthropic_adapter.py | grep '^+def '
   git diff $BASE origin/main -- agent/auxiliary_client.py | grep '^+def '
   ```
4. **Run baseline tests:** `pytest tests/ -x -q --tb=no 2>&1 | tail -5`
5. **Run baseline healthcheck:** `python3 scripts/channel_binding_healthcheck.py`

### Merge Strategy

Use standard merge with manual conflict resolution:
```bash
git checkout feature/channel-binding-hook-system
git fetch origin
git merge origin/main
# Resolve conflicts in editor, keeping BOTH our hooks AND upstream functions
```

If a file has only additive changes (e.g. `gateway/extensions/__init__.py`), you can `git checkout --ours` for that file, but **always verify** with `git diff origin/main -- <file>` afterwards.

### Post-Merge Checklist

1. **Audit for lost functions:**
   ```bash
   BASE=$(git merge-base HEAD origin/main)
   for f in hermes_cli/gateway.py hermes_cli/tools_config.py tui_gateway/server.py \
            agent/anthropic_adapter.py agent/auxiliary_client.py; do
     git diff $BASE origin/main -- "$f" | grep '^+def ' | sed 's/^+def //' | \
     awk -F'[:(]' '{print $1}' | while read func; do
       [ -n "$func" ] && ! grep -q "def $func(" "$f" 2>/dev/null && echo "MISSING: $func in $f"
     done
   done
   ```
2. **Smoke test imports:**
   ```bash
   python3 -c "import hermes_cli.gateway; import hermes_cli.tools_config; import tui_gateway.server"
   python3 -c "import agent.anthropic_adapter; import agent.auxiliary_client"
   ```
3. **Run tests:** `pytest tests/ -x -q --tb=line 2>&1 | tail -10`
4. **Run healthcheck:** `python3 scripts/channel_binding_healthcheck.py`
5. **Restart gateway:** `hermes gateway restart && sleep 5 && hermes gateway status`
6. **Check for zombies:** `pgrep -fa "hermes gateway" | grep -v grep`

## Project Context

- **Primary branch:** `feature/channel-binding-hook-system`
- **Upstream:** `origin/main` (NousResearch/hermes-agent)
- **Our fork remote:** `fork` (0xK8oX/hermes-agent)
- **Main branch purpose:** Keep `main` clean and in sync with upstream only. Do not merge feature work into `main`.
- **Feature branch work:** Channel binding hook system, Hall inter-soul messaging, cross-channel dispatch, session checkpointing.

## Key Files

- `gateway/extensions/__init__.py` — Hook registry (`register_hook`, `fire_hooks`, `fire_hooks_first`)
- `gateway/extensions/hall.py` — Hall messaging with auto-dispatch
- `gateway/extensions/cross_channel.py` — Cross-channel message injection
- `gateway/extensions/session_checkpoint.py` — Session continuity across restarts
- `gateway/extensions/channel_binding.py` — Channel-to-soul binding
- `scripts/channel_binding_healthcheck.py` — E2E binding verification (makes real API calls)
- `agent/auxiliary_client.py` — LLM client with Nous credential refresh fallback

## Testing

- Full suite: `pytest tests/ -x -q --tb=line`
- Pre-existing upstream failures: ~6 tests (baseline; new failures = regressions)
- Gateway healthcheck: `python3 scripts/channel_binding_healthcheck.py` (15 bindings)
