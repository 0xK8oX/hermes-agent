# Upstream Sync Guide

This guide prevents the #1 failure mode when syncing `feature/channel-binding-v3` with upstream `main`: **silently dropping upstream functions** during merge.

---

## Sync Strategy: Single Squashed Commit (Recommended)

We maintain **one squashed commit** on top of upstream. This keeps history clean and makes it easy to see our delta.

### When upstream has new commits:

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
# If 0 or different, re-add hook calls per the hook locations below

# 6. Stage and commit
git add -A
git commit -m "feat: channel binding hook system for NousResearch/hermes-agent

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"

# 7. Verify
python3 -c "import gateway.run; import gateway.extensions; print('Import OK')"
python3 scripts/audit_merge.py
```

### Hook Locations in gateway/run.py

If upstream modified `gateway/run.py`, you may need to re-add hook calls. The required locations:

| Hook | Purpose | Location Pattern |
|------|---------|------------------|
| `get_session_overrides` | Model override for session | After model resolution |
| `on_session_cleanup` | Cleanup extension state | Session expiry loop |
| `get_checkpoint` | Load session state | Before agent creation |
| `get_skills_override` | Skill list override | After checkpoint loading |
| `on_new_session` | Notify new session | After session key generation |
| `save_checkpoint` | Persist session state | After tool calls |
| `on_session_reset` | Notify session reset | After reset logic |

Use `fire_hooks_first` when you need the first non-None result (overrides).
Use `fire_hooks` when you just want to notify all extensions (no return value needed).

### Custom Command Dispatch

In addition to hooks, `gateway/run.py` contains direct command dispatch for custom
slash commands (e.g. `/bind`). These are **not** hook calls — they are `if canonical ==
"cmd"` blocks in the command dispatcher. After sync, verify that custom commands are
still wired:

```bash
grep -q 'canonical == "bind"' gateway/run.py || echo "MISSING: /bind dispatch"
```

If a custom command handler was extracted to `gateway/extensions/`, the dispatch
block in `run.py` must still import and call it. Missing dispatch wiring produces
"Unknown command" at runtime even though the handler function exists.

---

## Why Single Squashed Commit?

**Pros:**
- Clean `git log` - one line per sync cycle
- Easy to see what *we* added vs upstream
- Simple rollback if something breaks

**Cons:**
- All conflicts resolve at once (one big conflict block)
- Can't cherry-pick individual features

For a fork that syncs weekly, single commit is simpler than managing multiple commits.

---

## The Problem with `-X ours`

`git merge -X ours main` resolves all conflicts in favor of our branch. This sounds safe, but it also **discards upstream additions** to files we modified — functions added to upstream after our branch diverged simply vanish. We have hit this multiple times, losing:

- `_is_kimi_coding_endpoint` → runtime `NameError`
- `_iter_plugin_command_entries` → broken command registration
- `_wait_for_user_dbus_socket`, `_preflight_user_systemd`, `_plugin_image_gen_providers`, `_coerce_statusbar` → systemd and TUI failures

**Never** run `git merge -X ours origin/main` as a blanket strategy.

---

## Alternative: Standard Merge

If you prefer commit history over clean history:

```bash
git checkout feature/channel-binding-v3
git fetch origin
git merge origin/main
# Resolve conflicts in editor, keeping BOTH our hooks AND upstream functions
```

For files where our changes are purely additive (e.g. `gateway/extensions/*`), you can safely take ours:
```bash
git checkout --ours gateway/extensions/__init__.py
# Then verify no upstream additions were lost
git diff origin/main -- gateway/extensions/__init__.py
```

---

## Pre-Sync Checklist

### 1. Know what upstream changed
```bash
# How far behind are we?
git log --oneline origin/main..HEAD | wc -l

# Which files did upstream touch that we also touched?
BASE=$(git merge-base HEAD origin/main)
git diff --name-only $BASE origin/main > /tmp/upstream_files.txt
git diff --name-only $BASE HEAD > /tmp/our_files.txt
cat /tmp/upstream_files.txt /tmp/our_files.txt | sort | uniq -d
```

### 2. Identify upstream functions added to shared files
```bash
BASE=$(git merge-base HEAD origin/main)
git diff $BASE origin/main -- hermes_cli/gateway.py | grep '^+def '
git diff $BASE origin/main -- hermes_cli/tools_config.py | grep '^+def '
git diff $BASE origin/main -- tui_gateway/server.py | grep '^+def '
git diff $BASE origin/main -- agent/anthropic_adapter.py | grep '^+def '
git diff $BASE origin/main -- agent/auxiliary_client.py | grep '^+def '
```

### 3. Run baseline tests **before** syncing
```bash
pytest tests/ -x -q --tb=no 2>&1 | tail -5
python3 scripts/channel_binding_healthcheck.py
```

---

## Post-Sync Checklist

### 1. Smoke test imports
```bash
python3 -c "import gateway.run; import gateway.extensions; print('Import OK')"
```

### 2. Audit for lost functions
```bash
python3 scripts/audit_merge.py
```

Or manually:
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

### 3. Run tests
```bash
pytest tests/ -x -q --tb=line 2>&1 | tail -10
```

Compare with your pre-sync baseline. New failures = sync regression.

### 4. Run integration checks
```bash
python3 scripts/channel_binding_healthcheck.py
```

### 5. Restart and verify live gateway
```bash
hermes gateway restart
sleep 5
hermes gateway status
pgrep -fa "hermes gateway" | grep -v grep
```

---

## Quick Reference

| Command | Purpose |
|---------|---------|
| `git log --oneline origin/main..HEAD \| wc -l` | Commits behind upstream |
| `git diff origin/main..HEAD > /tmp/our_changes.patch` | Save our changes |
| `git reset --hard origin/main` | Reset to upstream |
| `git apply /tmp/our_changes.patch` | Apply our changes |
| `python3 scripts/audit_merge.py` | Automated lost-function check |
| `python3 scripts/channel_binding_healthcheck.py` | Live gateway validation |
