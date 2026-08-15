# Qwen Cloud FinOps

NanoDelta places an authenticated OpenAI-compatible gateway in front of Alibaba Cloud Model
Studio/Qwen. TradingAgents and other LLM callers use NanoDelta's `/v1/chat/completions` endpoint
instead of receiving the Alibaba API key.

## Billing modes

### Subscription

Use `SUBSCRIPTION` for an Alibaba Cloud Coding Plan or another fixed-fee plan. Token counts remain
operational telemetry, but marginal token cost is recorded as zero. The configured monthly fee is
shown separately. The guard enforces configured 5-hour, weekly, monthly, daily-request, and
daily-token quotas.

Subscription limits must be copied from the purchased plan. They are configuration, not
hardcoded assumptions.

### PAYG

Use `PAYG` for Model Studio token billing. Deployment constructs a versioned `PriceCatalog`
from the official pricing page for the exact model, deployment scope, input tier, output mode,
and cache price. Calls fail closed if no exact price tier exists.

## Usage contract

The Qwen OpenAI-compatible response supplies input, output, cached-input, and reasoning token
counts. Reasoning tokens are already included in completion tokens and are not charged twice.

## Call lifecycle

1. Caller supplies model, bounded `max_completion_tokens`, estimated input tokens, market,
   component, and reason.
2. FinOps reserves projected requests, tokens, and PAYG cost.
3. Daily and subscription rolling quotas are checked.
4. A blocked request never reaches Qwen.
5. Qwen usage and request ID become an immutable record.
6. Threshold alerts are emitted and overruns activate the kill-switch.

The Qwen kill-switch does not stop ETL, deterministic strategies, risk, or paper position
management. Those components continue without optional agent evidence.

## Gateway and operations

`POST /v1/chat/completions` requires `X-API-Key`, `X-Estimated-Input-Tokens`,
`X-NanoDelta-Component`, `X-NanoDelta-Reason`, and optional `X-NanoDelta-Market`.
Streaming is rejected until a usage-aware final-chunk adapter exists.

- `GET /api/finops` returns today's requests, tokens, cost semantics, and kill state.
- `GET /api/finops/alerts` returns budget alerts.
- `POST /api/finops/kill-switch` requires an admin actor.

API keys are never persisted in usage records, alerts, representations, or logs.
