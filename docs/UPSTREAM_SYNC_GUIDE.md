# Upstream Sync Guide

This guide prevents the #1 failure mode when syncing `feature/channel-binding-hook-system` with upstream `main`: **silently dropping upstream functions** during merge.

---

## The Problem

`git merge -X ours main` resolves all conflicts in favor of our branch. This sounds safe, but it also **discards upstream additions** to files we modified — functions added to upstream after our branch diverged simply vanish. We have hit this three times, losing:

- `_is_kimi_coding_endpoint` → runtime `NameError`
- `_iter_plugin_command_entries` → broken command registration
- `_wait_for_user_dbus_socket`, `_preflight_user_systemd`, `_plugin_image_gen_providers`, `_coerce_statusbar` → systemd and TUI failures

---

## Pre-Merge Checklist

### 1. Know what upstream changed
```bash
# How far behind are we?
git log --oneline feature/channel-binding-hook-system..origin/main | wc -l

# Which files did upstream touch that we also touched?
git merge-base feature/channel-binding-hook-system origin/main
# ^ save the SHA, e.g. abc1234

git diff --name-only abc1234 origin/main > /tmp/upstream_files.txt
git diff --name-only abc1234 feature/channel-binding-hook-system > /tmp/our_files.txt

# The intersection is where we risk losing code
cat /tmp/upstream_files.txt /tmp/our_files.txt | sort | uniq -d
```

### 2. Identify upstream functions added to shared files
```bash
# For each file in the intersection, list functions upstream added
git diff abc1234 origin/main -- hermes_cli/gateway.py | grep '^+def '
git diff abc1234 origin/main -- hermes_cli/tools_config.py | grep '^+def '
git diff abc1234 origin/main -- tui_gateway/server.py | grep '^+def '
git diff abc1234 origin/main -- agent/anthropic_adapter.py | grep '^+def '
git diff abc1234 origin/main -- agent/auxiliary_client.py | grep '^+def '
```

Save this list. These are the functions most likely to disappear.

### 3. Run baseline tests **before** merging
```bash
pytest tests/ -x -q --tb=no 2>&1 | tail -5
python3 scripts/channel_binding_healthcheck.py
```

If tests are already failing, note which ones so you can distinguish pre-existing failures from merge-induced regressions.

---

## Merge Strategy (Do Not Use `-X ours`)

### Option A: Standard merge + manual conflict resolution (Recommended)
```bash
git checkout feature/channel-binding-hook-system
git fetch origin
git merge origin/main
# Resolve conflicts in your editor, keeping BOTH our hooks AND upstream functions
```

### Option B: If conflicts are overwhelming — ours-only per file
```bash
# Resolve most files normally
git merge origin/main

# For files where our changes are purely additive (e.g. gateway/extensions/*),
# you can safely take ours, but VERIFY the file afterwards:
git checkout --ours gateway/extensions/__init__.py
# Then manually inspect that no upstream additions were lost
git diff origin/main -- gateway/extensions/__init__.py
```

### Option C: Rebase (cleaner history, harder)
```bash
git checkout feature/channel-binding-hook-system
git rebase origin/main
# Fix conflicts commit by commit
```

**Never** run `git merge -X ours origin/main` as a blanket strategy. It is the root cause of every lost-function incident.

---

## Post-Merge Checklist

### 1. Audit for lost functions
```bash
# Run the same command from pre-merge step 2
git diff MERGE_BASE origin/main -- hermes_cli/gateway.py | grep '^+def ' > /tmp/upstream_funcs.txt

# Now check which ones exist in our merged branch
cat /tmp/upstream_funcs.txt | while read line; do
    func=$(echo "$line" | sed 's/^+def //' | awk '{print $1}' | tr -d ':')
    file=$(echo "$line" | grep -o '[-a-z_]*\.py' | head -1)
    if ! grep -q "def $func" "$file" 2>/dev/null; then
        echo "MISSING: $func in $file"
    fi
done
```

### 2. Run the smoke test for undefined references
```bash
# Fast syntax/import check for the files most often broken
python3 -c "import hermes_cli.gateway"
python3 -c "import hermes_cli.tools_config"
python3 -c "import tui_gateway.server"
python3 -c "import agent.anthropic_adapter"
python3 -c "import agent.auxiliary_client"
```

### 3. Run tests
```bash
pytest tests/ -x -q --tb=line 2>&1 | tail -10
```

Compare with your pre-merge baseline. New failures = merge regression.

### 4. Run integration checks
```bash
python3 scripts/channel_binding_healthcheck.py
```

### 5. Restart and verify live gateway
```bash
hermes gateway restart
sleep 5
hermes gateway status
# Check for zombies
pgrep -fa "hermes gateway" | grep -v grep
```

---

## Automation: The `audit_merge.py` Script

Save this as `scripts/audit_merge.py` and run it after every merge:

```python
#!/usr/bin/env python3
"""Post-merge audit: detect upstream functions lost during merge."""

import subprocess
import sys

SHARED_FILES = [
    "hermes_cli/gateway.py",
    "hermes_cli/tools_config.py",
    "tui_gateway/server.py",
    "agent/anthropic_adapter.py",
    "agent/auxiliary_client.py",
    "agent/gemini_native_adapter.py",
    "agent/gemini_cloudcode_adapter.py",
]


def get_merge_base() -> str:
    return subprocess.check_output(
        ["git", "merge-base", "HEAD", "origin/main"],
        text=True,
    ).strip()


def upstream_functions(merge_base: str, path: str) -> set[str]:
    diff = subprocess.run(
        ["git", "diff", merge_base, "origin/main", "--", path],
        capture_output=True,
        text=True,
    )
    funcs = set()
    for line in diff.stdout.splitlines():
        if line.startswith("+def "):
            name = line[5:].split("(")[0].split(":")[0].strip()
            funcs.add(name)
    return funcs


def our_functions(path: str) -> set[str]:
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return set()
    funcs = set()
    for line in text.splitlines():
        if line.startswith("def "):
            name = line[4:].split("(")[0].split(":")[0].strip()
            funcs.add(name)
    return funcs


def main() -> int:
    merge_base = get_merge_base()
    print(f"Merge base with origin/main: {merge_base[:12]}")
    missing = []
    for path in SHARED_FILES:
        upstream = upstream_functions(merge_base, path)
        ours = our_functions(path)
        lost = upstream - ours
        if lost:
            missing.extend((path, f) for f in lost)
            for f in lost:
                print(f"  MISSING: {f} in {path}")
    if missing:
        print(f"\nFAIL: {len(missing)} upstream function(s) missing after merge.")
        return 1
    print("\nPASS: No upstream functions lost.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run after every merge:
```bash
python3 scripts/audit_merge.py
```

---

## Quick Reference

| Command | Purpose |
|---------|---------|
| `git log --oneline ..origin/main \| wc -l` | Commits behind upstream |
| `git merge-base HEAD origin/main` | Find common ancestor |
| `git diff MERGE_BASE origin/main -- file.py \| grep '^+def '` | Functions upstream added |
| `python3 scripts/audit_merge.py` | Automated lost-function check |
| `python3 scripts/channel_binding_healthcheck.py` | Live gateway validation |
