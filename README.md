<div align="center">
  <img src="docs/assets/nanodelta-banner.svg" alt="NanoDelta — multi-market quantitative research and paper trading" width="100%" />

  <br />

  [![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
  [![FastAPI](https://img.shields.io/badge/FastAPI-0.123%2B-009688?logo=fastapi&logoColor=white)](src/nanodelta/api/)
  [![Next.js](https://img.shields.io/badge/Next.js-16-000000?logo=nextdotjs)](web/package.json)
  [![TimescaleDB](https://img.shields.io/badge/PostgreSQL%20%2B%20TimescaleDB-4169E1?logo=postgresql&logoColor=white)](migrations/)
  [![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
  [![Paper only](https://img.shields.io/badge/execution-PAPER%20ONLY-F5A623)](#safety-boundary)
  [![License](https://img.shields.io/badge/license-MIT-22C55E)](LICENSE)

  **A market-isolated quantitative research and paper-trading platform for NSE, Forex, and Crypto.**

  *Market data → canonical history → features → validated strategies → deterministic risk → paper execution → outcomes*
</div>

> [!IMPORTANT]
> NanoDelta is a **paper-trading research platform**. It cannot place live broker or exchange
> orders. The repository contains implemented foundations and an active integration roadmap; it is
> not evidence of a completed production deployment.

## Table of contents

- [What NanoDelta is](#what-nanodelta-is)
- [Core capabilities](#core-capabilities)
- [Market coverage](#market-coverage)
- [Architecture](#architecture)
- [Decision lifecycle](#decision-lifecycle)
- [Web application and BUY/SELL signals](#web-application-and-buysell-signals)
- [Services and process ownership](#services-and-process-ownership)
- [Data platform](#data-platform)
- [Strategy governance](#strategy-governance)
- [Risk and paper execution](#risk-and-paper-execution)
- [Quick start](#quick-start)
- [Configuration and secrets](#configuration-and-secrets)
- [Local endpoints](#local-endpoints)
- [Development](#development)
- [Testing](#testing)
- [Deployment and recovery](#deployment-and-recovery)
- [Production-readiness status](#production-readiness-status)
- [Detailed project structure](#detailed-project-structure)
- [Documentation](#documentation)
- [Safety boundary](#safety-boundary)

## What NanoDelta is

NanoDelta is a clean, self-hosted foundation for operating the same controlled quantitative
workflow across three different market domains:

- **NSE equities** using Dhan and TrueData provider boundaries;
- **Forex** using OANDA;
- **Crypto** using OKX and Poloniex.

It is not a black-box signal feed. Provider payloads, normalized candles, generated features,
strategy identity, validation evidence, candidate scores, rejection reasons, risk decisions,
paper orders, fills, positions and outcomes are designed to remain traceable.

The central design rule is **market isolation**. NSE, Forex and Crypto share contracts and
operational patterns, but they do not share provider state, canonical symbols, database schemas,
worker state or health conclusions.

## Core capabilities

### Market data and ETL

- immutable Bronze records with provider lineage and redacted payloads;
- canonical Silver candles using UTC timestamps and canonical symbols;
- deterministic Gold feature snapshots linked to their source windows;
- settled-candle enforcement before Silver and Gold;
- provider-specific pagination behind shared history orchestration;
- idempotent writes, committed watermarks, overlap windows and targeted gap repair;
- 730-day target history where provider entitlement and retention support it;
- required grains: 5m, 15m, 30m, 1h, 4h and 1d;
- capability-specific primary/fallback routing.

### Research and decisions

- versioned strategy definitions and exact runtime identities;
- cost-aware validation and walk-forward evidence;
- multiple-testing control and approval expiry/revocation;
- optional bounded TradingAgents research evidence;
- staged candidate scoring and portfolio construction;
- append-only decision records with explicit rejection reasons;
- Qwen cost attribution, budgets, alerts and a Qwen-only kill switch.

### Risk and paper execution

- deterministic final risk authority;
- daily-loss, notional, exposure and position-count limits;
- idempotent paper order and fill processing;
- signed paper positions and closed outcomes;
- full lineage back to strategy, features, validation and decision;
- offline outcome review that cannot place orders or mutate approvals.

### Operations

- FastAPI market-scoped read and command surfaces;
- authenticated administrative commands through API-key contracts;
- idempotency and confirmation requirements for state-changing operations;
- checksum-verified, advisory-lock-protected database migrations;
- Docker Compose foundation for TimescaleDB, migration, API and web;
- backup, restore and deployment-verification scripts.

## Market coverage

| Market | Historical provider | Realtime design | Canonical symbol | Session model |
|---|---|---|---|---|
| NSE | Dhan; TrueData where licensed/capable | TrueData primary, Dhan fallback | `RELIANCE` | Exchange calendar, IST boundaries |
| Forex | OANDA | OANDA with reconnect/recovery | `EUR_USD` | 24×5, UTC alignment |
| Crypto | OKX primary, Poloniex fallback | OKX primary, Poloniex capability fallback | `BTC_USDT` | 24×7 |

Provider symbols are retained in Bronze. Silver and all downstream layers use canonical symbols.

## Architecture

<img src="docs/assets/nanodelta-architecture.svg" alt="NanoDelta end-to-end architecture" width="100%" />

```mermaid
flowchart LR
    subgraph Providers
      NSE[Dhan / TrueData]
      FX[OANDA]
      CR[OKX / Poloniex]
    end

    NSE --> B[Immutable Bronze]
    FX --> B
    CR --> B
    B --> S[Canonical settled Silver]
    S --> G[Versioned Gold features]
    G --> C[Strategy candidates]
    C --> V[Exact validation admission]
    V --> Q[Scoring and portfolio construction]
    Q --> R[Deterministic risk]
    R --> P[Paper execution]
    P --> O[Positions and outcomes]
    O --> API[FastAPI operations API]
    API --> UI[Next.js operations UI]
    A[Optional agent evidence] -. advisory only .-> Q
```

### Architectural invariants

1. Bronze is append-only and preserves source lineage.
2. Invalid provider data remains auditable but cannot silently enter Silver.
3. Only validated, settled candles can produce Gold features.
4. Gold contains reproducible analytical data—not final BUY/SELL decisions.
5. A runtime strategy must match an exact, unexpired approval identity.
6. Agent/LLM output is evidence only; it cannot approve, size or execute.
7. Deterministic risk is the final decision authority.
8. Paper execution has no live broker interface.
9. State-changing operational commands are authenticated and audited.
10. The UI must eventually read authoritative APIs; prototype data is not trading evidence.

## Decision lifecycle

```text
Provider event
  → Bronze record
  → canonical Silver candle
  → Gold feature snapshot
  → strategy setup
  → exact approval lookup
  → candidate score
  → optional advisory review
  → portfolio selection
  → deterministic risk decision
  → final BUY / SELL / ABSTAIN
  → paper order
  → paper fill
  → position
  → closed outcome
  → offline review evidence
```

Every stage may reject or abstain. Rejection is a first-class result, not an error hidden from the
operator.

### Where a BUY/SELL signal comes from

A BUY or SELL shown to an operator is not a raw indicator crossover. It is the result of:

1. settled and validated market data;
2. deterministic feature calculation;
3. strategy-specific setup generation;
4. exact strategy approval admission;
5. cost- and regime-aware scoring;
6. portfolio selection;
7. final deterministic risk checks.

A setup that fails any required stage remains visible as rejected or abstained and must not become
a paper order.

## Web application and BUY/SELL signals

The Next.js prototype defines a common shell and the same page structure for NSE, Forex and Crypto.

| Page | Operator purpose | Important filters |
|---|---|---|
| Overview | Cross-market equity, activity and health | Market, date |
| Workspace | One-market command center | Symbol, side, timeframe, status, date |
| Decisions | Final BUY/SELL/ABSTAIN lifecycle | Symbol, BUY/SELL, strategy, timeframe, stage, status |
| Charts | Price, indicators and signal markers | Symbol, timeframe, strategy, date range |
| Portfolio | Paper positions and exposure | Market, symbol, side, state |
| Orders & Trades | Paper order/fill ledger | Symbol, side, status, date |
| Strategies | Version, approval and expiry | Market, family, timeframe, approval |
| Strategy Lab | Backtest/validation experiments | Strategy, version, symbol, timeframe |
| Performance | Outcomes by strategy and regime | Market, strategy, symbol, period |
| Data Center | Coverage, gaps and repair state | Provider, symbol, timeframe, readiness |
| Operations | Worker and provider health | Market, service, state |
| Alerts & Events | Operational and policy events | Severity, component, market, time |
| Risk | Limits and current utilization | Market, account, limit family |
| Reports | Trading, risk, data and cost reports | Type, market, period |
| Settings | Deployment/runtime configuration | Market, provider, component |
| Audit Log | Immutable operator and system changes | Actor, action, resource, time |

**Current `main` boundary:** the UI is a visual prototype and includes representative values in
`web/app/page.tsx`. It demonstrates navigation, filtering and decision presentation, but it is
not yet authoritative trading evidence. API-backed UI authentication and unavailable-state work
is being integrated separately.

The final signal location is **Workspace → Decisions**. Each row is designed to show time, symbol,
BUY/SELL, strategy, timeframe, expected-R and current stage. Selecting a row opens its full
decision lifecycle.

## Services and process ownership

The current Compose file contains four services:

| Service | Responsibility | Lifecycle |
|---|---|---|
| `db` | PostgreSQL 16 + TimescaleDB durable state | Long-running |
| `migrate` | Apply verified SQL migrations before application startup | Run-to-completion |
| `api` | FastAPI health, reads and guarded operational commands | Long-running |
| `web` | Next.js operator interface | Long-running |

The database must become healthy before migrations run. The API starts only after migrations
succeed. The web service starts after API health is available.

Long-lived multi-market worker supervision, realtime failover and the observability overlay are
developed in follow-up integration branches and are not represented as merged `main` services
until those PRs land.

## Data platform

### Bronze

Bronze retains:

- market and provider;
- provider symbol and event type;
- received timestamp and schema version;
- redacted provider payload;
- payload hash and deterministic record identity.

Bronze is written first. A malformed or incomplete event may remain in Bronze for replay and audit
without entering downstream layers.

### Silver

Silver normalizes provider data into canonical contracts. Candle grain is:

```text
(market, canonical_symbol, timeframe, open_time)
```

Validation includes market/provider ownership, symbol mapping, timezone awareness, finite prices,
valid OHLC relationships, non-negative volume, duplicate handling and settled-state enforcement.

### Gold

Gold is built from ordered settled Silver inputs. Each snapshot records its feature-set version and
source relationship. Feature generation is deterministic so a historical decision can be
reproduced.

### TimescaleDB schemas

```text
control
nse_bronze       nse_silver       nse_gold
forex_bronze     forex_silver     forex_gold
crypto_bronze    crypto_silver    crypto_gold
research
paper
```

The migrations create market-isolated storage plus control, strategy, decision, execution,
outcome, history and FinOps records.

### History and incremental loading

Each job:

1. reads its last committed watermark;
2. subtracts a bounded overlap window;
3. caps the request at the last settled market boundary;
4. follows provider-specific pagination;
5. writes Bronze idempotently;
6. normalizes and upserts Silver by canonical grain;
7. measures coverage against expected sessions;
8. commits the watermark only after durable success;
9. schedules targeted contiguous gap repair;
10. recomputes affected Gold windows.

Watermarks optimize fetching; they do not prove completeness. Readiness comes from actual coverage.

## Strategy governance

Runtime admission uses this exact identity:

```text
(market, strategy_id, strategy_version, timeframe, trade_horizon, feature_set_version)
```

A similar name, newer code or approval for a different timeframe is not sufficient. Approval
artifacts are immutable, expire, and can be revoked.

Validation supports:

- minimum trade samples;
- transaction costs and slippage;
- walk-forward evaluation;
- stability checks;
- cost-adjusted expectancy;
- maximum drawdown;
- Bonferroni-adjusted significance.

Initial strategy families represented in the architecture include EMA + RSI, SuperTrend + ADX,
VWAP pullback, opening-range breakout, trend pullback, momentum continuation and range
mean-reversion. A family name does not mean a production approval exists.

### TradingAgents and Qwen

TradingAgents is wrapped as an optional external research adapter. Qwen access is guarded by model
attribution, token/accounting records, budgets, alerts and a component-specific kill switch.

Neither component can:

- write Bronze, Silver or Gold;
- approve or revoke a strategy;
- relax deterministic risk;
- write an order or fill;
- access broker execution credentials;
- convert an abstention into a BUY or SELL.

## Risk and paper execution

The deterministic risk engine can reject a candidate for:

- missing or stale strategy approval;
- stale portfolio state;
- daily-loss limit;
- order or position notional;
- market or total gross exposure;
- maximum open positions;
- invalid decision lineage.

Accepted decisions enter an idempotent paper ledger:

```text
decision → paper order → paper fill → signed position → closed outcome
```

The paper engine applies configured slippage and fees. It does not contain a live-mode switch or a
broker-order adapter.

## Quick start

### Prerequisites

- Docker Engine 24+ and Docker Compose v2
- Git
- OpenSSL or another secure random-secret generator
- provider credentials only for markets you intentionally configure

### Docker Compose

```bash
git clone https://github.com/ven2day/NanoDelta.git
cd NanoDelta

cp env/.env.production.example .env.production
mkdir -p secrets
openssl rand -base64 48 > secrets/db_password
openssl rand -base64 48 > secrets/admin_api_key
chmod 600 secrets/db_password secrets/admin_api_key

docker compose --env-file .env.production build
docker compose --env-file .env.production up -d
docker compose --env-file .env.production ps
```

Verify the deployment:

```bash
curl -fsS http://127.0.0.1:8000/health
docker compose logs --since=10m migrate api web
bash scripts/verify-deployment.sh
```

Stop without deleting durable TimescaleDB data:

```bash
docker compose down
```

Do not use `docker compose down -v` unless you intentionally want to delete the database volume.

## Configuration and secrets

Environment templates are separated by responsibility:

| Template | Responsibility |
|---|---|
| `env/.env.example` | Shared local runtime/database configuration |
| `env/.env.production.example` | Compose ports and production deployment values |
| `env/.env.nse.example` | Dhan and TrueData settings |
| `env/.env.forex.example` | OANDA practice settings |
| `env/.env.crypto.example` | OKX and Poloniex public endpoints |
| `env/.env.qwen.example` | Qwen model, budget and billing settings |

Sensitive values belong in protected files below `secrets/`, not committed environment files.

| Secret | Purpose |
|---|---|
| `secrets/db_password` | PostgreSQL password |
| `secrets/admin_api_key` | Administrative API command key |
| Dhan PIN file | Automatic Dhan authentication |
| Dhan TOTP secret file | Automatic Dhan token generation |

Use mode `0600`. Never print secrets in CI, shell history, screenshots or issue reports.

### NSE universe

Copy the safe template and populate only licensed instruments:

```bash
cp config/nse/symbols.example.csv config/nse/symbols.csv
```

A blank Dhan security ID is resolved from the detailed instrument master. Missing or ambiguous
mappings fail instead of silently selecting an instrument.

## Local endpoints

All published ports bind to loopback by default.

| Service | Default URL | Purpose |
|---|---|---|
| Web | <http://127.0.0.1:3000> | Operator UI prototype |
| API health | <http://127.0.0.1:8000/health> | Liveness/readiness |
| PostgreSQL | `127.0.0.1:5432` | Local administration only |

Use a TLS reverse proxy, VPN or SSH tunnel for remote access. Do not expose PostgreSQL directly.

## Development

### Python

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

pytest
ruff check .
mypy src
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy src
```

### Web

```bash
cd web
npm install
npm run lint
npm run build
npm run dev
```

Open <http://127.0.0.1:3000>.

### Database migrations

```bash
export DATABASE_URL='postgresql://nanodelta:password@127.0.0.1:5432/nanodelta'
nanodelta-migrate
```

The runner uses a PostgreSQL advisory lock, records applied versions and SHA-256 checksums, and
refuses to continue if an already-applied migration was edited.

## Testing

| Test area | Representative coverage |
|---|---|
| ETL | Bronze-first persistence, canonical validation, settled candles, Gold determinism |
| Persistence | migrations, checksums, market isolation and idempotency |
| Providers | payload normalization, pagination, routing and injected transports |
| History | watermarks, overlap, coverage and targeted repair |
| Strategy | identity, validation, approval, expiry and revocation |
| Decisions | staged scoring, selection, rejection and decision ledger |
| Risk/paper | limits, idempotent fills, positions and outcomes |
| Operations | API scope, authentication, idempotency and lifecycle commands |
| FinOps | attribution, budgets, alerts and kill switch |
| Deployment | Docker/Compose and secret-mount contracts |

Provider unit tests use injected transports and do not require real credentials. Credentialed
provider/database integration must be opt-in and must never run with production order authority.

## Deployment and recovery

The Compose foundation follows these rules:

- database persists in the named `timescale_data` volume;
- migrations must succeed before API startup;
- API and web containers use read-only filesystems where practical;
- published ports bind to `127.0.0.1`;
- secrets are mounted read-only;
- containers use `no-new-privileges`.

Backup:

```bash
bash scripts/backup.sh
```

Restore into a controlled environment:

```bash
bash scripts/restore.sh
```

Always validate a restore before declaring backups operational. A backup script existing in the
repository is not proof that recovery meets the target RPO/RTO.

See [Production deployment](docs/PRODUCTION_DEPLOYMENT.md).

## Production-readiness status

| Capability | Status on `main` | Required proof or follow-up |
|---|---|---|
| Bronze/Silver/Gold contracts | Implemented | Provider-entitled production data verification |
| TimescaleDB migrations | Implemented | Deployed schema and retention inspection |
| Historical provider clients | Implemented | Credentialed multi-market E2E runs |
| Realtime provider operation | Partial/integration work | Sustained sessions and failover evidence |
| Strategy registry and validator | Implemented | More real strategies and approved artifacts |
| Deterministic risk and paper engine | Implemented | Real provider-to-outcome paper session |
| FastAPI operations | Implemented foundation | Full runtime composition and role separation |
| Next.js UI | Visual prototype | Authoritative API integration and authentication |
| Docker Compose | Implemented foundation | Target-host deployment evidence |
| Backup/restore scripts | Implemented | Timed recovery drill |
| CI/CD | Follow-up PR stream | Green checks and approved deployment |
| Observability | Follow-up PR stream | Metrics, alerts, receiver and monitoring run |
| Load/latency/soak/failover | Follow-up PR stream | Published acceptance evidence |

## Detailed project structure

```text
NanoDelta/
├── .github/
│   └── workflows/                    # CI/CD workflows land through the integration stream
├── config/
│   └── nse/
│       └── symbols.example.csv       # Safe NSE universe template
├── deploy/
│   └── api-entrypoint.sh             # API container startup and DB connection assembly
├── docs/
│   ├── README.md                     # Documentation index and non-negotiable rules
│   ├── ARCHITECTURE.md               # Target system boundaries and ownership
│   ├── ETL_ENGINES.md                # Bronze/Silver/Gold and market-data engines
│   ├── DATABASE_AND_INCREMENTAL_LOAD.md
│   │                                  # Schemas, grains, watermarks and backfill
│   ├── NSE_SYMBOLS_AND_DHAN_AUTH.md   # Universe and protected PIN/TOTP setup
│   ├── STRATEGY_AND_AGENTS.md         # Validation and advisory-agent boundaries
│   ├── STAGED_DECISION_PIPELINE.md    # Candidate scoring and portfolio construction
│   ├── QWEN_FINOPS.md                 # Cost attribution, budgets and kill switch
│   ├── UI_LAST.md                     # UI principles and page responsibilities
│   ├── PRODUCTION_DEPLOYMENT.md       # Deployment and recovery foundation
│   ├── IMPLEMENTATION_ROADMAP.md      # Checkpoints and definitions of done
│   └── assets/
│       ├── nanodelta-banner.svg       # Repository header artwork
│       └── nanodelta-architecture.svg # End-to-end architecture visual
├── env/
│   ├── .env.example                  # Shared environment template
│   ├── .env.production.example       # Compose deployment template
│   ├── .env.nse.example              # Dhan and TrueData
│   ├── .env.forex.example            # OANDA practice
│   ├── .env.crypto.example           # OKX and Poloniex
│   └── .env.qwen.example             # Qwen and FinOps
├── migrations/
│   ├── 0001_timescaledb_foundation.sql
│   ├── 0002_strategy_and_agent_governance.sql
│   ├── 0003_paper_execution_and_outcomes.sql
│   ├── 0004_history_and_operations.sql
│   ├── 0005_qwen_finops.sql
│   └── 0006_staged_decision_pipeline.sql
├── scripts/
│   ├── backup.sh                     # PostgreSQL backup hook
│   ├── restore.sh                    # Controlled restore hook
│   └── verify-deployment.sh          # Post-deployment verification
├── secrets/
│   └── README.md                     # Required secret files; values are never committed
├── src/
│   └── nanodelta/
│       ├── __init__.py
│       ├── contracts.py              # Market/provider enums and Bronze/Silver/Gold records
│       ├── storage.py                # Idempotent local file-lake boundary
│       ├── pipeline.py               # Bronze → Silver → Gold orchestration
│       ├── features.py               # Deterministic Gold features
│       ├── decisions.py              # Append-only staged decision contract
│       ├── decisions_postgres.py     # Durable decision ledger
│       ├── agents/
│       │   └── tradingagents.py      # Bounded advisory-only upstream adapter
│       ├── api/
│       │   ├── app.py                # Market-scoped FastAPI application
│       │   └── runtime.py            # Environment and service composition
│       ├── finops/
│       │   ├── core.py               # Usage, pricing, budgets, alerts and kill switch
│       │   └── qwen.py               # Guarded OpenAI-compatible Qwen gateway
│       ├── history/
│       │   ├── engine.py             # Backfill, incremental sync, coverage and repair
│       │   ├── postgres.py           # Durable watermarks, runs and coverage
│       │   └── timeframes.py         # Settled boundaries and market calendars
│       ├── markets/
│       │   └── adapters.py           # Provider payload → canonical contract adapters
│       ├── operations/
│       │   ├── controller.py         # Worker commands, authz, idempotency and audit
│       │   └── postgres.py           # Atomic worker-state and audit persistence
│       ├── orchestration/
│       │   ├── decision_pipeline.py  # Generate, score, review, allocate and revalidate
│       │   └── paper_batch.py        # Deterministic risk-to-paper batch handoff
│       ├── outcomes/
│       │   └── learning.py           # Closed outcomes and offline review evidence
│       ├── paper/
│       │   └── execution.py          # Idempotent paper orders, fills and positions
│       ├── persistence/
│       │   ├── cli.py                # `nanodelta-migrate` entrypoint
│       │   ├── migrations.py         # Advisory-lock/checksum migration runner
│       │   └── postgres.py           # Market-isolated PostgreSQL record store
│       ├── providers/
│       │   ├── base.py               # History/realtime/capability contracts
│       │   ├── transports.py         # HTTP and reconnecting stream transports
│       │   ├── registry.py           # Capability-specific provider routing
│       │   ├── dhan_auth.py          # Protected-file PIN/TOTP token generation
│       │   ├── dhan.py               # NSE historical/realtime client
│       │   ├── truedata.py           # NSE alternative/realtime client
│       │   ├── oanda.py              # Forex client
│       │   ├── okx.py                # Crypto primary client
│       │   └── poloniex.py           # Crypto fallback client
│       ├── risk/
│       │   └── engine.py             # Pure deterministic final risk authority
│       ├── strategies/
│       │   ├── registry.py           # Definitions, approvals, expiry and revocation
│       │   ├── runtime.py            # Runtime strategy interfaces
│       │   └── validation.py         # Cost/walk-forward/drawdown/statistical gates
│       └── universe/
│           └── nse.py                # CSV loading and Dhan instrument-ID resolution
├── tests/
│   ├── test_pipeline.py
│   ├── test_persistence.py
│   ├── test_provider_clients.py
│   ├── test_history_engine.py
│   ├── test_nse_universe_and_dhan_auth.py
│   ├── test_strategy_registry.py
│   ├── test_tradingagents_adapter.py
│   ├── test_staged_decision_pipeline.py
│   ├── test_risk_and_paper_execution.py
│   ├── test_outcomes_and_learning.py
│   ├── test_api_and_operations.py
│   ├── test_qwen_finops.py
│   └── test_deployment_foundation.py
├── web/
│   ├── app/
│   │   ├── page.tsx                  # Multi-market operations UI prototype
│   │   ├── layout.tsx
│   │   └── globals.css
│   ├── package.json
│   ├── next.config.ts
│   ├── eslint.config.mjs
│   └── tsconfig.json
├── Dockerfile.api                    # Python API/migration image
├── Dockerfile.web                    # Next.js production image
├── docker-compose.yml                # DB, migrate, API and web topology
├── pyproject.toml                    # Python package, tooling and CLI entrypoints
├── uv.lock                           # Locked Python dependency graph
├── AGENTS.md                         # Repository contribution rules for agents
├── LICENSE
└── README.md
```

## Documentation

1. [Documentation map](docs/README.md)
2. [Architecture](docs/ARCHITECTURE.md)
3. [ETL and market-data engines](docs/ETL_ENGINES.md)
4. [Database and incremental loading](docs/DATABASE_AND_INCREMENTAL_LOAD.md)
5. [NSE symbols and Dhan authentication](docs/NSE_SYMBOLS_AND_DHAN_AUTH.md)
6. [Strategy governance and TradingAgents](docs/STRATEGY_AND_AGENTS.md)
7. [Staged decision pipeline](docs/STAGED_DECISION_PIPELINE.md)
8. [Qwen FinOps](docs/QWEN_FINOPS.md)
9. [UI final-phase contract](docs/UI_LAST.md)
10. [Production deployment foundation](docs/PRODUCTION_DEPLOYMENT.md)
11. [Implementation roadmap](docs/IMPLEMENTATION_ROADMAP.md)

## Contribution rules

Changes should preserve the repository’s non-negotiable contracts:

- no provider-specific symbol or payload leakage beyond adapters;
- no incomplete candle in Silver or Gold;
- no unapproved strategy in runtime;
- no agent authority over risk or execution;
- no live-order capability hidden behind a flag;
- no secret committed to Git;
- no UI metric presented as authoritative without a backend source;
- no documentation claim without code and test evidence.

Run Python tests, Ruff and strict mypy for backend changes. Run lint and a production build for web
changes. Database changes require a new forward migration; never edit an applied migration.

## Safety boundary

NanoDelta is provided for engineering research and paper trading. It is not investment advice.
Market data entitlements, exchange rules, tax obligations and regulatory requirements remain the
operator’s responsibility.

Any future live execution capability must be a separately reviewed project with broker
reconciliation, kill switches, position recovery, credential isolation, incident response,
operational approvals and jurisdiction-specific compliance.
