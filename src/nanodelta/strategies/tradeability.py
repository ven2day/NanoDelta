"""Real, data-backed per-symbol tradeability screening -- pipeline stage 2.

Checks computable from settled candle history (price, volume, ATR, gap) run
unconditionally. Circuit-limit and spread checks run only when a quote
snapshot is supplied (see providers/dhan.py's fetch_quotes -- a REST poll
separate from the realtime feed): circuit limits and top-of-book bid/ask
genuinely aren't available from the settled-candle data alone, so these stay
skipped rather than faked when no snapshot is passed.

Corporate-action adjustment: Dhan's own daily historical data is already
split/bonus-adjusted server-side (per DhanHQ support docs), so daily-timeframe
history needs no separate adjustment layer. Intraday timeframes are not
documented as adjusted, so a mid-session split/bonus would show up as a real
price discontinuity -- see the reprice check below, which is a genuine safety
net for that case rather than a fabricated calendar of corporate-action dates
this system doesn't have access to.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from nanodelta.strategies.technical_features import TechnicalCandle

_BAR_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


@dataclass(frozen=True)
class TradeabilityLimits:
    minimum_price: float
    minimum_average_volume: float
    minimum_average_traded_value: float
    minimum_atr_pct: float
    maximum_atr_pct: float
    maximum_gap_pct: float
    average_window: int = 20
    maximum_bar_gap_multiple: float = 1.5
    maximum_reprice_pct: float = 0.30
    maximum_spread_pct: float = 0.01
    circuit_proximity_pct: float = 0.02

    def __post_init__(self) -> None:
        positive = (
            self.minimum_price,
            self.minimum_average_volume,
            self.minimum_average_traded_value,
            self.minimum_atr_pct,
            self.maximum_atr_pct,
            self.maximum_gap_pct,
            self.maximum_reprice_pct,
            self.maximum_spread_pct,
            self.circuit_proximity_pct,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("tradeability limits must be positive")
        if self.minimum_atr_pct >= self.maximum_atr_pct:
            raise ValueError("minimum_atr_pct must be below maximum_atr_pct")
        if self.average_window < 2:
            raise ValueError("average_window must be at least 2")
        if self.maximum_bar_gap_multiple <= 1:
            raise ValueError("maximum_bar_gap_multiple must be above 1")


def evaluate_tradeability(
    candles: Sequence[TechnicalCandle],
    atr_14: float,
    limits: TradeabilityLimits,
    *,
    timeframe: str | None = None,
    circuit_limits: tuple[float, float] | None = None,
    best_bid: float | None = None,
    best_ask: float | None = None,
) -> tuple[bool, str]:
    """Pure, deterministic screen over already-settled candles and an
    already-computed ATR (callers already have one warm; recomputing it here
    would just duplicate materialize_technical_features)."""
    settled = sorted((candle for candle in candles if candle.settled), key=lambda c: c.event_time)
    if len(settled) < 2:
        return False, "INSUFFICIENT_HISTORY"
    latest = settled[-1]
    previous = settled[-2]
    if timeframe is not None:
        expected_seconds = _BAR_SECONDS.get(timeframe)
        if expected_seconds is not None:
            observed_seconds = (latest.event_time - previous.event_time).total_seconds()
            if observed_seconds > expected_seconds * limits.maximum_bar_gap_multiple:
                return False, "MISSING_BAR_DETECTED"
    if latest.close <= 0:
        return False, "INVALID_PRICE"
    if latest.close < limits.minimum_price:
        return False, "BELOW_MINIMUM_PRICE"
    window = settled[-limits.average_window :]
    average_volume = sum(candle.volume for candle in window) / len(window)
    if average_volume < limits.minimum_average_volume:
        return False, "BELOW_MINIMUM_VOLUME"
    average_traded_value = sum(candle.close * candle.volume for candle in window) / len(window)
    if average_traded_value < limits.minimum_average_traded_value:
        return False, "BELOW_MINIMUM_TRADED_VALUE"
    atr_pct = atr_14 / latest.close
    if atr_pct < limits.minimum_atr_pct:
        return False, "RANGE_TOO_TIGHT"
    if atr_pct > limits.maximum_atr_pct:
        return False, "RANGE_TOO_WIDE"
    if previous.close > 0:
        gap_pct = abs(latest.open - previous.close) / previous.close
        if gap_pct > limits.maximum_gap_pct:
            return False, "GAP_TOO_WIDE"
    prior_window = window[:-1] if window[-1] is latest else window
    if prior_window:
        average_prior_close = sum(candle.close for candle in prior_window) / len(prior_window)
        if average_prior_close > 0:
            reprice_pct = abs(latest.close - average_prior_close) / average_prior_close
            if reprice_pct > limits.maximum_reprice_pct:
                return False, "PRICE_DISCONTINUITY_SUSPECTED"
    if circuit_limits is not None:
        lower, upper = circuit_limits
        if lower > 0 and upper > lower:
            band = upper - lower
            if latest.close - lower < band * limits.circuit_proximity_pct:
                return False, "NEAR_LOWER_CIRCUIT"
            if upper - latest.close < band * limits.circuit_proximity_pct:
                return False, "NEAR_UPPER_CIRCUIT"
    if best_bid is not None and best_ask is not None and best_bid > 0 and best_ask > best_bid:
        spread_pct = (best_ask - best_bid) / best_bid
        if spread_pct > limits.maximum_spread_pct:
            return False, "SPREAD_TOO_WIDE"
    return True, "TRADEABLE"
