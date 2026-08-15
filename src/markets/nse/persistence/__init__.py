"""NSE-only repository factories.

The market/provider identity is bound here and cannot be supplied by a caller.
"""

from typing import Any

import pandas as pd

from src.core.market_data import SchemaBoundRawMarketRepository
from src.core.models import MarketProvider
from src.core.persistence import SchemaBoundCandleRepository, SchemaBoundTradingRepository
from src.markets.nse.market_data.history_manager import resample_nse_ohlcv


def _load_nse_analysis_frame(
    store: Any,
    symbol: str,
    timeframe: str,
    **kwargs: Any,
) -> pd.DataFrame:
    """Load native candles or derive NSE-session-aligned 30m/4h candles."""

    bars = kwargs.pop("bars", None)
    complete_only = bool(kwargs.pop("complete_only", True))
    source_timeframe, rule, factor = {
        "30m": ("15m", "30min", 2),
        "4h": ("1h", "4h", 4),
    }.get(timeframe, (timeframe, "", 1))
    source_bars = bars * factor if bars is not None else None
    frame = store.load_frame(
        symbol,
        source_timeframe,
        bars=source_bars,
        complete_only=complete_only,
        market="NSE",
        provider="DHAN",
        **kwargs,
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    if not rule:
        return frame
    derived = resample_nse_ohlcv(frame, rule)
    return derived.tail(bars) if bars is not None else derived


def bind_candle_repository(store: Any) -> SchemaBoundCandleRepository:
    return SchemaBoundCandleRepository(
        store,
        market="NSE",
        provider="DHAN",
        analysis_loader=_load_nse_analysis_frame,
    )


def bind_trading_repository(engine: Any) -> SchemaBoundTradingRepository:
    return SchemaBoundTradingRepository(engine, market="NSE", provider="DHAN")


def bind_raw_market_repository(engine: Any) -> SchemaBoundRawMarketRepository:
    return SchemaBoundRawMarketRepository(
        engine,
        market="NSE",
        provider=MarketProvider.DHAN,
    )


__all__ = ["bind_candle_repository", "bind_raw_market_repository", "bind_trading_repository"]
