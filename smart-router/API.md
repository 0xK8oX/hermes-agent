# Smart Router API Documentation

Base URL: `https://<your-worker>.workers.dev` (local: `http://localhost:8790`)

---

## Chat Completion

### OpenAI Format

```
POST /v1/chat/completions
Content-Type: application/json
X-Plan: <plan-slug>        # optional, defaults to "default"
```

**Request body:**
```json
{
  "model": "auto",
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "stream": false,
  "tools": [],
  "tool_choice": "auto"
}
```

**Model behavior:**
- `"model": "auto"` — uses the provider's configured model from the plan
- `"model": "glm-5.1"` — overrides with the specified model

**Response (non-streaming):**
```json
{
  "id": "msg_...",
  "object": "chat.completion",
  "model": "glm-5.1",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "Hi there!"
      }
    }
  ]
}
```

**Response (streaming):**
```
data: {"choices":[{"delta":{"content":"Hi"}}]}
data: {"choices":[{"delta":{"content":" there"}}]}
data: [DONE]
```

### Anthropic Format

```
POST /v1/messages
Content-Type: application/json
X-Plan: <plan-slug>
```

**Request body:**
```json
{
  "model": "auto",
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "stream": false,
  "max_tokens": 4096
}
```

**Response:** Anthropic Messages API format (translated from the backend provider).

---

## Plan Management

### List All Plans

```
GET /v1/plans
```

**Response:**
```json
{
  "kato": {
    "providers": [
      {"name": "kato-glm", "base_url": "...", "model": "glm-5.1", "format": "anthropic", "timeout": 60},
      {"name": "kato-kimi", "base_url": "...", "model": "kimi-for-coding", "format": "anthropic", "timeout": 60}
    ]
  },
  "sam": { ... },
  "jason": { ... }
}
```

### Get Single Plan

```
GET /v1/plans/:slug
```

**Example:** `GET /v1/plans/kato`

**Response (200):**
```json
{
  "providers": [
    {"name": "kato-glm", "base_url": "...", "model": "glm-5.1", "format": "anthropic", "timeout": 60},
    {"name": "kato-kimi", "base_url": "...", "model": "kimi-for-coding", "format": "anthropic", "timeout": 60}
  ]
}
```

**Response (404):**
```json
{"error": "Plan not found"}
```

### Create a New Plan

```
POST /v1/plans
Content-Type: application/json
```

**Request body:**
```json
{
  "slug": "auto-kato",
  "config": {
    "providers": [
      {
        "name": "kato-glm",
        "base_url": "https://open.bigmodel.cn/api/anthropic/v4",
        "model": "glm-5.1",
        "format": "anthropic",
        "timeout": 60
      },
      {
        "name": "kato-kimi",
        "base_url": "https://api.kimi.com/coding/",
        "model": "kimi-for-coding",
        "format": "anthropic",
        "timeout": 60
      }
    ]
  }
}
```

**Response (200):**
```json
{"ok": true, "slug": "auto-kato"}
```

### Update a Plan

```
PUT /v1/plans/:slug
Content-Type: application/json
```

**Request body:** (same shape as `config` above)
```json
{
  "providers": [
    {"name": "kato-kimi", "base_url": "...", "model": "kimi-for-coding", "format": "anthropic", "timeout": 60},
    {"name": "kato-glm", "base_url": "...", "model": "glm-5.1", "format": "anthropic", "timeout": 60}
  ]
}
```

Replaces the entire provider list. Use this to reorder providers.

**Response (200):**
```json
{"ok": true, "slug": "auto-kato"}
```

### Delete a Plan

```
DELETE /v1/plans/:slug
```

**Response (200):**
```json
{"ok": true, "slug": "auto-kato"}
```

---

## Health Status

```
GET /v1/health?plan=<slug>
```

**Example:** `GET /v1/health?plan=kato`

**Response:**
```json
{
  "providers": {
    "kato-glm": {
      "status": "healthy",
      "consecutiveFailures": 0,
      "lastFailureAt": 0,
      "cooldownUntil": 0,
      "lastFailureReason": ""
    },
    "kato-kimi": {
      "status": "unhealthy",
      "consecutiveFailures": 3,
      "lastFailureAt": 1777090000000,
      "cooldownUntil": 1777090600000,
      "lastFailureReason": "rate_limit"
    }
  }
}
```

---

## Key Management

API keys are stored **encrypted in D1** using AES-256-GCM. The master encryption key (`KEY_ENCRYPTION_KEY`) is the only secret kept in Wrangler secrets.

### List Keys (names only)

```
GET /v1/keys
```

**Response:**
```json
{
  "keys": ["kato-glm", "kato-kimi", "volcengine", "minimax"]
}
```

### Store a Key

```
POST /v1/keys
Content-Type: application/json
```

**Request body:**
```json
{
  "provider_name": "kato-glm",
  "api_key": "sk-..."
}
```

**Response (200):**
```json
{"ok": true, "provider_name": "kato-glm"}
```

### Delete a Key

```
DELETE /v1/keys/:provider_name
```

**Response (200):**
```json
{"ok": true, "provider_name": "kato-glm"}
```

---

## Error Responses

### Plan Not Found
```json
{"error": "Plan \"xyz\" not found or empty"}
```

### All Providers Failed
```json
{
  "error": "All providers failed",
  "details": [
    {"provider": "kato-glm", "status": 401, "message": "Invalid API key"},
    {"provider": "kato-kimi", "status": 429, "message": "Rate limited"}
  ]
}
```

### All Providers in Cooldown
```json
{"error": "All providers in cooldown"}
```

---

## Adding a New Plan (Step-by-Step)

### 1. Create the plan via API

```bash
curl -X POST http://localhost:8790/v1/plans \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "auto-kato",
    "config": {
      "providers": [
        {"name": "kato-glm", "base_url": "https://open.bigmodel.cn/api/anthropic/v4", "model": "glm-5.1", "format": "anthropic", "timeout": 60},
        {"name": "kato-kimi", "base_url": "https://api.kimi.com/coding/", "model": "kimi-for-coding", "format": "anthropic", "timeout": 60}
      ]
    }
  }'
```

### 2. Store the encrypted API keys

```bash
curl -X POST http://localhost:8790/v1/keys \
  -H "Content-Type: application/json" \
  -d '{"provider_name": "kato-glm", "api_key": "sk-..."}'

curl -X POST http://localhost:8790/v1/keys \
  -H "Content-Type: application/json" \
  -d '{"provider_name": "kato-kimi", "api_key": "sk-..."}'
```

Keys are encrypted with AES-256-GCM before storage. Even if the D1 database leaks, keys remain unusable without the master `KEY_ENCRYPTION_KEY` (stored in Wrangler secrets).

### 3. Test it

```bash
curl -X POST http://localhost:8790/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Plan: auto-kato" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'
```

---

## Hermes Agent Integration

In `config.yaml`, point the model base_url to the router:

```yaml
model:
  base_url: "http://localhost:8790/v1"
  model: "auto"
  
  headers:
    X-Plan: "kato"
```

Or for a specific task (compression, summary, coding):

```yaml
compression:
  base_url: "http://localhost:8790/v1"
  model: "auto"
  headers:
    X-Plan: "compression"
```

The router handles:
- Format translation (OpenAI ↔ Anthropic)
- Provider fallback (tries next provider on failure)
- Circuit breaker (skips unhealthy providers)
- Model override (`auto` = plan default, specific name = pass through)
