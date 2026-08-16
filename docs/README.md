# NanoDelta documentation map

- [Initial replaceable strategies and validation](INITIAL_STRATEGIES.md)

NanoDelta is built in strict dependency order. The repository now contains tested foundations beyond
ETL, including strategy governance, staged decisions, deterministic risk, paper execution, operations
APIs, and a UI prototype. Later operational acceptance is still tracked explicitly; code existence is
not the same as production readiness.

1. [Target architecture](ARCHITECTURE.md)
2. [ETL and market-data engines](ETL_ENGINES.md)
3. [Database model and incremental loading](DATABASE_AND_INCREMENTAL_LOAD.md)
4. [Strategy governance and TradingAgents](STRATEGY_AND_AGENTS.md)
5. [Staged decision pipeline](STAGED_DECISION_PIPELINE.md)
6. [Implementation roadmap](IMPLEMENTATION_ROADMAP.md)
7. [UI — final phase](UI_LAST.md)
8. [Production deployment foundation](PRODUCTION_DEPLOYMENT.md)

## Non-negotiable rules

- NSE, Forex, and Crypto remain isolated in storage, runtime state, configuration, and health.
- Bronze is immutable; Silver is validated canonical data; Gold is reproducible features.
- Only settled candles enter Silver and Gold.
- Strategy validation and model fitting are offline. Runtime is inference-only.
- TradingAgents produces research evidence, never orders.
- Deterministic risk is the final approval authority.
- Execution remains paper-only until an explicit reviewed decision changes that policy.
- UI reads authoritative APIs and must never invent pipeline state.
- Secrets are supplied through protected files and are never committed or logged.
