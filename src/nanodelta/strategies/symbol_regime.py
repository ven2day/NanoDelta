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

    def __post_init__(self) -> None:
        if self.adx_no_trend <= 0 or self.adx_strong_trend <= self.adx_no_trend:
            raise ValueError("adx_strong_trend must be above adx_no_trend, which must be positive")
        if self.minimum_fit <= 0 or self.maximum_fit <= self.minimum_fit:
            raise ValueError("maximum_fit must be above minimum_fit, which must be positive")
        if not 0 < self.misaligned_penalty <= 1:
            raise ValueError("misaligned_penalty must be in (0, 1]")


def evaluate_symbol_regime(
    features: Mapping[str, float], limits: SymbolRegimeLimits
) -> tuple[float, str]:
    """Pure, deterministic trend-fit score for regime.symbol_fit.

    Scales ADX-14 between the no-trend and strong-trend thresholds into
    [minimum_fit, maximum_fit], then discounts that score when EMA-9/21
    slope disagrees with the SuperTrend direction -- two real, already-
    computed indicators disagreeing is itself real evidence the symbol's
    trend is not clean right now.
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
    return fit, label
