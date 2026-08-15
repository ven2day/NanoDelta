# DeltaQuant six-layer architecture

This document maps the code on GitHub `master` at commit `f6e0c5f` to the shared
pipeline contract in `src/core/pipeline`. It is an implementation inventory, not a
claim that every layer is complete.

## Platform rule

NSE, Forex, and Crypto use the same six layers and the same broker-neutral contracts.
Each market retains its own provider adapters, sessions, costs, risk policy, execution
adapter, persistence schema, configuration, and model namespace.

```text
Provider -> Raw -> Canonical -> Feature -> Decision -> Execution -> Outcome
                                     ^                              |
                                     +------ learning feedback -----+
```

ML and agents are processors, not storage layers:

- ML inference reads Feature and writes evidence into Decision.
- Trading agents read Feature/Decision and write evidence into Decision.
- deterministic risk reads Decision/Execution state and writes approval or rejection
  into Decision;
- only the Execution Engine may write Execution;
- ML training reads Feature and Outcome and publishes versioned model artifacts.

These permissions are executable in `src/core/pipeline/layers.py`.

## Existing-code inventory

| Layer | Existing implementation on GitHub master | Current gap |
|---|---|---|
| Raw/Bronze | `src/core/market_data/raw.py`, schema-bound raw repository, Dhan REST/WebSocket and OANDA candle/pricing emission | GitHub-master Crypto has no provider to emit raw events |
| Canonical/Silver | `src/core/models`, canonical quality validation, `src/core/candles.py`, market history managers and schema-bound candle persistence | Canonical contracts do not yet cover every trade/order-book type |
| Feature/Gold | `src/core/indicators`, `src/core/features`, feature snapshots and market-relative context | Feature materialization is primarily runtime/in-memory rather than a formal feature repository |
| Decision | `src/core/candidates`, `src/core/aggregation`, `src/agents`, eligibility, ML registry/inference, Qwen policy and deterministic risk | Decision data is split across signal payloads, agent state and market persistence rather than one versioned contract |
| Execution | NSE execution service, paper engine, lifecycle, journal and preflight; Forex lifecycle/paper positions | Forex execution is embedded in its runtime; Crypto has no execution implementation on master |
| Outcome | Trade lifecycle ledger, performance tracking, memory/analyzer feedback and attribution fields | No shared cross-market Outcome Engine contract/repository yet |

## Market ownership

| Responsibility | Shared core | NSE | Forex | Crypto on master |
|---|---|---|---|---|
| Provider ingestion | Provider protocols | Dhan | OANDA | Unconfigured/fail-closed |
| Sessions | Common identity only | NSE calendar and IST | 24x5 Forex calendar | Placeholder |
| Canonical data | Candle, quote and instrument contracts | NSE history/quotes | OANDA normalization/history | Persistence scaffold only |
| Features/strategies | Shared indicators, features and strategies | NSE configuration | Forex configuration | No configured runtime |
| ML | Shared artifact and registry contracts | NSE namespace | Forex namespace | Empty namespace |
| Risk | Shared boundary vocabulary | NSE costs, guards, sizing and pretrade | Forex costs/model | Empty policy package |
| Execution | Shared layer authority | Full paper/broker-shaped service | Runtime-owned paper lifecycle | Not implemented |
| Persistence | Schema-bound repositories | `nse` schema | `forex` schema | `crypto` scaffold |

## Target package ownership

The target does not require three copies of the engines:

```text
src/core/
  pipeline/       six-layer vocabulary, lineage and processor boundaries
  market_data/    shared ingestion, normalization and quality contracts
  features/       shared feature engine contracts
  decisions/      candidate, evidence and final-decision contracts
  execution/      broker-neutral order/fill/position contracts
  outcomes/       performance and attribution contracts

src/markets/{nse,forex,crypto}/
  providers/      provider-specific extraction and normalization
  market_data/    market calendars, routing, reconciliation and backfill
  strategies/     market-specific configuration/eligibility
  risk/           market-specific costs and limits
  execution/      broker or exchange adapters and paper execution
  runtime/        process lifecycle and orchestration only
```

Existing packages will move only when a focused checkpoint can preserve imports and
behavior. Directory renaming is not itself architecture progress.

## Delivery checkpoints

1. **Foundation:** shared layer vocabulary, lineage, legal transitions and processor
   permissions. No trading behavior changes.
2. **Raw/Canonical:** immutable raw-event persistence plus explicit normalization and
   quality contracts, introduced provider by provider.
3. **Feature:** one feature snapshot/materialization contract for all markets.
4. **Decision:** one candidate/evidence/final-decision contract; ML and agents remain
   advisory, deterministic risk remains authoritative.
5. **Execution:** broker-neutral order lifecycle with market-owned adapters; paper-only
   remains the default and current safety gates remain intact.
6. **Outcome:** cross-market performance, attribution and learning contract feeding
   offline ML training without creating an execution bypass.
7. **UI/operations:** one common terminal shell with isolated NSE, Forex and Crypto
   workspaces displaying the same layer lifecycle and market-specific details.

Each checkpoint must add tests, preserve market isolation, and report pre-existing
failures separately from regressions.
