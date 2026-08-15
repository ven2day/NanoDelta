"""
Historical Data Manager

Pre-fetches, caches, and maintains rolling OHLCV DataFrames for each symbol.
Enables real indicator calculation from actual price history instead of
fabricating indicators from a single price point.

Features:
- Pre-fetch historical data on startup via DhanHQ
- Append new quotes as intraday candles
- Configurable lookback and cache TTL
- Thread-safe data access
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from src.core.indicators import Timeframe
from src.core.market_data import RawEventSink
from src.core.paths import DATA_ROOT
from src.markets.nse.market_data.historical_feed import HistoricalDataFeed, HistoricalFeed
from src.markets.nse.sessions.calendar import get_nse_calendar
from src.markets.nse.sessions.market_time import (
    IST,
    MARKET_CLOSE,
    MARKET_OPEN,
    is_market_hours,
    now_ist,
)

if TYPE_CHECKING:
    from src.core.candles import CandleStore

logger = logging.getLogger(__name__)

# Minimum bars needed for indicator calculation
MIN_BARS_FOR_INDICATORS = 50
DEFAULT_LOOKBACK_PERIOD = "3mo"  # 3 months of daily data
MAX_INTRADAY_BARS = 500  # Max intraday bars to keep in memory

# Feed interval/lookback pairs. HistoryManager fetches M15 and H1 as base frames,
# then derives M30 and H4. The one-off fetch helper can still request native M30.
INTRADAY_YF_PARAMS: dict[Timeframe, tuple[str, str]] = {
    Timeframe.M5: ("5m", "60d"),
    Timeframe.M15: ("15m", "60d"),
    # One-off callers may request native M30; HistoryManager derives it from M15.
    Timeframe.M30: ("30m", "60d"),
    Timeframe.H1: ("60m", "730d"),
}

# Minimum seconds between re-fetches of each intraday timeframe, so a signal cycle that
# runs every few seconds doesn't hammer Yahoo with a fresh request per symbol per cycle.
INTRADAY_REFRESH_SECONDS: dict[Timeframe, int] = {
    Timeframe.M5: 5 * 60,
    Timeframe.M15: 5 * 60,
    Timeframe.M30: 10 * 60,
    Timeframe.H1: 20 * 60,
    Timeframe.H4: 40 * 60,
}

_OHLCV_RESAMPLE_RULE: dict[Timeframe, str] = {
    Timeframe.M30: "30min",
    Timeframe.H4: "4h",
}

_TIMEFRAME_DURATION: dict[Timeframe, pd.Timedelta] = {
    Timeframe.M5: pd.Timedelta(minutes=5),
    Timeframe.M15: pd.Timedelta(minutes=15),
    Timeframe.M30: pd.Timedelta(minutes=30),
    Timeframe.H1: pd.Timedelta(hours=1),
    Timeframe.H4: pd.Timedelta(hours=4),
    Timeframe.D1: pd.Timedelta(days=1),
}


def _nse_bucket_open(value: datetime | pd.Timestamp, timeframe: Timeframe) -> pd.Timestamp:
    """Return the NSE-session-aligned candle open containing ``value``."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(IST)
    else:
        timestamp = timestamp.tz_convert(IST)
    session_open = pd.Timestamp.combine(timestamp.date(), MARKET_OPEN).tz_localize(IST)
    if timeframe == Timeframe.D1:
        return session_open
    elapsed = max(pd.Timedelta(0), timestamp - session_open)
    duration = _TIMEFRAME_DURATION[timeframe]
    return session_open + int(elapsed // duration) * duration


def _nse_candle_close(candle_open: pd.Timestamp, timeframe: Timeframe) -> pd.Timestamp:
    session_close = pd.Timestamp.combine(candle_open.date(), MARKET_CLOSE).tz_localize(IST)
    if timeframe == Timeframe.D1:
        return session_close
    return min(candle_open + _TIMEFRAME_DURATION[timeframe], session_close)


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample inside each NSE cash session, anchored at 09:15 IST.

    Pandas' default origin is midnight, which makes both 30-minute and four-hour
    boundaries wrong for NSE.  Grouping each trading date independently also makes
    cross-session contamination structurally impossible.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    duration = pd.Timedelta(rule)
    if duration <= pd.Timedelta(0):
        raise ValueError(f"Invalid resample rule: {rule}")

    normalized = normalize_ohlcv(df)
    if normalized is None:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    index = pd.DatetimeIndex(pd.to_datetime(normalized.index))
    if index.tz is None:
        index = index.tz_localize(IST)
    else:
        index = index.tz_convert(IST)
    normalized.index = index

    calendar = get_nse_calendar()
    sessions: list[pd.DataFrame] = []
    for raw_session_date, session in normalized.groupby(normalized.index.date):
        session_date = cast(date, raw_session_date)
        # Known calendar years use the maintained NSE holiday list.  For older,
        # unavailable years, weekend filtering still prevents cross-weekend bars;
        # the source feed remains authoritative for whether a weekday had trades.
        if session_date.weekday() >= 5:
            continue
        if calendar.is_known_year(session_date.year) and not calendar.is_trading_day(session_date):
            continue
        session_open = pd.Timestamp.combine(session_date, MARKET_OPEN).tz_localize(IST)
        session_close = pd.Timestamp.combine(session_date, MARKET_CLOSE).tz_localize(IST)
        session = session[(session.index >= session_open) & (session.index < session_close)]
        if session.empty:
            continue
        session_index = pd.DatetimeIndex(session.index)
        bucket_number = ((session_index - session_open) // duration).astype(int)
        bucket_open = pd.DatetimeIndex(session_open + bucket_number * duration)
        aggregated = session.groupby(bucket_open).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        sessions.append(aggregated)
    if not sessions:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return pd.concat(sessions).sort_index().dropna(subset=["open", "high", "low", "close"])


def resample_nse_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Public shared entry point for NSE-session-aligned derived candles."""

    return _resample_ohlcv(df, rule)


def normalize_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Normalize a raw yfinance frame to the lower-case OHLCV contract used throughout."""
    if df is None or df.empty:
        return None
    normalized = df.copy()
    normalized.columns = [str(column).lower() for column in normalized.columns]
    required = ["open", "high", "low", "close", "volume"]
    if not set(required).issubset(normalized.columns):
        logger.warning("Skipping data with missing OHLCV columns: %s", normalized.columns.tolist())
        return None
    normalized = normalized[required].apply(pd.to_numeric, errors="coerce")
    normalized = normalized.dropna(subset=required).sort_index()
    return normalized if not normalized.empty else None


def _merge_ohlcv_frames(*frames: pd.DataFrame | None) -> pd.DataFrame | None:
    """Merge stored/API/live frames on one exchange-time index."""
    normalized_frames: list[pd.DataFrame] = []
    for frame in frames:
        normalized = normalize_ohlcv(frame)
        if normalized is None:
            continue
        index = pd.to_datetime(normalized.index)
        if index.tz is None:
            index = index.tz_localize("Asia/Kolkata")
        else:
            index = index.tz_convert("Asia/Kolkata")
        normalized.index = index
        normalized_frames.append(normalized)
    if not normalized_frames:
        return None
    merged = pd.concat(normalized_frames).sort_index()
    return merged[~merged.index.duplicated(keep="last")]


def fetch_timeframe_history(
    feed: HistoricalFeed, symbol: str, timeframe: Timeframe
) -> pd.DataFrame | None:
    """Fetch OHLCV at a specific candle timeframe directly from a feed (no caching/state).

    Shared by anything that needs a one-off multi-timeframe pull (the backfill script,
    the scalping screener) — the same intervals/H4 resampling ``HistoryManager`` itself
    uses, just without its per-symbol TTL cache, which is tuned for a small actively
    traded set re-read every cycle rather than a one-off bulk scan.
    """
    if timeframe == Timeframe.D1:
        return normalize_ohlcv(feed.get_historical(symbol, period="3mo", interval="1d"))

    if timeframe == Timeframe.H4:
        hourly = normalize_ohlcv(feed.get_historical(symbol, period="730d", interval="60m"))
        return _resample_ohlcv(hourly, "4h") if hourly is not None else None

    params = INTRADAY_YF_PARAMS.get(timeframe)
    if params is None:
        raise ValueError(f"Unsupported timeframe: {timeframe.value}")
    interval, period = params
    return normalize_ohlcv(feed.get_historical(symbol, period=period, interval=interval))


class HistoryManager:
    """
    Manages rolling historical DataFrames for each symbol.

    Pre-fetches daily OHLCV data on startup and merges new intraday
    quotes to maintain a continuously updated price history suitable
    for real indicator calculation.
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        lookback_period: str = DEFAULT_LOOKBACK_PERIOD,
        allow_synthetic: bool = False,
        simulated_stream: Any | None = None,
        candle_store: CandleStore | None = None,
        raw_event_sink: RawEventSink | None = None,
    ) -> None:
        """
        Initialize the history manager.

        Args:
            symbols: List of stock symbols to track
            lookback_period: YFinance period string for initial data fetch
            allow_synthetic: Seed demo OHLCV when the configured history feed is unavailable
        """
        self.symbols = symbols or []
        self.lookback_period = lookback_period
        self.allow_synthetic = allow_synthetic
        self._simulated_stream = simulated_stream
        self._candle_store = candle_store

        # Thread-safe storage for historical DataFrames
        self._lock = threading.Lock()
        self._history: dict[str, pd.DataFrame] = {}
        self._last_fetch: dict[str, datetime] = {}
        self._feed = HistoricalDataFeed(
            symbols=self.symbols,
            raw_event_sink=raw_event_sink,
        )
        self._history_cache_dir = DATA_ROOT / "nse" / "history_cache"

        # Intraday multi-timeframe cache: keyed by (symbol, timeframe). Separate from
        # `_history` (which holds the daily bars built up from live quotes) because these
        # are fetched directly from Yahoo at their real interval, not aggregated ticks.
        self._intraday_history: dict[tuple[str, Timeframe], pd.DataFrame] = {}
        self._intraday_last_fetch: dict[tuple[str, Timeframe], datetime] = {}
        self._intraday_last_history_attempt: dict[tuple[str, Timeframe], datetime] = {}
        self._intraday_last_store_load: dict[tuple[str, Timeframe], datetime] = {}
        self._intraday_store_request_bars: dict[tuple[str, Timeframe], int] = {}
        self._derived_source_tokens: dict[tuple[str, Timeframe], str] = {}
        # Dhan quote volume is a cumulative day total. Track the previous value once
        # per symbol so each WebSocket event contributes only its incremental volume
        # to locally maintained intraday candles.
        self._last_intraday_cumulative_volume: dict[str, int] = {}

    def _load_persistent_frame(
        self, symbol: str, timeframe: Timeframe, bars: int | None = None
    ) -> pd.DataFrame | None:
        if self._candle_store is None:
            return None
        try:
            # The signal path is settled-candle only.  Asking the repository to
            # enforce completeness here prevents a persisted forming candle from
            # entering the historical prefix before get_settled_history performs
            # its exchange-close check.  The TypeError fallback keeps lightweight
            # test/legacy repositories compatible while they migrate to the full
            # CandleStore protocol.
            try:
                loaded = self._candle_store.load_frame(
                    symbol,
                    timeframe.value,
                    bars=bars,
                    complete_only=True,
                )
            except TypeError:
                loaded = self._candle_store.load_frame(symbol, timeframe.value, bars=bars)
            return loaded if isinstance(loaded, pd.DataFrame) else None
        except Exception:
            logger.warning(
                "Persistent candle read failed for %s [%s]",
                symbol,
                timeframe.value,
                exc_info=True,
            )
            return None

    def _persist_frame(self, symbol: str, timeframe: Timeframe, frame: pd.DataFrame) -> None:
        if self._candle_store is None:
            return
        try:
            self._candle_store.upsert_frame(symbol, timeframe.value, frame, source="dhan")
        except Exception:
            # Durable history improves reproducibility but must not kill the live loop.
            # The in-memory/API frame remains available for this cycle.
            logger.warning(
                "Persistent candle write failed for %s [%s]",
                symbol,
                timeframe.value,
                exc_info=True,
            )

    def prefetch_all(self) -> dict[str, bool]:
        """
        Pre-fetch historical data for all symbols.

        Returns:
            Dict of symbol -> success status
        """
        results = {}
        logger.info(f"Pre-fetching historical data for {len(self.symbols)} symbols...")

        if self.allow_synthetic and self._simulated_stream is not None:
            for symbol in self.symbols:
                self._seed_synthetic(symbol)
                results[symbol] = symbol in self._history
            logger.warning(
                "TESTING ONLY: loaded coherent simulated history for %d symbols",
                sum(1 for loaded in results.values() if loaded),
            )
            return results

        if not self._feed.is_available:
            for symbol in self.symbols:
                if self.allow_synthetic:
                    self._seed_synthetic(symbol)
                    results[symbol] = True
                else:
                    results[symbol] = False
            if self.allow_synthetic:
                logger.warning(
                    "TESTING ONLY: seeded synthetic daily history for %d symbols",
                    len(self.symbols),
                )
            else:
                logger.error(
                    "Pre-fetch skipped: DhanHQ history is unavailable; no synthetic history will be used"
                )
            return results

        for symbol in self.symbols:
            success = self.fetch_history(symbol)
            if not success and self.allow_synthetic:
                self._seed_synthetic(symbol)
                success = True
            results[symbol] = success

        fetched = sum(1 for v in results.values() if v)
        logger.info(
            "Pre-fetch complete: %d/%d symbols loaded with real DhanHQ history",
            fetched,
            len(self.symbols),
        )
        return results

    def _seed_synthetic(self, symbol: str, bars: int = 180) -> None:
        """Seed from the coherent stream, or use the legacy isolated fallback in tests."""
        if self._simulated_stream is not None:
            daily = self._simulated_stream.get_history(symbol, "1d")
            five_minute = self._simulated_stream.get_history(symbol, "5m")
            if daily is None or five_minute is None:
                return
            # M30/H4 are derived on read from these M15/H1 base frames (see
            # _OHLCV_RESAMPLE_RULE below) -- without refreshing them here too, every
            # timeframe but M5/D1 stays frozen at its cold-bootstrap state forever.
            fifteen_minute = self._simulated_stream.get_history(symbol, "15m")
            one_hour = self._simulated_stream.get_history(symbol, "1h")
            now = datetime.now()
            with self._lock:
                self._history[symbol] = daily
                self._intraday_history[(symbol, Timeframe.M5)] = five_minute
                self._last_fetch[symbol] = now
                self._intraday_last_fetch[(symbol, Timeframe.M5)] = now
                if fifteen_minute is not None:
                    self._intraday_history[(symbol, Timeframe.M15)] = fifteen_minute
                    self._intraday_last_fetch[(symbol, Timeframe.M15)] = now
                if one_hour is not None:
                    self._intraday_history[(symbol, Timeframe.H1)] = one_hour
                    self._intraday_last_fetch[(symbol, Timeframe.H1)] = now
            return

        """Generate a plausible random-walk daily OHLCV history (legacy test fallback)."""
        import numpy as np

        rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
        base = float(rng.uniform(100, 3000))
        returns = rng.normal(0.0004, 0.015, bars)
        closes = base * np.cumprod(1 + returns)
        dates = pd.date_range(end=now_ist().date(), periods=bars, freq="D")
        df = pd.DataFrame(
            {
                "open": closes * (1 - rng.uniform(0, 0.004, bars)),
                "high": closes * (1 + rng.uniform(0, 0.008, bars)),
                "low": closes * (1 - rng.uniform(0, 0.008, bars)),
                "close": closes,
                "volume": rng.integers(100_000, 5_000_000, bars),
            },
            index=dates,
        )
        with self._lock:
            self._history[symbol] = df
            self._last_fetch[symbol] = datetime.now()

    def fetch_history(self, symbol: str, period: str | None = None) -> bool:
        """
        Fetch historical data for a single symbol.

        Args:
            symbol: Stock symbol (e.g., 'RELIANCE')
            period: YFinance period string (default: lookback_period)

        Returns:
            True if data was fetched successfully
        """
        period = period or self.lookback_period
        try:
            stored = self._load_persistent_frame(symbol, Timeframe.D1)
            if stored is not None and len(stored) >= MIN_BARS_FOR_INDICATORS:
                with self._lock:
                    self._history[symbol] = stored
                    self._last_fetch[symbol] = datetime.now()
                logger.info("Loaded %d stored real bars for %s", len(stored), symbol)
                return True

            cached = self._load_cached_history(symbol)
            if cached is not None:
                with self._lock:
                    self._history[symbol] = cached
                    self._last_fetch[symbol] = datetime.now()
                self._persist_frame(symbol, Timeframe.D1, cached)
                logger.info("Loaded %d cached real bars for %s", len(cached), symbol)
                return True

            df = self._feed.get_historical(symbol, period=period)

            if df is None or df.empty:
                logger.warning(f"No historical data returned for {symbol}")
                return False

            # Normalize column names to lowercase for consistency
            df.columns = [c.lower() for c in df.columns]

            # Ensure required columns exist
            required = ["open", "high", "low", "close", "volume"]
            if not all(col in df.columns for col in required):
                logger.warning(f"Missing columns for {symbol}: {df.columns.tolist()}")
                return False

            with self._lock:
                self._history[symbol] = df.copy()
                self._last_fetch[symbol] = datetime.now()
            self._save_cached_history(symbol, df)
            self._persist_frame(symbol, Timeframe.D1, df)

            logger.info(
                f"Loaded {len(df)} bars for {symbol} "
                f"({df.index[0].date()} to {df.index[-1].date()})"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to fetch history for {symbol}: {e}")
            return False

    def _history_cache_path(self, symbol: str) -> Path:
        return Path(self._history_cache_dir / f"{symbol}.pkl")

    def _load_cached_history(self, symbol: str) -> pd.DataFrame | None:
        try:
            loaded = pd.read_pickle(self._history_cache_path(symbol))
            if not isinstance(loaded, pd.DataFrame):
                return None
            df = loaded
            if len(df) < MIN_BARS_FOR_INDICATORS:
                return None
            required = {"open", "high", "low", "close", "volume"}
            if not required.issubset(df.columns):
                return None
            return df
        except (OSError, ValueError, TypeError, EOFError):
            return None

    def _save_cached_history(self, symbol: str, df: pd.DataFrame) -> None:
        try:
            self._history_cache_dir.mkdir(parents=True, exist_ok=True)
            df.to_pickle(self._history_cache_path(symbol))
        except OSError:
            logger.warning("Could not cache real historical data for %s", symbol, exc_info=True)

    def append_quote(
        self,
        symbol: str,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: int,
        timestamp: datetime | None = None,
    ) -> None:
        """
        Append a new quote as a candle to the symbol's history.

        If the timestamp matches the last bar's date, the bar is updated
        (intraday aggregation). Otherwise, a new bar is appended.

        Args:
            symbol: Stock symbol
            open_price: Open price
            high: High price
            low: Low price
            close: Close price
            volume: Volume
            timestamp: Quote timestamp (default: now, in IST)
        """
        if timestamp is None:
            timestamp = now_ist()

        with self._lock:
            if symbol not in self._history:
                logger.debug(f"No history for {symbol}, skipping quote append")
                return

            df = self._history[symbol]

            # Check if we should update the last bar or create a new one
            if len(df) > 0:
                last_date = df.index[-1]

                # Same trading day — update the last bar
                if hasattr(last_date, "date") and last_date.date() == timestamp.date():
                    df.at[df.index[-1], "high"] = max(df.iloc[-1]["high"], high)
                    df.at[df.index[-1], "low"] = min(df.iloc[-1]["low"], low)
                    df.at[df.index[-1], "close"] = close
                    # Quote volume is the cumulative daily total, not a per-tick delta —
                    # keep the latest (monotonic) cumulative value instead of summing,
                    # which previously inflated volume without bound each cycle.
                    df.at[df.index[-1], "volume"] = max(float(df.iloc[-1]["volume"]), float(volume))
                    return

            # New day or first bar — append new row
            new_row = pd.DataFrame(
                {
                    "open": [open_price],
                    "high": [high],
                    "low": [low],
                    "close": [close],
                    "volume": [volume],
                },
                index=[pd.Timestamp(timestamp)],
            )

            self._history[symbol] = pd.concat([df, new_row])

            # Trim to max bars to prevent memory growth
            if len(self._history[symbol]) > MAX_INTRADAY_BARS:
                self._history[symbol] = self._history[symbol].iloc[-MAX_INTRADAY_BARS:]

    def append_intraday_quote(
        self,
        symbol: str,
        price: float,
        cumulative_volume: int,
        timestamp: datetime | None = None,
    ) -> None:
        """Update cached M5/M15/H1 candles from one live WebSocket quote.

        Historical endpoints are needed to bootstrap each native timeframe once. After
        that, the real-time feed keeps the forming candles current locally, so scanning
        hundreds of symbols does not re-download full 60-day histories every five
        minutes. M30 and H4 continue to be derived from M15/H1 by
        :meth:`get_multi_timeframe_history`.

        The method intentionally updates only caches that already exist. This preserves
        the historical feed as the source of truth for the initial lookback and avoids
        pretending that a process started mid-session has enough warm-up history.
        """
        if price <= 0 or cumulative_volume < 0:
            return
        timestamp = timestamp or now_ist()
        current_volume = int(cumulative_volume)

        with self._lock:
            previous_volume = self._last_intraday_cumulative_volume.get(symbol)
            self._last_intraday_cumulative_volume[symbol] = current_volume
            volume_delta = (
                max(0, current_volume - previous_volume) if previous_volume is not None else 0
            )

            for timeframe, rule in (
                (Timeframe.M5, "5min"),
                (Timeframe.M15, "15min"),
                (Timeframe.H1, "1h"),
            ):
                key = (symbol, timeframe)
                frame = self._intraday_history.get(key)
                if frame is None or frame.empty:
                    continue

                bucket = _nse_bucket_open(timestamp, timeframe)
                last_index = pd.Timestamp(frame.index[-1])
                # Tests and synthetic fixtures may use naive indexes while real Dhan
                # frames are IST-aware. Match the cache's timezone before comparing.
                if last_index.tzinfo is None and bucket.tzinfo is not None:
                    bucket = bucket.tz_localize(None)
                elif last_index.tzinfo is not None and bucket.tzinfo is None:
                    bucket = bucket.tz_localize(last_index.tzinfo)
                elif last_index.tzinfo is not None and bucket.tzinfo is not None:
                    bucket = bucket.tz_convert(last_index.tzinfo)

                if bucket < last_index:
                    continue
                if bucket == last_index:
                    frame.at[frame.index[-1], "high"] = max(float(frame.iloc[-1]["high"]), price)
                    frame.at[frame.index[-1], "low"] = min(float(frame.iloc[-1]["low"]), price)
                    frame.at[frame.index[-1], "close"] = price
                    frame.at[frame.index[-1], "volume"] = (
                        float(frame.iloc[-1]["volume"]) + volume_delta
                    )
                else:
                    new_row = pd.DataFrame(
                        {
                            "open": [price],
                            "high": [price],
                            "low": [price],
                            "close": [price],
                            "volume": [volume_delta],
                        },
                        index=[bucket],
                    )
                    frame = pd.concat([frame, new_row]).iloc[-MAX_INTRADAY_BARS:]
                    self._intraday_history[key] = frame

                # A sufficiently warm cache updated from the live stream is fresh;
                # forcing a full historical request would only replace the same bar.
                if len(frame) >= MIN_BARS_FOR_INDICATORS:
                    self._intraday_last_fetch[key] = datetime.now()

    def get_history(
        self,
        symbol: str,
        bars: int | None = None,
        include_forming: bool = True,
    ) -> pd.DataFrame | None:
        """
        Get historical DataFrame for a symbol.

        Args:
            symbol: Stock symbol
            bars: Number of recent bars to return (None = all)
            include_forming: If False, drop the current (still-forming) bar — i.e. any
                trailing rows dated today (IST). The forming bar's OHLC repaints as live
                quotes arrive, so indicators/signals computed on it look ahead within the
                bar; pass False to compute on settled bars only. Never returns empty
                purely from this trim (falls back to the full history if everything is today).

        Returns:
            DataFrame with OHLCV columns, or None if not available
        """
        with self._lock:
            current = self._history.get(symbol)
            current = current.copy() if current is not None else None

        if self._candle_store is not None and (
            current is None or (bars is not None and len(current) < bars)
        ):
            stored = self._load_persistent_frame(symbol, Timeframe.D1, bars=bars)
            merged = _merge_ohlcv_frames(stored, current)
            if merged is not None:
                current = merged
                with self._lock:
                    self._history[symbol] = merged

        if current is None:
            return None
        df = current

        if not include_forming and len(df) > 0:
            today = now_ist().date()
            settled = df[[not (hasattr(ts, "date") and ts.date() == today) for ts in df.index]]
            if len(settled) > 0:
                df = settled

        if bars is not None and len(df) > bars:
            df = df.iloc[-bars:]

        return df

    def get_multi_timeframe_history(
        self,
        symbol: str,
        timeframe: Timeframe,
        bars: int | None = None,
    ) -> pd.DataFrame | None:
        """
        Get OHLCV history for a symbol at a specific candle timeframe.

        Daily (D1) is served from the existing quote-built history (`get_history`).
        M15 and H1 are fetched from the configured feed and cached with a TTL.
        M30 and H4 are resampled from those base frames, avoiding duplicate broker
        history requests during full-universe analysis.

        Returns None if the timeframe isn't supported or no data is available yet
        (never raises — a fetch failure just falls back to whatever is already cached).

        Re-checks market hours on every call (not just once at construction): with a
        configured simulated_stream, closed-market cycles compute indicators/signals
        off simulated data instead of a frozen real snapshot -- so the signal-
        generation pipeline is actually exercisable for pipeline testing on a closed
        weekend, the same way MarketDataManager already auto-switches the live quote
        feed. Reverts to real DhanHQ data automatically once the market reopens, no
        restart required. Chart display and already-open positions' pricing/exits are
        unaffected -- they resolve their own lineage separately (see
        run_live_trading.py's _get_candles and manager.py's get_real_quotes()/
        get_simulated_quotes()), not through this method.
        """
        if self.allow_synthetic and self._simulated_stream is not None:
            simulated = self._simulated_stream.get_history(symbol, timeframe.value, bars)
            return simulated if isinstance(simulated, pd.DataFrame) else None
        if self._simulated_stream is not None and not is_market_hours():
            simulated = self._simulated_stream.get_history(symbol, timeframe.value, bars)
            return simulated if isinstance(simulated, pd.DataFrame) else None

        if timeframe == Timeframe.D1:
            return self.get_history(symbol, bars=bars)

        if timeframe in _OHLCV_RESAMPLE_RULE:
            base_timeframe = Timeframe.M15 if timeframe == Timeframe.M30 else Timeframe.H1
            # A requested derived window needs a proportionally deeper native
            # prefix.  Without passing this requirement through, a short live tail
            # suppresses the Timescale read and leaves 30m/4h context unavailable.
            factor = 2 if timeframe == Timeframe.M30 else 4
            base_bars = bars * factor if bars is not None else None
            base = self.get_multi_timeframe_history(symbol, base_timeframe, bars=base_bars)
            if base is None or base.empty:
                return None
            key = (symbol, timeframe)
            # A forming base candle may change on every quote, but no derived settled
            # candle can change until a new base-candle timestamp appears.
            source_token = f"{base.index[-1]}:{len(base)}"
            with self._lock:
                cached_derived = self._intraday_history.get(key)
                previous_token = self._derived_source_tokens.get(key)
            if cached_derived is not None and previous_token == source_token:
                df = cached_derived.copy()
            else:
                df = _resample_ohlcv(base, _OHLCV_RESAMPLE_RULE[timeframe])
                with self._lock:
                    self._intraday_history[key] = df
                    self._derived_source_tokens[key] = source_token
        else:
            params = INTRADAY_YF_PARAMS.get(timeframe)
            if params is None:
                logger.warning(f"Unsupported intraday timeframe: {timeframe}")
                return None

            key = (symbol, timeframe)
            with self._lock:
                last = self._intraday_last_fetch.get(key)
                last_history_attempt = self._intraday_last_history_attempt.get(key)
                last_store_load = self._intraday_last_store_load.get(key)
                previous_store_bars = self._intraday_store_request_bars.get(key, 0)
                current = self._intraday_history.get(key)
                current = current.copy() if current is not None else None

            # ML can request a deeper window than the 200-bar indicator snapshot. Read
            # that window from the local store without turning it into another Dhan call.
            persistent_bars = bars or MAX_INTRADAY_BARS
            history_insufficient = current is None or current.empty or (
                bars is not None and len(current) < bars
            )
            store_refresh_after = INTRADAY_REFRESH_SECONDS.get(timeframe, 300)
            store_load_due = history_insufficient and (
                last_store_load is None
                or persistent_bars > previous_store_bars
                or (datetime.now() - last_store_load).total_seconds() >= store_refresh_after
            )
            if self._candle_store is not None and store_load_due:
                stored = self._load_persistent_frame(symbol, timeframe, bars=persistent_bars)
                loaded_at = datetime.now()
                with self._lock:
                    self._intraday_last_store_load[key] = loaded_at
                    self._intraday_store_request_bars[key] = persistent_bars
                # Stored history is the prefix and the in-memory quote-built frame
                # is the authoritative fresh tail.  _merge_ohlcv_frames keeps the
                # latter on duplicate timestamps because it is passed last.
                merged = _merge_ohlcv_frames(stored, current)
                if merged is not None:
                    current = merged
                    with self._lock:
                        self._intraday_history[key] = merged
                        if bars is None or len(merged) >= bars:
                            self._intraday_last_fetch[key] = datetime.now()
                            last = self._intraday_last_fetch[key]
            history_insufficient = current is None or current.empty or (
                bars is not None and len(current) < bars
            )
            refresh_after = INTRADAY_REFRESH_SECONDS.get(timeframe, 300)
            regular_refresh_due = (
                last is None or (datetime.now() - last).total_seconds() >= refresh_after
            )
            history_refresh_due = history_insufficient and (
                last_history_attempt is None
                or (datetime.now() - last_history_attempt).total_seconds() >= refresh_after
            )
            stale = history_refresh_due if history_insufficient else regular_refresh_due
            # During the live NSE session, once bootstrapped, quote ingestion owns the
            # forming/new candle tail. Re-downloading a 60-day/730-day range here would
            # turn every scan into historical ingestion again.
            if (
                current is not None
                and not current.empty
                and not history_insufficient
                and is_market_hours()
            ):
                stale = False

            if stale:
                interval, period = params
                try:
                    fetched = self._feed.get_historical(symbol, period=period, interval=interval)
                except Exception as e:
                    logger.warning(f"Intraday fetch failed for {symbol} [{timeframe.value}]: {e}")
                    fetched = None

                # Record every attempted provider refresh, including a failed one,
                # so an insufficient DB series cannot cause one Dhan history call
                # per symbol on every scheduler wake-up.
                attempted_at = datetime.now()
                with self._lock:
                    self._intraday_last_history_attempt[key] = attempted_at
                if fetched is not None and not fetched.empty:
                    fetched = fetched.copy()
                    fetched.columns = [c.lower() for c in fetched.columns]
                    self._persist_frame(symbol, timeframe, fetched)
                    merged = _merge_ohlcv_frames(current, fetched)
                    if merged is None:
                        merged = fetched
                    with self._lock:
                        self._intraday_history[key] = merged
                        self._intraday_last_fetch[key] = attempted_at
                elif current is None or current.empty:
                    # Never successfully fetched — nothing to fall back to.
                    return None

            with self._lock:
                cached = self._intraday_history.get(key)
            if cached is None:
                return None
            df = cached.copy()

        if bars is not None and len(df) > bars:
            df = df.iloc[-bars:]
        return df

    def get_settled_history(
        self,
        symbol: str,
        timeframe: Timeframe,
        bars: int | None = None,
        *,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> pd.DataFrame | None:
        """Return only candles whose NSE-session close has occurred."""
        if timeframe == Timeframe.D1:
            frame = self.get_history(symbol, bars=None, include_forming=True)
        else:
            # Request the independently required window for this exact timeframe.
            # One extra row allows a currently-forming tail to be excluded without
            # reducing a full settled window below the caller's requirement.
            requested_bars = bars + 1 if bars is not None else None
            frame = self.get_multi_timeframe_history(symbol, timeframe, bars=requested_bars)
        if frame is None or frame.empty:
            return None
        # Same fix as latest_settled_candle_timestamp: judge settlement against the
        # simulator's own event clock in MOCK mode, not real now_ist() -- otherwise
        # every freshly-ticked bar reads as future-dated and this always falls back
        # to the frozen cold-bootstrap frame, silently blocking every later cycle.
        if as_of is not None:
            reference = as_of
        elif self._simulated_stream is not None:
            reference = self._simulated_stream.event_time
        else:
            reference = now_ist()
        current = pd.Timestamp(reference)
        if current.tzinfo is None:
            current = current.tz_localize(IST)
        else:
            current = current.tz_convert(IST)
        settled_positions: list[int] = []
        for position, value in enumerate(frame.index):
            candle_open = pd.Timestamp(value)
            if candle_open.tzinfo is None:
                candle_open = candle_open.tz_localize(IST)
            else:
                candle_open = candle_open.tz_convert(IST)
            if _nse_candle_close(candle_open, timeframe) <= current:
                settled_positions.append(position)
        if not settled_positions:
            return None
        settled = frame.iloc[settled_positions]
        if bars is not None and len(settled) > bars:
            settled = settled.iloc[-bars:]
        return settled

    def latest_settled_candle_timestamp(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> pd.Timestamp | None:
        # The simulator's event clock is independent of wall-clock time -- it ticks
        # forward through its own synthetic calendar (often into "tomorrow" within
        # minutes of boot). Settlement must be judged against that same clock, not
        # real now_ist(), or every freshly-ticked bar reads as future-dated and
        # never settles -- freezing this on the cold-bootstrap bar forever.
        if as_of is not None:
            reference = as_of
        elif self._simulated_stream is not None:
            reference = self._simulated_stream.event_time
        else:
            reference = now_ist()
        current = pd.Timestamp(reference)
        if current.tzinfo is None:
            current = current.tz_localize(IST)
        else:
            current = current.tz_convert(IST)

        if timeframe == Timeframe.D1:
            with self._lock:
                frame = self._history.get(symbol)
        elif timeframe in _OHLCV_RESAMPLE_RULE:
            base_timeframe = Timeframe.M15 if timeframe == Timeframe.M30 else Timeframe.H1
            with self._lock:
                base = self._intraday_history.get((symbol, base_timeframe))
            if base is None or base.empty:
                # Cold bootstrap only. Later timer wake-ups read the in-memory tail.
                self.get_multi_timeframe_history(symbol, base_timeframe, bars=8)
                with self._lock:
                    base = self._intraday_history.get((symbol, base_timeframe))
            if base is None or base.empty:
                return None
            for value in reversed(base.index):
                base_open = pd.Timestamp(value)
                if base_open.tzinfo is None:
                    base_open = base_open.tz_localize(IST)
                else:
                    base_open = base_open.tz_convert(IST)
                bucket = _nse_bucket_open(base_open, timeframe)
                if _nse_candle_close(bucket, timeframe) <= current:
                    return bucket
            return None
        else:
            with self._lock:
                frame = self._intraday_history.get((symbol, timeframe))
            if frame is None or frame.empty:
                # Cold bootstrap only. The live quote updater maintains this cache.
                self.get_multi_timeframe_history(symbol, timeframe, bars=2)
                with self._lock:
                    frame = self._intraday_history.get((symbol, timeframe))

        if frame is None or frame.empty:
            return None
        for value in reversed(frame.index):
            candle_open = pd.Timestamp(value)
            if candle_open.tzinfo is None:
                candle_open = candle_open.tz_localize(IST)
            else:
                candle_open = candle_open.tz_convert(IST)
            if _nse_candle_close(candle_open, timeframe) <= current:
                return pd.Timestamp(value)
        return None

    def sync_simulated_stream(self) -> None:
        """Refresh daily and five-minute caches from the authoritative simulated stream."""
        if self._simulated_stream is None:
            return
        for symbol in self.symbols:
            self._seed_synthetic(symbol)

    def has_sufficient_data(self, symbol: str, min_bars: int = MIN_BARS_FOR_INDICATORS) -> bool:
        """
        Check if a symbol has enough data for indicator calculation.

        Args:
            symbol: Stock symbol
            min_bars: Minimum number of bars required

        Returns:
            True if sufficient data is available
        """
        with self._lock:
            if symbol not in self._history:
                return False
            return len(self._history[symbol]) >= min_bars

    def get_available_symbols(self) -> list[str]:
        """Get list of symbols with loaded history."""
        with self._lock:
            return [s for s in self._history if len(self._history[s]) >= MIN_BARS_FOR_INDICATORS]

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about loaded history data."""
        with self._lock:
            stats = {}
            for symbol, df in self._history.items():
                stats[symbol] = {
                    "bars": len(df),
                    "start": str(df.index[0].date()) if len(df) > 0 else "N/A",
                    "end": str(df.index[-1].date()) if len(df) > 0 else "N/A",
                    "sufficient": len(df) >= MIN_BARS_FOR_INDICATORS,
                }
            return stats

    def refresh_stale(self, max_age_hours: int = 12) -> int:
        """
        Re-fetch data for symbols whose cache is stale.

        Args:
            max_age_hours: Maximum age in hours before refetching

        Returns:
            Number of symbols refreshed
        """
        refreshed = 0
        cutoff = datetime.now() - timedelta(hours=max_age_hours)

        for symbol in self.symbols:
            last = self._last_fetch.get(symbol)
            if last is None or last < cutoff:
                if self.fetch_history(symbol):
                    refreshed += 1

        return refreshed
