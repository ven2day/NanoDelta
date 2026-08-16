# Initial replaceable strategies and validation

NanoDelta ships three deterministic strategy plugins as research implementations. They do not
have order authority and are not registered as `PAPER_APPROVED` by default.

| Strategy | Markets | Default inputs | Current status |
|---|---|---|---|
| VWAP pullback | NSE, Crypto | settled OHLCV with real traded volume | RESEARCH |
| EMA + RSI momentum | NSE, Forex, Crypto | settled OHLC | RESEARCH |
| SuperTrend + ADX | NSE, Forex, Crypto | settled OHLC | RESEARCH |

VWAP is intentionally unavailable for Forex because OANDA candle volume is activity/tick volume,
not centralized traded volume. Each market/timeframe combination is a distinct immutable
`StrategyIdentity`; approving NSE 15m never approves Forex or Crypto.

## No-lookahead contract

Plugins receive a chronological tuple of `ClosedBar` records. `StrategyContext` rejects a bar at or
after the decision timestamp. Offline replay calculates a signal after bar N, fills no earlier than
bar N+1 open, charges fees and slippage on both sides, and assumes the stop is hit first if an OHLC
bar contains both stop and target.

## Validation and promotion

Replay produces cost-adjusted trades, chronological walk-forward window results, drawdown, and a
two-sided sign-test p-value. `validate_strategy` applies sample-size, window stability, expectancy,
drawdown, and Bonferroni family-wise error gates.

Validation output is content-addressed using source-data lineage, code revision, identity, policy,
and metrics. Artifact files use exclusive creation and cannot be overwritten with different
content. Promotion is explicit:

1. `RESEARCH`: implementation or parameters may be investigated as a new version.
2. `SHADOW`: may emit decisions for observation, but cannot create paper orders.
3. `PAPER_APPROVED`: requires an exact-identity passing validation artifact and a separately
   recorded, named, expiring manual approval.

A failed validation cannot be promoted by the registry. A parameter change requires a new strategy
version and new validation. Nothing in this package performs live execution.

## Fixture evidence

The deterministic oscillating fixture in `tests/test_initial_strategies_and_validation.py` checks
mechanics and failure behavior; it is not market evidence. None of the three strategies is declared
profitable or paper-approved from synthetic data. Before shadow or paper promotion, validate each
exact identity against versioned, recorded provider data representative of its market and include
fees, spread, slippage, missing-feed periods, and out-of-sample dates.
