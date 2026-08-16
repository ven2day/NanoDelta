# NanoDelta

NanoDelta is a production-oriented, market-isolated quantitative research and paper-trading
platform for **NSE, Forex, and Crypto**.

The project is built in dependency order:

```text
Market data
  -> Raw / Bronze
  -> Canonical / Silver
  -> Features / Gold
  -> Strategy evaluation and approval
  -> Optional TradingAgents research evidence
  -> Deterministic qualification and risk
  -> Final BUY / SELL / abstain decision
  -> Paper execution
  -> Outcomes and offline learning
  -> APIs and operations
  -> UI (last)
```

NanoDelta is a clean rebuild. It does not copy the previous inflated DeltaQuant application.
Every later component must be added as a tested checkpoint on top of authoritative data
contracts.

## Implementation status

| Area | Status |
|---|---|
| Immutable Raw/Bronze record contract | Implemented |
| Canonical settled Silver candle contract | Implemented |
| Deterministic basic Gold features | Implemented |
| Market/provider ownership validation | Implemented |
| Idempotent local file storage | Implemented |
| PostgreSQL/TimescaleDB migrations | Implemented |
| Historical and realtime provider clients | Implemented |
| CSV-driven NSE/Dhan universe bootstrap | Implemented |
| 730-day backfill and incremental gap repair | Implemented |
| Strategy registry and validation | Implemented |
| TradingAgents adapter | Implemented |
| Deterministic risk and paper execution | Implemented |
| Outcomes and learning | Implemented |
| APIs and operational controls | Implemented |
| Qwen Cloud FinOps and spend kill-switch | Implemented |
| Staged strategy scoring and portfolio construction | Implemented |
| Web UI | Functional prototype; API integration pending |
| Docker/Compose deployment foundation | Implemented |
| Deterministic provider-to-paper session evidence | Implemented |
| Credentialed provider and TimescaleDB verification | Opt-in; environment evidence required |

Documentation describes the target architecture. A documented component must not be treated as
implemented until its checkpoint, migrations, tests, and operational controls are complete.

## Core principles

- NSE, Forex, and Crypto remain isolated in storage, configuration, runtime state, and health.
- Bronze is immutable and retains source lineage after secret redaction.
- Silver contains validated canonical market data using UTC timestamps and canonical symbols.
- Gold contains versioned, reproducible analytical features—not BUY/SELL decisions.
- Only settled candles enter Silver and Gold.
- Provider fallback is capability-specific, not one global primary/fallback flag.
- Historical validation and model fitting happen offline.
- Runtime loads only approved strategy/model artifacts and performs inference only.
- TradingAgents produces advisory evidence; it cannot approve strategies, size positions, or
  place orders.
- Deterministic risk is the final authority before paper execution.
- Execution remains paper-only unless the owner explicitly changes that policy.
- UI is the final phase and reads authoritative APIs instead of inventing lifecycle state.

## Market ownership

| Market | Historical primary | Historical fallback | Realtime primary | Realtime fallback | Canonical symbol |
|---|---|---|---|---|---|
| NSE | Dhan | TrueData where licensed/capable | TrueData | Dhan | `RELIANCE` |
| Forex | OANDA | none initially | OANDA | reconnect to OANDA | `EUR_USD` |
| Crypto | OKX | Poloniex | OKX | Poloniex where capable | `BTC_USDT` |

Provider symbols exist in Bronze. Silver and downstream layers use only canonical symbols.

## End-to-end architecture

```text
Dhan / TrueData          OANDA          OKX / Poloniex
       \                   |                   /
        +---------- ingestion engines --------+
                            |
                    Raw / Bronze events
                            |
             validation + provider normalization
                            |
                Canonical / Silver records
                            |
           settled-candle feature materialization
                            |
                  Feature / Gold snapshots
                            |
          strategy registry + candidate generation
                            |
             exact validation-artifact admission
                            |
       TradingAgents evidence (optional, cached, bounded)
                            |
          deterministic qualification and risk
                            |
               final BUY / SELL / abstain
                            |
                  paper execution engine
                            |
             order -> fill -> position -> outcome
                            |
                 monitoring and market APIs
                            |
                         final UI
```

## Current repository structure

```text
NanoDelta/
├── src/
│   └── nanodelta/
│       ├── __init__.py
│       ├── contracts.py          # market/provider enums and Bronze/Silver/Gold records
│       ├── pipeline.py           # Bronze -> Silver -> Gold orchestration
│       ├── storage.py            # idempotent local storage boundary
│       ├── features.py           # deterministic initial Gold features
│       ├── markets/
│           ├── __init__.py
│           └── adapters.py       # Dhan, TrueData, OANDA, OKX, Poloniex normalization
│       ├── persistence/
│       │   ├── migrations.py     # checksum and advisory-lock migration runner
│       │   ├── postgres.py       # market-isolated PostgreSQL record store
│       │   └── cli.py            # nanodelta-migrate command
│       ├── providers/
│           ├── base.py           # history/realtime/capability contracts
│           ├── transports.py     # HTTP and reconnecting stream transports
│           ├── registry.py       # capability-specific primary/fallback routing
│           ├── dhan_auth.py       # protected-file PIN/TOTP token generation
│           ├── dhan.py
│           ├── truedata.py
│           ├── oanda.py
│           ├── okx.py
│           └── poloniex.py
│       ├── strategies/
│       │   ├── registry.py       # exact identity, approval, expiry, revocation
│       │   └── validation.py     # cost, walk-forward, drawdown, Bonferroni gates
│       ├── agents/
│       │   └── tradingagents.py # bounded advisory-only upstream adapter
│       ├── risk/
│       │   └── engine.py         # pure deterministic risk decisions
│       ├── paper/
│       │   └── execution.py      # idempotent paper order/fill/position ledger
│       ├── outcomes/
│       │   └── learning.py       # closed outcomes and offline review evidence
│       ├── history/
│       │   ├── engine.py         # backfill, incremental sync, coverage, repair
│       │   ├── timeframes.py     # settled boundaries and market calendars
│       │   └── postgres.py       # durable watermarks/runs/coverage
│       ├── operations/
│       │   ├── controller.py     # worker lifecycle, authz, idempotency, audit
│       │   └── postgres.py       # durable worker state and atomic audit
│       ├── finops/
│       │   ├── core.py           # usage, pricing, budgets, alerts, kill-switch
│       │   └── qwen.py           # guarded OpenAI-compatible Qwen gateway
│       ├── orchestration/
│       │   ├── decision_pipeline.py # generate, score, review, allocate, revalidate
│       │   └── paper_batch.py     # deterministic risk and paper batch handoff
│       ├── decisions.py           # append-only stage decision contract
│       ├── decisions_postgres.py  # durable decision ledger
│       ├── universe/
│       │   └── nse.py             # symbols.csv loading and Dhan ID resolution
│       └── api/
│           └── app.py            # market-scoped FastAPI application factory
├── migrations/
│   ├── 0001_timescaledb_foundation.sql
│   ├── 0002_strategy_and_agent_governance.sql
│   ├── 0003_paper_execution_and_outcomes.sql
│   ├── 0004_history_and_operations.sql
│   ├── 0005_qwen_finops.sql
│   └── 0006_staged_decision_pipeline.sql
├── tests/
│   ├── test_pipeline.py
│   ├── test_persistence.py
│   ├── test_provider_clients.py
│   ├── test_strategy_registry.py
│   ├── test_tradingagents_adapter.py
│   ├── test_risk_and_paper_execution.py
│   ├── test_outcomes_and_learning.py
│   ├── test_history_engine.py
│   ├── test_api_and_operations.py
│   └── test_qwen_finops.py
├── env/
│   ├── .env.example              # shared persistence/runtime settings
│   ├── .env.nse.example          # Dhan and TrueData
│   ├── .env.forex.example        # OANDA practice environment
│   ├── .env.crypto.example       # OKX and Poloniex public endpoints
│   └── .env.qwen.example         # Qwen credentials and FinOps limits
├── config/
│   └── nse/
│       └── symbols.example.csv    # safe template; copy to symbols.csv locally
├── docs/
│   ├── README.md
│   ├── ARCHITECTURE.md
│   ├── ETL_ENGINES.md
│   ├── DATABASE_AND_INCREMENTAL_LOAD.md
│   ├── STRATEGY_AND_AGENTS.md
│   ├── IMPLEMENTATION_ROADMAP.md
│   └── UI_LAST.md
├── AGENTS.md
├── pyproject.toml
├── LICENSE
└── README.md
```

## Target project structure

Directories are introduced only when their implementation checkpoint begins.

```text
src/nanodelta/
├── core/
│   ├── contracts/               # shared immutable layer and lifecycle records
│   ├── quality/                 # validation and quarantine vocabulary
│   ├── lineage/                 # correlation and source-to-outcome lineage
│   ├── routing/                 # provider capability and fallback contracts
│   ├── clocks/                  # UTC and settled-boundary utilities
│   └── observability/           # metrics/logging interfaces
├── etl/
│   ├── bronze/                  # append-only ingestion
│   ├── silver/                  # canonical normalization and reconciliation
│   ├── gold/                    # versioned feature materialization
│   ├── backfill/                # 730-day resumable history loading
│   ├── incremental/             # watermarks, overlap windows, and repairs
│   └── orchestration/           # job state, retries, start/stop/drain
├── markets/
│   ├── nse/
│   │   ├── providers/           # Dhan and TrueData
│   │   ├── calendar/            # sessions and verified holidays
│   │   ├── reconciliation/
│   │   └── config/
│   ├── forex/
│   │   ├── providers/           # OANDA
│   │   ├── calendar/            # 24x5 UTC alignment
│   │   └── config/
│   └── crypto/
│       ├── providers/           # OKX and Poloniex
│       ├── orderbook/           # sequence-aware books
│       └── config/
├── persistence/
│   ├── repositories/            # storage interfaces and implementations
│   ├── migrations/              # reviewed versioned SQL
│   └── timescale/               # hypertable-specific adapters
├── research/
│   ├── strategies/              # versioned deterministic strategies
│   ├── backtesting/             # cost-aware backtests
│   ├── validation/              # walk-forward and multiple-testing controls
│   ├── registry/                # approval, expiry, and revocation
│   └── tradingagents/           # bounded external-framework adapter
├── decisions/
│   ├── candidates/
│   ├── qualification/
│   └── risk/                    # deterministic final approval
├── paper/
│   ├── execution/
│   ├── orders/
│   ├── positions/
│   └── outcomes/
├── api/                         # market-scoped read/command endpoints
└── operations/                  # workers, health, jobs, alerts, and recovery

web/                              # created only in the final UI checkpoint
├── overview/
├── nse/
├── forex/
├── crypto/
└── operations/
```

## ETL layers

### Raw / Bronze

Bronze stores a deterministic record ID, market, provider, event type, provider symbol,
received time, schema version, redacted payload, and payload hash. Invalid payloads remain in
Bronze for replay and audit.

### Canonical / Silver

Silver maps provider records to canonical UTC schemas. Candle grain is:

```text
(market, canonical_symbol, timeframe, open_time)
```

Silver rejects provider/market mismatches, missing symbol mappings, naive timestamps, non-finite
prices, impossible OHLC relationships, negative volume, duplicates, and incomplete candles.

### Features / Gold

Gold is calculated only from settled, ordered Silver records. Every feature snapshot is linked to
its source candle/window and feature-set version. Gold is deterministic and never contains an
agent recommendation or final BUY/SELL decision.

## Storage layout

The current implementation uses an idempotent local file lake:

```text
data/{nse|forex|crypto}/{bronze|silver|gold}/
  event_date=YYYY-MM-DD/{record_id}.json
```

The production target is PostgreSQL with TimescaleDB:

```text
control
nse_bronze       nse_silver       nse_gold
forex_bronze     forex_silver     forex_gold
crypto_bronze    crypto_silver    crypto_gold
research
paper
```

Primary production tables include raw events, instruments, candles, quotes, order books, feature
snapshots, provider watermarks, ingestion runs, data-quality issues, strategy definitions,
validation runs, approvals, agent evidence, decisions, paper orders/fills/positions, and outcomes.

See [Database and incremental loading](docs/DATABASE_AND_INCREMENTAL_LOAD.md) for table grains,
representative DDL, retention, and TimescaleDB guidance.

## Historical and incremental loading

Every active symbol targets at least 730 days where the provider supports it. Required grains are
5m, 15m, 30m, 1h, 4h, and 1d.

Incremental loading:

1. reads the committed provider/symbol/timeframe watermark;
2. subtracts an overlap window for late corrections;
3. caps requests at the last settled boundary;
4. follows provider-specific pagination;
5. writes Bronze idempotently;
6. normalizes/upserts Silver by canonical grain;
7. detects expected-session gaps using the market calendar;
8. advances the watermark only after durable success;
9. queues targeted gap repairs;
10. recomputes only affected Gold windows.

Watermarks optimize loading but do not prove completeness. Readiness is calculated from actual
coverage using `READY`, `BACKFILLING`, `INSUFFICIENT_DATA`, `STALE`, and `FAILED`.

The implemented history engine keeps provider-specific pagination in each provider client and
owns the cross-provider guarantees: fallback, committed watermarks, bounded overlap, actual
settled-Silver coverage, and contiguous targeted repair windows. The PostgreSQL adapter reads
coverage directly from the correct market Silver schema.

Default calendars deliberately contain no guessed exchange holidays. Deployment must inject a
verified NSE holiday set for the requested 730-day horizon before treating readiness as
production-authoritative.

### NSE symbols and Dhan authentication

Copy `config/nse/symbols.example.csv` to the ignored local file
`config/nse/symbols.csv`. Each enabled row creates a 730-day history job for every requested
timeframe. A blank `security_id` is resolved from Dhan's official detailed instrument master;
missing or ambiguous symbols stop startup instead of silently selecting an instrument.

Automatic authentication reads the six-digit Dhan PIN and Base32 TOTP secret from separate
protected files using `DHAN_PIN_PATH` and `DHAN_TOTP_SECRET_PATH`. The generated access token is
cached until shortly before expiry. Credentials are not stored in the CSV or logged. A manually
generated `DHAN_ACCESS_TOKEN` remains supported as an alternative.

Dhan has no direct 30-minute historical interval in this adapter. NanoDelta requests 15-minute
data and emits only complete 30-minute pairs aligned to the NSE 09:15 IST session. See
[NSE symbols and Dhan authentication](docs/NSE_SYMBOLS_AND_DHAN_AUTH.md) for the CSV contract,
secret setup, startup wiring, and operational checks.

## APIs and operational controls

`nanodelta.api.create_app(ApiServices(...))` creates the FastAPI application. Reads are strictly
market-scoped. Runtime and repair commands require `X-API-Key`, an operator/admin actor,
`Idempotency-Key`, and explicit confirmation.

Implemented endpoints include overview, market health/history/features/strategies/agent
runs/decisions/paper positions/outcomes, history repair, and runtime start/stop/drain.
Start/stop/drain invokes an injected market worker lifecycle; missing workers fail without
changing state. PostgreSQL transition persistence writes worker state and its immutable audit
record in one transaction. NanoDelta provides no default API key.

## Qwen Cloud FinOps

Qwen calls pass through an authenticated OpenAI-compatible gateway that records provider request
ID, model, deployment scope, market/component/reason attribution, and
input/output/cached/reasoning tokens. PAYG uses an injected, versioned exact-model price catalog.
Subscription mode records zero marginal token cost, reports the configured fixed fee separately,
and enforces rolling request plus daily token/request budgets.

Budget thresholds create alerts. Exceeding a limit activates a Qwen-only kill-switch; ETL,
deterministic risk, and paper position management continue. See
[Qwen Cloud FinOps](docs/QWEN_FINOPS.md).

## Strategy lifecycle

```text
idea
 -> implementation
 -> cost-aware backtest
 -> walk-forward validation
 -> multiple-testing controls
 -> approval artifact
 -> runtime registry
 -> candidate
 -> deterministic final decision
```

Runtime strategies are plugins. A strategy self-declares factual compatibility and deterministic
signal generation; market/sector/symbol/MTF regime evidence affects expected-R scoring rather than a
central veto matrix. See [staged decision pipeline](docs/STAGED_DECISION_PIPELINE.md).

Runtime admission uses the exact identity:

```text
(market, strategy_id, strategy_version, timeframe, trade_horizon, feature_set_version)
```

A strategy is not runtime-eligible unless an unexpired approval exists for the exact identity.
The implemented validator gates minimum sample size, walk-forward stability, cost-adjusted
expectancy, maximum drawdown, and Bonferroni-adjusted significance. Registry definitions and
approval artifacts are immutable; changed logic requires a new strategy version.
Initial planned families include EMA9 + RSI14, SuperTrend ATR14 x3 with ADX, NSE VWAP pullback,
NSE opening-range breakout, trend pullback, momentum continuation, range mean-reversion, and
Crypto order-book imbalance after order-book quality is proven.

## TradingAgents integration

[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) is integrated
through a pinned adapter, not copied wholesale into the core.

NanoDelta may use its analyst, research/debate, recommendation, and risk-review concepts to
produce structured advisory evidence. TradingAgents cannot:

- override NanoDelta Silver/Gold prices;
- approve an unvalidated strategy;
- size positions or relax deterministic limits;
- write orders, fills, positions, Bronze, Silver, or Gold;
- access broker credentials;
- call a broker or exchange.

```text
Gold + approved candidate
          |
  TradingAgents adapter
          |
structured evidence and recommendation
          |
NanoDelta deterministic qualification/risk
          |
final BUY / SELL / abstain
```

Agent inputs, role evidence, citations, model/configuration, token cost, failures, and final
NanoDelta influence are stored as immutable research records. Agent output is not Gold because it
is non-deterministic.

The adapter wraps the upstream `TradingAgentsGraph.propagate(ticker, date)` contract lazily.
TradingAgents remains an optional external install and its version/commit must be supplied to the
adapter. A missing package, timeout, or malformed decision produces explicit `ABSTAIN` evidence,
not an approval, order, or hidden retry.

See [Strategy governance and TradingAgents](docs/STRATEGY_AND_AGENTS.md).

## Paper execution and outcomes

Only an approved deterministic decision can enter paper execution. Orders require idempotency
keys and produce an audited order -> fill -> position lifecycle. Closed positions create outcomes
linked to the exact Gold snapshot, strategy approval, optional agent run, decision, and execution.

No outcome or learning component can place an order directly.

The implemented risk engine enforces exact strategy approval, portfolio freshness, daily loss,
order/position notional, market/total gross exposure, and open-position limits. The execution
engine has no live mode or broker interface: it produces deterministic immediate paper fills with
configured slippage and fees, maintains signed positions, and refuses rejected decisions.

Closed positions materialize one idempotent outcome with complete lineage. Offline learning
summarizes exact-strategy outcomes as `INSUFFICIENT_DATA`, `RETAIN`, `REVIEW`, or
`SUSPENSION_REVIEW` evidence. It cannot mutate approvals or invoke risk/execution.

## UI is last

The UI is implemented after database read models, lifecycle records, APIs, and operational
commands exist. It will provide:

- one common overview;
- separate NSE, Forex, and Crypto workspaces;
- data readiness, gaps, and repairs;
- provider routing and health;
- strategy approvals and evidence;
- TradingAgents role evidence and citations;
- final decisions and rejection reasons;
- paper orders, positions, and outcomes;
- audited start, stop, drain, and repair controls.

The frontend never accesses the database directly or fabricates counts. See [UI — final phase](docs/UI_LAST.md).

## Development setup

Python 3.11 or newer is required.

### Linux/macOS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
mypy src
```

## Apply database migrations

TimescaleDB must be installed on the target PostgreSQL server. Set `DATABASE_URL`, then run:

```bash
nanodelta-migrate
```

The runner serializes migration execution with a PostgreSQL advisory lock, verifies the SHA-256
checksum of every previously applied migration, and records successful versions in
`control.schema_migrations`. It refuses to continue if an applied migration file was edited.

Environment configuration is separated by responsibility:

- `env/.env.example` — shared database and runtime settings;
- `env/.env.nse.example` — Dhan and TrueData;
- `env/.env.forex.example` — OANDA practice account;
- `env/.env.crypto.example` — OKX and Poloniex public endpoints;
- `env/.env.qwen.example` — Qwen credentials, billing mode, and FinOps limits.

Copy only the required templates into your deployment's secret/environment store. Local populated
files such as `env/.env.nse` and `env/.env.forex` are ignored by Git and must never be committed.
For NSE, also keep `config/nse/symbols.csv` and every file below `secrets/` local; both paths are
ignored. Never put a PIN or TOTP secret directly in an environment file.

Provider unit tests use injected transports/SDK fakes and never require secrets. Before deploying
any market worker, run an opt-in credentialed smoke test for the subscribed account and data
entitlements; provider access, symbol permissions, and TrueData exchange approvals vary by account.
The exact replay, TimescaleDB, and secret-file opt-in procedures are documented in
[Provider, database and paper-session verification](docs/PROVIDER_DATABASE_E2E.md). The committed
evidence report proves deterministic wiring only and does not claim a live exchange session.

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
ruff check .
mypy src
```

## Current ETL example

```python
from datetime import UTC, datetime
from pathlib import Path

from nanodelta.contracts import EventType, Market, Provider
from nanodelta.pipeline import EtlPipeline
from nanodelta.storage import FileLake

pipeline = EtlPipeline(FileLake(Path("data")))
result = pipeline.ingest(
    market=Market.CRYPTO,
    provider=Provider.OKX,
    event_type=EventType.CANDLE,
    provider_symbol="BTC-USDT",
    payload={
        "ts": "1786752000000",
        "o": "60000",
        "h": "61000",
        "l": "59500",
        "c": "60500",
        "vol": "120.5",
        "confirm": "1",
    },
    received_at=datetime.now(UTC),
)
print(result.canonical)
```

`EtlPipeline.ingest` persists Bronze first. Invalid or incomplete rows remain in Bronze but do
not enter Silver. Gold is built only from validated settled Silver candles.

## Documentation

- [Documentation map](docs/README.md)
- [Target architecture](docs/ARCHITECTURE.md)
- [ETL and market-data engines](docs/ETL_ENGINES.md)
- [NSE symbols and Dhan authentication](docs/NSE_SYMBOLS_AND_DHAN_AUTH.md)
- [Database and incremental loading](docs/DATABASE_AND_INCREMENTAL_LOAD.md)
- [Strategy governance and TradingAgents](docs/STRATEGY_AND_AGENTS.md)
- [Qwen Cloud FinOps](docs/QWEN_FINOPS.md)
- [Staged decision pipeline](docs/STAGED_DECISION_PIPELINE.md)
- [Initial replaceable strategies and validation](docs/INITIAL_STRATEGIES.md)
- [Implementation roadmap](docs/IMPLEMENTATION_ROADMAP.md)
- [UI — final phase](docs/UI_LAST.md)
- [Production deployment foundation](docs/PRODUCTION_DEPLOYMENT.md)
- [Executable multi-market runtime](docs/EXECUTABLE_RUNTIME.md)

## Implementation order

1. Production database foundation
2. Historical ingestion and 730-day backfill
3. Realtime engines
4. Canonical quality and Gold expansion
5. Strategy research, validation, and registry
6. TradingAgents adapter
7. Deterministic qualification and paper execution
8. Outcomes and offline learning
9. APIs and operations
10. UI

See the [implementation roadmap](docs/IMPLEMENTATION_ROADMAP.md) for the definition of done for
every checkpoint.

## License

NanoDelta is licensed under the MIT License. TradingAgents is an external Apache-2.0 project;
pin its version and preserve required attribution if any upstream code is copied or modified.
