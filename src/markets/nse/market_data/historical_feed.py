"""Historical-candle feed selection: DhanHQ, gated by enable_dhan_historical_data.

Single place that decides where OHLCV history comes from, so HistoryManager,
SectorMoversTracker, and ScalpingScreenerTracker don't each need their own
Dhan wiring. Returns None on any failure (disabled, init failure, or a failed
fetch) rather than raising — matching this codebase's "never let a
data-source failure propagate" convention.
"""

import logging
from typing import Protocol

import pandas as pd

from src.config import get_settings
from src.core.market_data import RawEventSink
from src.markets.nse.broker.dhan.historical import DhanHistoricalFeed

logger = logging.getLogger(__name__)


class HistoricalFeed(Protocol):
    """Structural type satisfied by DhanHistoricalFeed and this module's
    HistoricalDataFeed — whatever fetch_timeframe_history() and similar
    one-off callers actually need from a feed."""

    def get_historical(
        self, symbol: str, period: str = "1mo", interval: str = "1d"
    ) -> pd.DataFrame | None: ...


class HistoricalDataFeed:
    """Thin wrapper around DhanHistoricalFeed, gated by
    enable_dhan_historical_data. Returns None (never raises) if disabled or
    if DhanHistoricalFeed fails to initialize or fetch."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        *,
        raw_event_sink: RawEventSink | None = None,
    ):
        settings = get_settings()
        self._dhan: DhanHistoricalFeed | None = None
        if settings.enable_dhan_historical_data:
            try:
                self._dhan = DhanHistoricalFeed(
                    symbols=symbols,
                    raw_event_sink=raw_event_sink,
                )
            except Exception:
                logger.warning("Failed to initialize DhanHistoricalFeed", exc_info=True)

    def get_historical(
        self, symbol: str, period: str = "1mo", interval: str = "1d"
    ) -> pd.DataFrame | None:
        if self._dhan is None:
            return None
        return self._dhan.get_historical(symbol, period=period, interval=interval)

    @property
    def is_available(self) -> bool:
        """Whether this process is configured to fetch DhanHQ history."""
        return self._dhan is not None
