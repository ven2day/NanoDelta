"""Real, breadth-based market/sector regime -- pipeline stages 3 and 4.

There is no live NIFTY or NIFTY-sector index feed wired into this system: adding
one means subscribing Dhan on a different exchange segment (indices trade on
IDX_I, equities on NSE_EQ), which is a materially bigger and riskier change than
is safe to make right before a live session starts. Instead, both regime
classifications are computed from breadth across the already-tracked universe's
own settled candle history -- the fraction of symbols in a group trending up vs
down, and their average trend strength (ADX-14, the same real indicator used by
symbol_regime.py). That is a real, not fabricated, signal; it is a coarser proxy
for "the market" than a literal index would be, and it cannot detect a genuine
risk-off flight-to-safety state (that needs a volatility index or defensive-sector
rotation signal this system doesn't have), so RISK-OFF is never classified.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from nanodelta.contracts import Market
from nanodelta.persistence.migrations import Connection
from nanodelta.runtime.technical_context import fetch_settled_candles
from nanodelta.strategies import materialize_technical_features


@dataclass(frozen=True)
class RegimeBreadth:
    label: str
    fit: float


def fetch_breadth_inputs(
    connection: Connection, market: Market, symbols: Sequence[str], timeframe: str
) -> tuple[dict[str, float], dict[str, bool]]:
    """One indicator snapshot per symbol -- the same warm-window computation
    technical_context.py already does for a single symbol, just looped across a
    group. Symbols without enough settled history to warm up are silently
    excluded, the same "not yet ready" contract used everywhere else in this
    pipeline rather than a fabricated placeholder."""
    adx_by_symbol: dict[str, float] = {}
    bullish_by_symbol: dict[str, bool] = {}
    for symbol in symbols:
        candles = fetch_settled_candles(connection, market, symbol, timeframe)
        if not candles:
            continue
        snapshots = materialize_technical_features(candles)
        if not snapshots:
            continue
        values = snapshots[-1].values
        adx_by_symbol[symbol] = values["adx_14"]
        bullish_by_symbol[symbol] = values["ema_9"] > values["ema_21"]
    return adx_by_symbol, bullish_by_symbol


def classify_breadth(
    adx_by_symbol: Mapping[str, float],
    bullish_by_symbol: Mapping[str, bool],
    *,
    minimum_symbols: int = 5,
    trending_adx: float = 22.0,
    bullish_majority: float = 0.6,
    bearish_majority: float = 0.4,
    ranging_fit: float = 0.7,
    trending_fit: float = 1.15,
    volatile_fit: float = 0.6,
) -> RegimeBreadth:
    symbols = set(adx_by_symbol) & set(bullish_by_symbol)
    if len(symbols) < minimum_symbols:
        return RegimeBreadth("INSUFFICIENT_BREADTH", 1.0)
    average_adx = sum(adx_by_symbol[s] for s in symbols) / len(symbols)
    bullish_fraction = sum(1 for s in symbols if bullish_by_symbol[s]) / len(symbols)
    if average_adx < trending_adx:
        return RegimeBreadth("RANGING", ranging_fit)
    if bullish_fraction >= bullish_majority:
        return RegimeBreadth("TRENDING_UP", trending_fit)
    if bullish_fraction <= bearish_majority:
        return RegimeBreadth("TRENDING_DOWN", trending_fit)
    return RegimeBreadth("VOLATILE", volatile_fit)
