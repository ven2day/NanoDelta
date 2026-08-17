"""Real, data-backed per-symbol tradeability screening -- pipeline stage 2.

Only checks computable from data this system actually has (settled candle
history: price, volume, ATR, gap) are implemented. Circuit-limit and bid/ask
spread/slippage checks are deliberately absent: there is no real circuit-band or
order-book data source wired up yet, and a fabricated threshold for either would
be worse than no check at all -- it would look like real protection while
actually being invented.
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

    def __post_init__(self) -> None:
        positive = (
            self.minimum_price,
            self.minimum_average_volume,
            self.minimum_average_traded_value,
            self.minimum_atr_pct,
            self.maximum_atr_pct,
            self.maximum_gap_pct,
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
    return True, "TRADEABLE"
