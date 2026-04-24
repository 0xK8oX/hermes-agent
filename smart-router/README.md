# Smart Router

Cloudflare Worker that acts as a smart proxy / circuit breaker for LLM providers.

## Features

- **Multi-format**: Accepts both OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) requests
- **Smart routing**: Routes to healthy providers based on configurable plans
- **Circuit breaker**: Temporarily disables providers on repeated failures or quota exhaustion
- **Streaming**: Real-time SSE translation between formats
- **Tool calling**: Full bidirectional tool schema + tool_call translation

## Architecture

```
Client (OpenAI or Anthropic format)
  → Cloudflare Worker
    → HealthTracker Durable Object (circuit breaker state)
    → Try provider #1
      → Translate request to provider format
      → Call upstream
      → Translate response back
    → If fails: report to DO, try provider #2...
```

## Setup

### 1. Install dependencies

```bash
cd smart-router
npm install
```

### 2. Configure providers

Edit `plans.json` to define your provider pools per plan:

```json
{
  "plans": {
    "default": {
      "providers": [
        {"name": "volcengine", "base_url": "...", "model": "...", "format": "openai"},
        {"name": "kimi", "base_url": "...", "model": "...", "format": "anthropic"}
      ]
    }
  }
}
```

### 3. Set API keys as Wrangler secrets

```bash
npx wrangler secret put PROVIDER_KEY_VOLCENGINE
npx wrangler secret put PROVIDER_KEY_KIMI
npx wrangler secret put PROVIDER_KEY_MINIMAX
npx wrangler secret put PROVIDER_KEY_OPENROUTER
npx wrangler secret put PROVIDER_KEY_OPEN_BIGMODEL_CN
npx wrangler secret put PROVIDER_KEY_ORFREE
```

Secret name pattern: `PROVIDER_KEY_<UPPERCASE_PROVIDER_NAME>`.

### 4. Deploy

```bash
npx wrangler login   # one-time auth
npx wrangler deploy
```

### 5. Update Hermes config

Point your model at the Worker:

```yaml
model:
  base_url: https://<your-worker>.workers.dev/v1
  provider: openai
  api_key: dummy          # Worker ignores this; keys are in Wrangler secrets
```

Remove or simplify `fallback_providers` since the Worker handles fallback:

```yaml
fallback_providers:
  default: []
  compression: []
  summary: []
```

## API

### `POST /v1/chat/completions`

OpenAI-compatible endpoint. Set `X-Plan: default` header to select provider pool.

### `POST /v1/messages`

Anthropic-compatible endpoint. Set `X-Plan: default` header.

### `GET /v1/health?plan=default`

Returns current circuit breaker state for all providers in a plan.

## Circuit Breaker Rules

| Failure | Threshold | Cooldown |
|---------|-----------|----------|
| Quota exhausted | 1 | 5 hours |
| Rate limit (429) | 3 | 5 min |
| Server error (5xx) | 2 | 2 min |
| Timeout | 2 | 2 min |
| Connection refused | 2 | 1 min |

A single success resets a provider to healthy.

## Local Development

```bash
npx wrangler dev
```

Test:

```bash
curl -X POST http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Plan: default" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hi"}]}'
```
