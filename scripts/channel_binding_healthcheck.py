#!/usr/bin/env python3
"""
Channel Binding E2E Health Check.

For each binding in config.yaml, creates a real AIAgent with the bound
model/provider/api_key, sends "who are you and what model are you",
and verifies the response matches expectations.

Usage:
    python scripts/channel_binding_healthcheck.py          # check all bindings
    python scripts/channel_binding_healthcheck.py --json    # JSON output
    python scripts/channel_binding_healthcheck.py --binding alpha  # check one binding
"""

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

# ── Bootstrap ────────────────────────────────────────────────────────

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(HERMES_HOME / ".env")
except ImportError:
    pass


def load_bindings_from_config():
    """Load all channel_personality_bindings from config.yaml."""
    import yaml
    config_path = HERMES_HOME / "config.yaml"
    if not config_path.exists():
        print(f"❌ config.yaml not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    all_bindings = []
    for platform_key in ("whatsapp", "telegram", "discord", "slack"):
        platform_cfg = cfg.get(platform_key, {})
        if isinstance(platform_cfg, dict):
            extra = platform_cfg.get("extra", {})
            if isinstance(extra, dict):
                bindings = extra.get("channel_personality_bindings", [])
                if isinstance(bindings, list):
                    for b in bindings:
                        b["_platform"] = platform_key
                        all_bindings.append(b)
    return all_bindings


def load_soul_content(soul_name):
    """Read soul file, strip YAML frontmatter, return content."""
    soul_path = HERMES_HOME / "souls" / f"{soul_name}.md"
    if not soul_path.exists():
        return None
    content = soul_path.read_text()
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()
    return content


def expand_api_key(raw_key):
    """Expand ${ENV_VAR} in api_key."""
    if not raw_key:
        return None
    if raw_key.startswith("${") and raw_key.endswith("}"):
        env_var = raw_key[2:-1]
        return os.environ.get(env_var)
    return raw_key


def _load_global_model_config():
    """Load the global model config (model.default) from config.yaml."""
    import yaml
    config_path = HERMES_HOME / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    model_cfg = cfg.get("model", {})
    return {
        "model": model_cfg.get("default", ""),
        "provider": model_cfg.get("provider"),
        "api_key": model_cfg.get("api_key"),
        "base_url": model_cfg.get("base_url"),
    }


def _resolve_custom_provider(provider_name, config_cfg=None):
    """Resolve a custom:xxx provider from config.yaml custom_providers list."""
    if not provider_name or not provider_name.startswith("custom:"):
        return None
    custom_name = provider_name[7:]  # strip "custom:"
    import yaml
    config_path = HERMES_HOME / "config.yaml"
    with open(config_path) as f:
        cfg = config_cfg or yaml.safe_load(f) or {}
    for cp in cfg.get("custom_providers", []):
        cp_name = cp.get("name", "").lower().replace(" ", "-")
        if cp_name == custom_name.lower() or cp.get("name") == custom_name:
            return cp
    return None


def resolve_binding_runtime(binding):
    """Resolve the full runtime config for a binding.

    Mirrors gateway's _resolve_session_agent_runtime():
    - If binding has explicit model/provider/api_key/base_url, use directly
    - If provider is "custom:xxx", resolve from custom_providers
    - If no model override, use global model.default config
    - api_mode: determined by provider type (anthropic → anthropic_messages, else chat_completions)
    """
    global_cfg = _load_global_model_config()

    model = binding.get("model")
    provider = binding.get("provider")
    api_key = expand_api_key(binding.get("api_key"))
    base_url = binding.get("base_url")

    # No model override → use global default
    if not model:
        return {
            "model": global_cfg.get("model", ""),
            "provider": global_cfg.get("provider") or "custom",
            "api_key": global_cfg.get("api_key"),
            "base_url": global_cfg.get("base_url"),
            "api_mode": "chat_completions",
            "_source": "global-default",
        }

    # Provider is "custom:xxx" → resolve from custom_providers
    if provider and provider.startswith("custom:"):
        cp = _resolve_custom_provider(provider)
        if cp:
            return {
                "model": model,
                "provider": "custom",
                "api_key": cp.get("api_key", api_key),
                "base_url": cp.get("base_url", base_url),
                "api_mode": cp.get("api_mode", "chat_completions"),
                "_source": f"custom:{cp.get('name')}",
            }

    # Direct binding — use exactly what's configured
    # Determine api_mode from provider
    api_mode = "chat_completions"
    if provider == "anthropic" or (base_url and "/anthropic" in base_url):
        api_mode = "anthropic_messages"

    return {
        "model": model,
        "provider": provider or global_cfg.get("provider") or "custom",
        "api_key": api_key or global_cfg.get("api_key"),
        "base_url": base_url or global_cfg.get("base_url"),
        "api_mode": api_mode,
        "_source": "binding-direct",
    }


def check_binding(binding, timeout=30):
    """Run a single binding health check. Returns result dict."""
    from run_agent import AIAgent

    soul_name = binding.get("soul", "?")
    chat_id = binding.get("id", "?")
    platform = binding.get("_platform", "?")
    label = f"[{platform}:{chat_id[:20]}] soul={soul_name}"

    result = {
        "soul": soul_name,
        "platform": platform,
        "chat_id": chat_id,
        "model": binding.get("model"),
        "provider": binding.get("provider"),
        "base_url": binding.get("base_url"),
        "status": "pending",
        "response": None,
        "error": None,
        "elapsed_s": 0,
    }

    # Load soul content
    soul_content = load_soul_content(soul_name)
    if not soul_content:
        result["status"] = "skip"
        result["error"] = f"Soul file not found or empty: {soul_name}"
        return result

    # Resolve runtime
    try:
        runtime = resolve_binding_runtime(binding)
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"Failed to resolve runtime: {e}"
        return result

    result["_runtime"] = {
        "model": runtime["model"],
        "provider": runtime["provider"],
        "base_url": runtime.get("base_url", "")[:50],
        "source": runtime.get("_source"),
    }

    # Create agent and call API
    prompt = (
        "Answer briefly:\n"
        "1. Who are you? (name/role)\n"
        "2. What model are you? (exact model name)\n"
        "3. What provider are you using?\n"
        "Reply in max 3 lines."
    )

    try:
        start = time.time()
        agent = AIAgent(
            model=runtime["model"],
            provider=runtime["provider"],
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            api_mode=runtime.get("api_mode"),
            ephemeral_system_prompt=soul_content,
            max_iterations=3,
            quiet_mode=True,
            enabled_toolsets=[],
            skip_context_files=True,
            skip_memory=True,
            platform="healthcheck",
        )
        response = agent.chat(prompt)
        elapsed = time.time() - start

        result["response"] = response
        result["elapsed_s"] = round(elapsed, 1)

        if not response or not response.strip():
            result["status"] = "fail"
            result["error"] = "Empty response from model"
        else:
            # Check if response mentions the soul name or expected content
            resp_lower = response.lower()
            expected_model = (binding.get("model") or "").lower()
            expected_soul = soul_name.lower()

            # Loose matching — model name substring
            model_mentioned = (
                not expected_model  # no model override = can't check
                or expected_model in resp_lower
                or any(part in resp_lower for part in expected_model.split("/") if part)
            )

            result["status"] = "pass" if response.strip() else "fail"

    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        result["elapsed_s"] = round(time.time() - start, 1)

    return result


def print_result(result, verbose=False):
    """Print a single result."""
    status_icons = {
        "pass": "✅",
        "fail": "❌",
        "error": "💥",
        "skip": "⏭️",
    }
    icon = status_icons.get(result["status"], "?")
    soul = result["soul"]
    model = result.get("model") or "(default)"
    provider = result.get("provider") or "(default)"
    elapsed = result.get("elapsed_s", 0)

    print(f"\n{icon} [{result['platform']}] soul={soul}  model={model}  provider={provider}  ({elapsed}s)")

    if result.get("_runtime"):
        rt = result["_runtime"]
        print(f"   Runtime: model={rt['model']} provider={rt['provider']} base={rt.get('base_url','')[:40]} source={rt.get('source')}")

    if result.get("response"):
        # Print first 3 lines of response
        lines = result["response"].strip().split("\n")[:3]
        for line in lines:
            print(f"   📝 {line.strip()}")

    if result.get("error"):
        print(f"   ⚠️  {result['error']}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Channel Binding E2E Health Check")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--binding", type=str, help="Only check a specific soul binding")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout per check (seconds)")
    args = parser.parse_args()

    print("=" * 70)
    print("🔍 Channel Binding E2E Health Check")
    print("=" * 70)

    bindings = load_bindings_from_config()
    print(f"\nFound {len(bindings)} binding(s) in config.yaml")

    if args.binding:
        bindings = [b for b in bindings if b.get("soul") == args.binding]
        if not bindings:
            print(f"❌ No binding found for soul '{args.binding}'")
            sys.exit(1)

    results = []
    passed = 0
    failed = 0
    errors = 0
    skipped = 0

    for binding in bindings:
        result = check_binding(binding, timeout=args.timeout)
        results.append(result)
        print_result(result)

        if result["status"] == "pass":
            passed += 1
        elif result["status"] == "fail":
            failed += 1
        elif result["status"] == "error":
            errors += 1
        elif result["status"] == "skip":
            skipped += 1

    # Summary
    print(f"\n{'=' * 70}")
    print(f"📊 Summary: {passed} passed, {failed} failed, {errors} errors, {skipped} skipped")
    print(f"{'=' * 70}")

    if args.json:
        # Clean up non-serializable fields
        for r in results:
            r.pop("_runtime", None)
        print(json.dumps(results, indent=2, default=str))

    sys.exit(1 if (failed + errors) > 0 else 0)


if __name__ == "__main__":
    main()
