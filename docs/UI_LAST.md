# UI — final implementation phase

The UI is last because it must display real backend state. Building it earlier recreates the
previous problem: labels and counters exist before authoritative lifecycle records exist.

## Information architecture

```text
NanoDelta
├── Overview
├── NSE
│   ├── Data
│   ├── Strategies
│   ├── Agent evidence
│   ├── Decisions
│   └── Paper portfolio
├── Forex
│   └── same lifecycle, Forex-specific details
├── Crypto
│   └── same lifecycle, Crypto/order-book-specific details
└── Operations
    ├── Jobs and watermarks
    ├── Data quality and repairs
    ├── Provider health
    ├── Model/strategy registry
    └── LLM cost and failures
```

## Overview page

Show only cross-market summaries:

- worker state and last heartbeat;
- provider health, source, lag, and fallback reason;
- Bronze/Silver/Gold freshness;
- universe size and ready-symbol count;
- approved strategies and expiring artifacts;
- current paper exposure and realized outcome summary;
- active alerts.

Counts come directly from backend read models. The frontend never calculates `universe - skipped`
or combines symbol-level and grain-level counts.

## Market workspace

Each workspace uses the same structure but preserves market-specific facts:

1. session clock and calendar;
2. provider routing and health;
3. history/backfill/readiness by symbol and timeframe;
4. pipeline funnel from changed candle to final decision;
5. strategy eligibility and validation artifact;
6. TradingAgents role evidence and citations;
7. deterministic rejection/approval reason;
8. paper order, fill, position, and outcome history;
9. start/stop/repair controls with confirmation and audit.

## Decision detail

One expandable record must explain the complete lineage:

```text
Silver candle(s)
 -> Gold feature snapshot
 -> strategy/version and validation approval
 -> deterministic setup evidence
 -> TradingAgents run and per-role evidence (if used)
 -> qualification/risk rules
 -> final BUY/SELL/abstain
 -> paper order/fill/position
 -> closed outcome
```

The UI must distinguish `not run`, `not applicable`, `abstained`, `failed`, and `zero`. It must
show timestamps with timezone labels and provide UTC internally.

## API prerequisites

Do not implement a panel until its endpoint and authoritative grain exist. Minimum endpoints:

- `GET /api/overview`
- `GET /api/{market}/health`
- `GET /api/{market}/history-status`
- `POST /api/{market}/history-repair`
- `GET /api/{market}/features`
- `GET /api/{market}/strategies`
- `GET /api/{market}/agent-runs`
- `GET /api/{market}/decisions`
- `GET /api/{market}/paper/positions`
- `GET /api/{market}/paper/outcomes`
- `POST /api/{market}/runtime/{start|stop|drain}`

Commands require authentication, authorization, idempotency keys, confirmation for broad actions,
and an audit record. Read endpoints remain strictly market-scoped.

