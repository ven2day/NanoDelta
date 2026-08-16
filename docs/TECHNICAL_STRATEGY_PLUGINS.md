# Deterministic technical strategy plugins

This module provides three paper-trading strategy families over declared Gold
feature-set version 2:

- VWAP Pullback: NSE and Crypto, 5-minute bars.
- EMA(9)/EMA(21) with RSI(14) continuation: NSE, Forex and Crypto, 15-minute bars.
- SuperTrend(ATR 14 × 3) with ADX(14): NSE 15-minute, Forex 1-hour and Crypto
  15-minute bars.

`materialize_technical_features()` is a pure reference implementation. It sorts
settled candles, ignores forming bars, uses Wilder smoothing for RSI/ATR/ADX,
seeds EMA values from historical simple averages, and resets VWAP at the UTC
session boundary. Snapshots are emitted only after all indicators are warm.
Appending future candles does not change an earlier snapshot.

Each plugin declares its exact required feature names and exact feature-set
version through its identity and `required_features`. Compatibility rejects a
market, timeframe, horizon, feature version, or feature payload mismatch.

The factory `technical_strategies()` creates immutable definitions and plugin
instances only. It does not create validation results or approvals. These
strategies must undergo cost-aware out-of-sample validation and explicit operator
approval before the existing admission layer can execute them. Deterministic
fixtures verify calculations and signal rules; they are not evidence of
profitability or provider readiness.
