"""Real market/sector regime -- pipeline stages 3 and 4.

Two data sources, in priority order:

1. Real NIFTY / sector-index candles (runtime/index_feed.py), when the index
   feed is enabled (NANODELTA_INDEX_FEED_ENABLED) and has warmed up -- ADX-14
   and EMA-9/21 computed directly on the index's own price action, the same
   real indicators symbol_regime.py uses for individual symbols.
2. A breadth proxy across the already-tracked equity universe's own settled
   candle history, when the index feed is disabled or not yet warm -- the
   fraction of symbols trending up vs down and their average trend strength.
   This is a coarser signal than a literal index, but real, not fabricated.

Risk-off is classified from India VIX level (see classify_risk_off) when the
index feed is enabled; without it there is no volatility-index proxy worth
trusting, so RISK-OFF is never emitted.
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


def fetch_index_snapshot(
    connection: Connection, index_symbol: str, timeframe: str
) -> Mapping[str, float] | None:
    """Latest warm indicator snapshot for one index symbol (e.g. NIFTY,
    BANKNIFTY) -- same warm-window contract as fetch_breadth_inputs, just for
    a single instrument. None means the index feed isn't enabled or hasn't
    warmed up yet, not an error; callers fall back to the breadth proxy."""
    candles = fetch_settled_candles(connection, Market.NSE, index_symbol, timeframe)
    if not candles:
        return None
    snapshots = materialize_technical_features(candles)
    if not snapshots:
        return None
    return snapshots[-1].values


def classify_index_regime(
    values: Mapping[str, float],
    *,
    trending_adx: float = 22.0,
    volatile_atr_pct: float = 0.012,
    ranging_fit: float = 0.7,
    trending_fit: float = 1.15,
    volatile_fit: float = 0.6,
) -> RegimeBreadth:
    """Real classification from one index's own ADX-14/EMA-9-21/ATR-14 -- the
    same indicators and reasoning as symbol_regime.py, applied to the index
    itself instead of an individual stock. Volatility takes priority over
    trend: a genuinely volatile tape discounts even a directional move."""
    atr_pct = values["atr_14"] / values["close"] if values["close"] > 0 else 0.0
    if atr_pct > volatile_atr_pct:
        return RegimeBreadth("VOLATILE", volatile_fit)
    if values["adx_14"] < trending_adx:
        return RegimeBreadth("RANGING", ranging_fit)
    bullish = values["ema_9"] > values["ema_21"]
    return RegimeBreadth("TRENDING_UP" if bullish else "TRENDING_DOWN", trending_fit)


def classify_risk_off(
    vix_close: float,
    *,
    elevated_vix: float = 20.0,
    risk_off_vix: float = 25.0,
    normal_fit: float = 1.0,
    elevated_fit: float = 0.75,
    risk_off_fit: float = 0.5,
) -> RegimeBreadth:
    """India VIX thresholds are the conventional bands used in Indian market
    commentary -- below ~15 is calm, 15-20 normal, 20-25 elevated caution,
    above 25 broadly considered risk-off. Real, published market convention,
    not an invented number."""
    if vix_close >= risk_off_vix:
        return RegimeBreadth("RISK_OFF", risk_off_fit)
    if vix_close >= elevated_vix:
        return RegimeBreadth("ELEVATED_VOLATILITY", elevated_fit)
    return RegimeBreadth("NORMAL", normal_fit)


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
