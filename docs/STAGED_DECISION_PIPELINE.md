# Staged decision pipeline

NanoDelta separates position management from new entries and separates entry processing into three
planes. Market, sector, symbol, and multi-timeframe regime evidence changes candidate ranking; it is
not a universal veto.

```text
market event
  ├─ position management (always first; entry gates cannot suppress exits)
  └─ Plane A: factual entry eligibility and deterministic candidate generation
       └─ Plane B: expected-R scoring, including regime/MTF/history/ML/cost terms
            └─ optional Qwen review: OFF, SHADOW, or ENFORCED_VETO
                 └─ Plane C: batch selection and sizing under portfolio constraints
                      └─ entry drift and reward/risk revalidation
                           └─ deterministic risk → paper execution
```

## Adding or changing a strategy

A runtime strategy implements `StrategyPlugin`:

```python
class StrategyPlugin(Protocol):
    definition: StrategyDefinition

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]: ...
    def generate(self, context: StrategyContext) -> StrategySignal | None: ...
```

`compatibility` is for factual incompatibility: unsupported market/timeframe, unavailable required
features, or another requirement intrinsic to the implementation. Ordinary regime disagreement
belongs in `RegimeEvidence` and changes the expected-R score instead of suppressing the strategy.

To deploy a future strategy:

1. publish a new immutable `StrategyDefinition` and exact `StrategyIdentity`;
2. implement and unit-test its plugin without changing the orchestrator;
3. register the definition and plugin;
4. record its cost-aware validation artifact;
5. grant a current approval for paper evaluation.

Changing implementation or parameters requires a new strategy version. The pipeline runs every
approved compatible plugin for each configured `(market, symbol, timeframe, horizon, feature set)`
grain.

## Scoring

```text
expected_r_net_of_costs
  = strategy_confidence
    × market_regime_fit
    × sector_regime_fit
    × symbol_regime_fit
    × mtf_alignment
    + historical_expectancy_r
    + bounded_ml_tilt_r
    - estimated_cost_r
```

Every term and final score is recorded in the decision ledger. Learned/regime terms never determine
position size. Position size is derived from equity risk and stop distance, capped by order and batch
notional constraints.

## Qwen modes

- `OFF`: no request; deterministic candidates continue.
- `SHADOW`: verdict recorded but cannot change the batch.
- `ENFORCED_VETO`: only `BLOCK` removes a candidate.
- `ABSTAIN`, timeout, provider error, budget exhaustion, and Qwen kill-switch fail open for the
  deterministic pipeline and are recorded.

Qwen cannot originate a candidate, reverse BUY/SELL, size a position, or bypass risk.

## Portfolio construction and entry revalidation

The default deterministic allocator ranks positive expected-R candidates and reserves position risk,
order notional, total new notional, sector capacity, symbol capacity, and pairwise correlation in one
batch. The allocator contract can later be replaced by a constrained optimiser without changing any
strategy plugin.

Before risk/execution, the current quote is checked against the settled signal price. Excessive drift,
invalid stop/target geometry, or degraded reward/risk rejects the entry.

## Decision ledger

Every stage writes a stable reason code to `control.decision_events`. The
`control.decision_funnel` view aggregates each cycle by market, stage, status, and reason. The API
exposes a complete cycle at `GET /api/decision-cycles/{cycle_id}`.

Examples include `MISSING_BARS`, `MIN_TRADED_VALUE`, `NO_APPROVED_STRATEGY`, `NO_TRIGGER`,
`NON_POSITIVE_EXPECTED_R`, `LLM_BLOCK`, `SECTOR_CONCENTRATION`, `CORRELATION_LIMIT`,
`ENTRY_DRIFT_EXCEEDED`, `RISK_APPROVED`, and `PAPER_ORDER_CREATED`.
