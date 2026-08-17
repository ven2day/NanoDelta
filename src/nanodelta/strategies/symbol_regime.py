"""Real, indicator-backed per-symbol regime scoring -- pipeline stage 5.

Every strategy currently registered (VWAP pullback, EMA/RSI continuation,
SuperTrend/ADX) is a trend-following strategy: each wants price making a
real directional move, not chopping sideways. ADX-14 is the standard
measure of trend strength, so it is the real signal used here -- there is
no separate NIFTY/sector regime feed to draw on yet (see tradeability.py
for the same reasoning applied to liquidity/volatility screening), so
market_fit and sector_fit stay at their neutral RegimeEvidence default
rather than being invented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolRegimeLimits:
    adx_no_trend: float
    adx_strong_trend: float
    minimum_fit: float
    maximum_fit: float
    misaligned_penalty: float = 0.7
    minimum_volume_ratio: float = 0.8
    low_volume_penalty: float = 0.8

    def __post_init__(self) -> None:
        if self.adx_no_trend <= 0 or self.adx_strong_trend <= self.adx_no_trend:
            raise ValueError("adx_strong_trend must be above adx_no_trend, which must be positive")
        if self.minimum_fit <= 0 or self.maximum_fit <= self.minimum_fit:
            raise ValueError("maximum_fit must be above minimum_fit, which must be positive")
        if not 0 < self.misaligned_penalty <= 1:
            raise ValueError("misaligned_penalty must be in (0, 1]")
        if self.minimum_volume_ratio <= 0:
            raise ValueError("minimum_volume_ratio must be positive")
        if not 0 < self.low_volume_penalty <= 1:
            raise ValueError("low_volume_penalty must be in (0, 1]")


def evaluate_symbol_regime(
    features: Mapping[str, float], limits: SymbolRegimeLimits
) -> tuple[float, str]:
    """Pure, deterministic trend-fit score for regime.symbol_fit.

    Scales ADX-14 between the no-trend and strong-trend thresholds into
    [minimum_fit, maximum_fit], then discounts that score when EMA-9/21 slope
    disagrees with the SuperTrend direction (two already-computed indicators
    disagreeing is itself real evidence the trend isn't clean) or when
    volume_ratio_20 -- also already computed, from materialize_technical_features
    -- shows below-average participation, since a trend without volume behind it
    is weaker evidence than one with it.
    """
    adx = features["adx_14"]
    if adx <= limits.adx_no_trend:
        trend_component, label = 0.0, "NO_TREND"
    elif adx >= limits.adx_strong_trend:
        trend_component, label = 1.0, "STRONG_TREND"
    else:
        span = limits.adx_strong_trend - limits.adx_no_trend
        trend_component = (adx - limits.adx_no_trend) / span
        label = "DEVELOPING_TREND"
    ema_bullish = features["ema_9"] > features["ema_21"]
    supertrend_bullish = features["supertrend_direction"] > 0
    aligned = ema_bullish == supertrend_bullish
    fit = limits.minimum_fit + (limits.maximum_fit - limits.minimum_fit) * trend_component
    if not aligned:
        fit *= limits.misaligned_penalty
        label = f"{label}_MISALIGNED"
    if features["volume_ratio_20"] < limits.minimum_volume_ratio:
        fit *= limits.low_volume_penalty
        label = f"{label}_LOW_VOLUME"
    return fit, label


def evaluate_mtf_alignment(
    aligned: bool | None,
    *,
    aligned_fit: float = 1.15,
    misaligned_fit: float = 0.75,
    unknown_fit: float = 1.0,
) -> float:
    """A symbol's own timeframe trending doesn't mean much if the next timeframe
    up disagrees -- real multi-timeframe confirmation, from the same EMA-9/21
    direction check symbol regime already does, just on a second timeframe. When
    the higher timeframe hasn't warmed up yet, this stays neutral (unknown_fit)
    rather than penalizing a symbol for data that simply isn't ready yet."""
    if aligned is None:
        return unknown_fit
    return aligned_fit if aligned else misaligned_fit
