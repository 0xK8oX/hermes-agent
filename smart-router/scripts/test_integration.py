#!/usr/bin/env python3
"""
Smart Router - Integration Tests

Tests plan CRUD, routing with different plans, and fallback behavior.
Run against a local wrangler dev server (default: http://localhost:8790).
"""

import json
import sys
import time
import urllib.request
import urllib.error
from typing import Any

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8790"

PASSED = 0
FAILED = 0


def req(
    method: str,
    path: str,
    body: Any = None,
    headers: dict | None = None,
    expect_status: int | None = None,
) -> tuple[int, Any]:
    """Make HTTP request and return (status, parsed_json)."""
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req_obj = urllib.request.Request(url, data=data, method=method)
    req_obj.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req_obj.add_header(k, v)
    try:
        with urllib.request.urlopen(req_obj, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            return status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return e.code, parsed
    except urllib.error.URLError as e:
        return 0, str(e.reason)


def assert_eq(name: str, actual: Any, expected: Any) -> None:
    global PASSED, FAILED
    if actual == expected:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool) -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}")


# =============================================================================
# 1. PLAN LOADING
# =============================================================================
print("\n=== 1. Plan Loading ===")

status, plans = req("GET", "/v1/plans")
assert_eq("list plans status", status, 200)
assert_true("list plans has default", "default" in (plans or {}))
assert_true("list plans has coding", "coding" in (plans or {}))
assert_true("list plans has compression", "compression" in (plans or {}))
assert_true("list plans has summary", "summary" in (plans or {}))

status, default_plan = req("GET", "/v1/plans/default")
assert_eq("get default plan status", status, 200)
assert_true("default plan has providers", isinstance(default_plan, dict) and "providers" in default_plan)
assert_true("default plan has providers array", isinstance(default_plan.get("providers"), list))

status, not_found = req("GET", "/v1/plans/nonexistent-plan-xyz")
assert_eq("get nonexistent plan status", status, 404)

# =============================================================================
# 2. PLAN CRUD
# =============================================================================
print("\n=== 2. Plan CRUD ===")

test_slug = f"auto-test-{int(time.time())}"
new_plan = {
    "providers": [
        {"name": "orfree", "base_url": "http://localhost:23000/v1", "model": "auto", "format": "anthropic", "timeout": 30}
    ]
}

status, created = req("POST", "/v1/plans", body={"slug": test_slug, "config": new_plan})
assert_eq("create plan status", status, 200)
assert_eq("create plan ok", created.get("ok"), True)
assert_eq("create plan slug", created.get("slug"), test_slug)

status, fetched = req("GET", f"/v1/plans/{test_slug}")
assert_eq("get created plan status", status, 200)
assert_eq("get created plan providers count", len(fetched.get("providers", [])), 1)
assert_eq("get created plan provider name", fetched["providers"][0]["name"], "orfree")

updated_plan = {
    "providers": [
        {"name": "orfree", "base_url": "http://localhost:23000/v1", "model": "auto", "format": "anthropic", "timeout": 45},
        {"name": "volcengine", "base_url": "https://ark.cn-beijing.volces.com/api/coding", "model": "ark-code-latest", "format": "anthropic", "timeout": 60}
    ]
}
status, updated = req("PUT", f"/v1/plans/{test_slug}", body=updated_plan)
assert_eq("update plan status", status, 200)
assert_eq("update plan ok", updated.get("ok"), True)

status, fetched2 = req("GET", f"/v1/plans/{test_slug}")
assert_eq("get updated plan provider count", len(fetched2.get("providers", [])), 2)
assert_eq("get updated plan timeout", fetched2["providers"][0]["timeout"], 45)

status, deleted = req("DELETE", f"/v1/plans/{test_slug}")
assert_eq("delete plan status", status, 200)

status, gone = req("GET", f"/v1/plans/{test_slug}")
assert_eq("get deleted plan status", status, 404)

# =============================================================================
# 3. DIFFERENT PLAN ROUTING
# =============================================================================
print("\n=== 3. Different Plan Routing ===")

chat_body = {"model": "auto", "messages": [{"role": "user", "content": "Say hi in 2 words"}], "stream": False}

status, coding_resp = req("POST", "/v1/chat/completions", body=chat_body, headers={"X-Plan": "coding"})
assert_eq("coding plan chat status", status, 200)
assert_true("coding plan has choices", isinstance(coding_resp, dict) and "choices" in coding_resp)

status, default_resp = req("POST", "/v1/chat/completions", body=chat_body, headers={"X-Plan": "default"})
assert_eq("default plan chat status", status, 200)
assert_true("default plan has choices", isinstance(default_resp, dict) and "choices" in default_resp)

# Verify different plans route to different providers (model should differ or provider field)
# We can't strictly compare models since orfree returns dynamic models, but we can verify both succeed
assert_true("coding and default both succeed", status == 200)

# =============================================================================
# 4. FALLBACK BEHAVIOR
# =============================================================================
print("\n=== 4. Fallback Behavior ===")

# 4a. Single bad provider followed by good provider
fallback_slug = f"fallback-test-{int(time.time())}"
fallback_plan = {
    "providers": [
        # First provider: unreachable → will fail with connection error
        {"name": "orfree", "base_url": "http://localhost:1", "model": "auto", "format": "anthropic", "timeout": 5},
        # Second provider: working → should succeed on fallback
        {"name": "orfree-real", "base_url": "http://localhost:23000/v1", "model": "auto", "format": "anthropic", "timeout": 30}
    ]
}

# Note: orfree-real won't have an API key (PROVIDER_KEY_ORFREE_REAL doesn't exist)
# So this will fail with "Missing API key". Let's use an existing provider name with a bad URL
# and another existing provider with a good URL.
# Actually, "orfree" is the only one pointing to localhost:23000 which we know works.
# Let's use "volcengine" with a bad URL (no key needed since key is in env) and "orfree" as fallback.
# Wait, volcengine key exists in env. If base_url is bad, it'll connection-fail.

fallback_plan = {
    "providers": [
        {"name": "volcengine", "base_url": "http://localhost:1", "model": "auto", "format": "anthropic", "timeout": 5},
        {"name": "orfree", "base_url": "http://localhost:23000/v1", "model": "auto", "format": "anthropic", "timeout": 30}
    ]
}

req("POST", "/v1/plans", body={"slug": fallback_slug, "config": fallback_plan})

status, fb_resp = req("POST", "/v1/chat/completions", body=chat_body, headers={"X-Plan": fallback_slug})
assert_eq("fallback to good provider status", status, 200)
assert_true("fallback response has choices", isinstance(fb_resp, dict) and "choices" in fb_resp)

# 4b. All providers bad → should return 503
all_bad_slug = f"all-bad-{int(time.time())}"
all_bad_plan = {
    "providers": [
        {"name": "volcengine", "base_url": "http://localhost:1", "model": "auto", "format": "anthropic", "timeout": 5},
        {"name": "kimi", "base_url": "http://localhost:1", "model": "auto", "format": "anthropic", "timeout": 5}
    ]
}
req("POST", "/v1/plans", body={"slug": all_bad_slug, "config": all_bad_plan})

status, all_bad_resp = req("POST", "/v1/chat/completions", body=chat_body, headers={"X-Plan": all_bad_slug})
assert_eq("all bad providers status", status, 503)
assert_true("all bad providers has error", isinstance(all_bad_resp, dict) and "error" in all_bad_resp)
assert_true("all bad providers has details", isinstance(all_bad_resp.get("details"), list))
assert_eq("all bad providers detail count", len(all_bad_resp.get("details", [])), 2)

# =============================================================================
# 5. CIRCUIT BREAKER
# =============================================================================
print("\n=== 5. Circuit Breaker ===")

cb_slug = f"cb-test-{int(time.time())}"
cb_plan = {
    "providers": [
        {"name": "volcengine", "base_url": "http://localhost:1", "model": "auto", "format": "anthropic", "timeout": 5}
    ]
}
req("POST", "/v1/plans", body={"slug": cb_slug, "config": cb_plan})

# Send 3 requests to trigger circuit breaker (unknown failure threshold = 3)
for i in range(3):
    req("POST", "/v1/chat/completions", body=chat_body, headers={"X-Plan": cb_slug})

# Check health state
time.sleep(0.5)  # small delay for DO to persist
status, health = req("GET", f"/v1/health?plan={cb_slug}")
assert_eq("health check status", status, 200)

providers_health = health.get("providers", {})
assert_true("health has volcengine entry", "volcengine" in providers_health)

volc_health = providers_health.get("volcengine", {})
# Connection errors threshold is 2 (not 3) per CIRCUIT_RULES
assert_eq("volcengine consecutive failures", volc_health.get("consecutiveFailures"), 2)
assert_eq("volcengine status", volc_health.get("status"), "unhealthy")
assert_true("volcengine cooldownUntil is in future", volc_health.get("cooldownUntil", 0) > int(time.time() * 1000))

# 4th request: provider should be skipped (no healthy providers)
status, skipped = req("POST", "/v1/chat/completions", body=chat_body, headers={"X-Plan": cb_slug})
assert_eq("skipped unhealthy provider status", status, 503)
assert_true("skipped response mentions all providers failed", isinstance(skipped, dict) and "error" in skipped)

# =============================================================================
# CLEANUP
# =============================================================================
print("\n=== Cleanup ===")
req("DELETE", f"/v1/plans/{fallback_slug}")
req("DELETE", f"/v1/plans/{all_bad_slug}")
req("DELETE", f"/v1/plans/{cb_slug}")
print(f"Deleted test plans: {fallback_slug}, {all_bad_slug}, {cb_slug}")

# =============================================================================
# SUMMARY
# =============================================================================
print(f"\n{'='*50}")
print(f"Results: {PASSED} passed, {FAILED} failed")
print(f"{'='*50}")

if FAILED > 0:
    sys.exit(1)
