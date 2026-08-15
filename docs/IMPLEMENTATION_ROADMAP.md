# Implementation roadmap

Each checkpoint must leave the repository runnable, add tests, and document any scope boundary.
Do not start the UI before the APIs and lifecycle records exist.

## Checkpoint 0 — current ETL kernel

- immutable RawRecord;
- canonical settled candles;
- deterministic basic features;
- market/provider ownership checks;
- idempotent local storage;
- unit tests.

## Checkpoint 1 — production database foundation

- PostgreSQL/TimescaleDB repository interfaces;
- versioned migrations and schemas;
- Bronze/Silver/Gold tables for all three markets;
- ingestion runs, watermarks, quality issues;
- transaction, idempotency, and migration tests.

## Checkpoint 2 — historical ingestion engines

Status: core 730-day backfill, incremental overlap, coverage readiness, fallback, and targeted
gap repair implemented.

- Dhan, OANDA, OKX primary historical clients;
- TrueData and Poloniex capability-specific fallbacks;
- 730-day resumable backfill;
- provider pagination/rate limiting;
- actual coverage and gap-repair engine;
- 5m, 15m, 30m, 1h, 4h, 1d readiness.

## Checkpoint 3 — realtime engines

- TrueData/Dhan NSE routing;
- OANDA stream;
- OKX/Poloniex stream and Crypto sequence validation;
- reconnect, hysteresis, subscription restoration, staleness health;
- forming-bar aggregation and settled-bar publication.

## Checkpoint 4 — canonical quality and Gold expansion

- instruments, quotes, trades, order books;
- calendars and timeframe alignment;
- reconciliation across providers;
- versioned feature sets and targeted recomputation;
- complete lineage and quality dashboards through APIs.

## Checkpoint 5 — strategy research and governance

- versioned strategy registry;
- deterministic strategy implementations;
- cost-aware backtesting;
- walk-forward and multiple-testing controls;
- approval/expiry/revocation workflow;
- runtime loads approved artifacts only.

## Checkpoint 6 — TradingAgents adapter

- pinned upstream version;
- NanoDelta-owned input/output schemas;
- bounded candidate review;
- structured evidence and citation persistence;
- caching, budgets, timeouts, checkpoint recovery, and deterministic fallback;
- no execution permission.

## Checkpoint 7 — qualification and paper execution

Status: core risk decision and paper ledger contracts implemented.

- candidate consolidation and conflicts (planned with strategy evaluators);
- deterministic risk limits and approval;
- idempotent paper order/fill/position lifecycle;
- configurable slippage/fee cost policy for every market;
- quote-driven exits and external reconciliation (planned; no broker exists in paper-only mode);
- paper-only enforcement at multiple boundaries.

## Checkpoint 8 — outcomes and learning

Status: core closed outcomes and bounded offline assessment implemented.

- closed-position outcomes linked to Gold, strategy, decision, agent run, and execution;
- performance by exact market/strategy/timeframe/horizon/version identity;
- immutable offline review/training inputs;
- no direct outcome-to-order feedback.

## Checkpoint 9 — APIs and operations

Status: core market APIs and authenticated/audited runtime controls implemented.

- market-scoped health, history, readiness, pipeline, strategy, agent, decision, and paper APIs;
- start/stop/drain/manual-repair commands with authorization and audit;
- metrics, structured logs, alerts, cost tracking, backups, and recovery runbooks.

## Checkpoint 10 — UI last

- common shell plus isolated NSE, Forex, and Crypto workspaces;
- exact lifecycle and data-readiness states;
- history, strategy, TradingAgents evidence, decisions, paper positions, and outcomes;
- no direct database access and no fabricated frontend calculations.

## Definition of done for every checkpoint

- schemas and contracts documented;
- unit, integration, isolation, idempotency, and failure tests pass;
- lint and strict typing pass;
- secrets cannot be persisted or logged;
- migration/rollback and operational behavior are documented;
- NSE, Forex, and Crypto impact is stated explicitly;
- known limitations are written down instead of hidden.
