# Claude Code Project Instructions

## Upstream Sync Rules (Critical)

This project is a fork of NousResearch/hermes-agent. We maintain a feature branch `feature/channel-binding-v3` that must stay sync'd with upstream `main`. The #1 failure mode is **silently dropping upstream functions during merge**.

### NEVER Use `git merge -X ours origin/main`

`-X ours` resolves all conflicts in favor of our branch, which also **discards upstream additions** to files we modified. Functions added to upstream after our branch diverged simply vanish. Past incidents:

- `_is_kimi_coding_endpoint` → runtime `NameError`
- `_iter_plugin_command_entries` → broken command registration
- `_wait_for_user_dbus_socket`, `_preflight_user_systemd` → systemd failures
- `_plugin_image_gen_providers`, `_coerce_statusbar` → TUI failures

### Sync Strategy: Single Squashed Commit

We maintain **one squashed commit** on top of upstream. This keeps history clean and makes it easy to see our delta.

#### When upstream has new commits (Weekly/Sync):

```bash
# 1. Fetch latest upstream
git fetch origin

# 2. Save our changes as patch (use merge-base so upstream additions are not reverted)
BASE=$(git merge-base origin/main HEAD)
git diff "$BASE"..HEAD > /tmp/our_changes.patch

# 3. Reset to fresh upstream
git reset --hard origin/main

# 4. Apply our changes
git apply /tmp/our_changes.patch

# 5. Check if gateway/run.py changed (may need to re-add hooks)
grep -c "fire_hooks" gateway/run.py
# If 0 or different, re-add hook calls per the hook locations in UPSTREAM_SYNC_GUIDE.md

# 6. Squash into one commit
git add -A
git commit -m "feat: channel binding hook system for NousResearch/hermes-agent

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"

# 7. Verify
python3 -c "import gateway.run; import gateway.extensions; print('Import OK')"
```

#### Pre-Sync Checklist

1. **Count commits behind:** `git log --oneline origin/main..HEAD | wc -l`
2. **Run baseline tests:** `pytest tests/ -x -q --tb=no 2>&1 | tail -5`
3. **Run baseline healthcheck:** `python3 scripts/channel_binding_healthcheck.py`

#### Post-Sync Checklist

1. **Smoke test imports:**
   ```bash
   python3 -c "import gateway.run; import gateway.extensions"
   ```
2. **Audit for lost functions:** `python3 scripts/audit_merge.py`
3. **Run tests:** `pytest tests/ -x -q --tb=line 2>&1 | tail -10`
4. **Run healthcheck:** `python3 scripts/channel_binding_healthcheck.py`
5. **Restart gateway:** `hermes gateway restart && sleep 5 && hermes gateway status`
6. **Check for zombies:** `pgrep -fa "hermes gateway" | grep -v grep`

### Alternative: Standard Merge (If you prefer commit history)

```bash
git checkout feature/channel-binding-v3
git fetch origin
git merge origin/main
# Resolve conflicts in editor, keeping BOTH our hooks AND upstream functions
```

If a file has only additive changes (e.g. `gateway/extensions/__init__.py`), you can `git checkout --ours` for that file, but **always verify** with `git diff origin/main -- <file>` afterwards.

## Project Context

- **Primary branch:** `feature/channel-binding-v3`
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
- `gateway/run.py` — 8 hook integration points (15 hook calls total)
- `scripts/channel_binding_healthcheck.py` — E2E binding verification (makes real API calls)
- `agent/auxiliary_client.py` — LLM client with Nous credential refresh fallback

## Testing

- Full suite: `pytest tests/ -x -q --tb=line`
- Pre-existing upstream failures: ~8 tests (baseline; new failures = regressions)
- Gateway healthcheck: `python3 scripts/channel_binding_healthcheck.py` (15 bindings)
- Audit script: `python3 scripts/audit_merge.py`
