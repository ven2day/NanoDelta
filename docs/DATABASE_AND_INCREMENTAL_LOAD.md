# Database model and incremental loading

## Target database

Use PostgreSQL with TimescaleDB for time-series tables. Local JSON storage remains useful for
tests and the first ETL checkpoint, but production persistence moves behind repository
interfaces. Redis is not required initially; add it only after measured cache pressure or
multi-process coordination justifies it.

## Schema layout

Use a schema per market and layer to make accidental mixing difficult:

```text
control
nse_bronze       nse_silver       nse_gold
forex_bronze     forex_silver     forex_gold
crypto_bronze    crypto_silver    crypto_gold
research
paper
```

`paper` rows still contain `market`, and repository methods must scope every query by market.
If stronger isolation is needed later, split `paper` into three schemas without changing the
contracts.

## Core tables and grain

| Table | Grain / unique key | Purpose |
|---|---|---|
| `{market}_bronze.raw_events` | `record_id` | immutable provider payload |
| `{market}_silver.instruments` | `(canonical_symbol, valid_from)` | symbol/provider mapping history |
| `{market}_silver.candles` | `(symbol, timeframe, open_time)` | canonical settled OHLCV |
| `{market}_silver.quotes` | `(symbol, event_time, provider_sequence)` | canonical quote stream |
| `{market}_silver.order_books` | `(symbol, event_time, sequence_id)` | sequenced snapshots/deltas |
| `{market}_gold.feature_snapshots` | `(symbol, timeframe, event_time, feature_set_version)` | reproducible features |
| `control.provider_watermarks` | `(market, provider, dataset, symbol, timeframe)` | incremental cursor |
| `control.ingestion_runs` | `run_id` | job lifecycle and counters |
| `control.data_quality_issues` | `issue_id` | rejected/quarantined lineage |
| `research.strategy_definitions` | `(strategy_id, version)` | immutable strategy specification |
| `research.validation_runs` | `validation_run_id` | walk-forward results |
| `research.strategy_approvals` | `(market, strategy_id, version, timeframe, horizon)` | runtime admission |
| `research.agent_runs` | `agent_run_id` | TradingAgents execution metadata |
| `research.agent_evidence` | `(agent_run_id, role, evidence_type)` | structured advisory evidence |
| `paper.decisions` | `decision_id` | final audited BUY/SELL/abstain decision |
| `paper.orders` | `order_id`, unique `idempotency_key` | paper order intent/state |
| `paper.fills` | `fill_id` | simulated fills and costs |
| `paper.positions` | `(market, account_id, symbol, position_id)` | current/closed paper positions |
| `paper.outcomes` | `outcome_id`, unique `position_id` | realized performance and attribution |

## Representative migration

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE SCHEMA IF NOT EXISTS control;
CREATE SCHEMA IF NOT EXISTS nse_bronze;
CREATE SCHEMA IF NOT EXISTS nse_silver;
CREATE SCHEMA IF NOT EXISTS nse_gold;

CREATE TABLE nse_bronze.raw_events (
    record_id text PRIMARY KEY,
    provider text NOT NULL,
    event_type text NOT NULL,
    provider_symbol text NOT NULL,
    source_event_time timestamptz,
    received_at timestamptz NOT NULL,
    schema_version integer NOT NULL,
    payload_hash text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE nse_silver.candles (
    symbol text NOT NULL,
    timeframe text NOT NULL,
    open_time timestamptz NOT NULL,
    provider text NOT NULL,
    raw_record_id text NOT NULL,
    open double precision NOT NULL,
    high double precision NOT NULL,
    low double precision NOT NULL,
    close double precision NOT NULL,
    volume double precision NOT NULL CHECK (volume >= 0),
    schema_version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, timeframe, open_time)
);

SELECT create_hypertable('nse_silver.candles', 'open_time', if_not_exists => TRUE);

CREATE TABLE control.provider_watermarks (
    market text NOT NULL,
    provider text NOT NULL,
    dataset text NOT NULL,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    cursor_value text,
    event_time_watermark timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (market, provider, dataset, symbol, timeframe)
);
```

The real migration must create equivalent Forex and Crypto schemas through reviewed SQL,
not runtime `create_all()` calls. Use versioned migrations from the first production database.

## Incremental-load algorithm

1. Read the committed watermark for `(market, provider, dataset, symbol, timeframe)`.
2. Subtract a small overlap window to handle corrected/late provider data.
3. Cap the request end at the last fully settled boundary.
4. Page using the provider's documented cursor direction.
5. Write Bronze idempotently.
6. Normalize and upsert Silver by canonical grain.
7. detect expected-session gaps using the market calendar;
8. commit the watermark only after Bronze and Silver transactions succeed;
9. enqueue targeted repairs for remaining gaps;
10. recompute only affected Gold windows.

Watermarks are optimization state, not proof of completeness. Readiness comes from actual
coverage checks. Status vocabulary: `READY`, `BACKFILLING`, `INSUFFICIENT_DATA`, `STALE`, and
`FAILED`.

## Retention and compression

- Bronze: retain long enough for replay/audit; compress by time and provider.
- Silver: authoritative history; retain indefinitely unless licensing forbids it.
- Gold: reproducible and versioned; old feature versions may be archived after model expiry.
- Quotes/order books: apply explicit, provider-license-aware retention.
- Never delete data only because a watermark moved forward.

