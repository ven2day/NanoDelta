# NanoDelta documentation map

NanoDelta is being built in strict dependency order. The current code implements the
ETL foundation. The documents below define the complete target platform without
pretending later phases already exist.

1. [Target architecture](ARCHITECTURE.md)
2. [ETL and market-data engines](ETL_ENGINES.md)
3. [Database model and incremental loading](DATABASE_AND_INCREMENTAL_LOAD.md)
4. [Strategy governance and TradingAgents](STRATEGY_AND_AGENTS.md)
5. [Implementation roadmap](IMPLEMENTATION_ROADMAP.md)
6. [UI — final phase](UI_LAST.md)

## Non-negotiable rules

- NSE, Forex, and Crypto are isolated in storage, runtime state, configuration, and health.
- Bronze is immutable; Silver is validated canonical data; Gold is reproducible features.
- Only settled candles enter Silver and Gold.
- Strategy validation and model fitting are offline. Runtime is inference-only.
- TradingAgents produces research evidence, never orders.
- Deterministic risk is the final approval authority.
- Execution is paper-only until a separate explicit decision changes that policy.
- UI is built last and reads authoritative APIs; it never invents pipeline state.

