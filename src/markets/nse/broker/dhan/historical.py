"""DhanHQ historical-candle feed — OHLCV data via DhanHQ's charts endpoints.

Exposes the ``(symbol, period, interval) -> DataFrame | None`` contract this
codebase's HistoricalFeed Protocol (historical_feed.py) expects — capitalized
Open/High/Low/Close/Volume columns, DatetimeIndex — so it flows through the
same ``normalize_ohlcv()``/``_resample_ohlcv()`` pipeline in history_manager.py
unchanged.

Two real API constraints (verified against DhanHQ's docs and the installed
``dhanhq`` SDK source, not guessed) shape this:

- DhanHQ's intraday intervals are only 1/5/15/25/60 minutes — no native 30m,
  which every timeframe-driven feature here needs. Built by resampling the
  native 15m interval, the same technique already used elsewhere in this
  codebase for H4-from-H1.
- Each ``/charts/intraday`` or ``/charts/historical`` call is capped at 90
  days of data per DhanHQ's docs. Most periods this codebase requests
  (60d/3mo) are trimmed to a much shorter lookback downstream anyway (see
  scalping_screener.py's ``lookback_days``) and fit in one call, but the
  strategy walk-forward validator (scripts/validate_strategy.py) genuinely
  needs "2y" of daily bars, so ``get_historical`` fetches periods longer than
  ``MAX_REQUEST_DAYS`` as several sequential ≤90-day windows and concatenates
  them, oldest first, instead of silently truncating to the most recent 90
  days.

Timestamps: DhanHQ returns Unix epoch seconds (UTC); this codebase's OHLCV
convention is tz-aware Asia/Kolkata timestamps. Converting explicitly to
Asia/Kolkata here is required, not cosmetic — day-grouping logic downstream
(e.g. the scalping screener's per-day zigzag scoring) buckets by calendar day
off this index, and a UTC vs IST mismatch would silently shift bars across day
boundaries.

Rate limiting: confirmed live that DhanHQ enforces a single account-wide cap
of 5 requests/second across ALL Data APIs combined (quote, ohlc, historical,
intraday) — not a separate budget per endpoint. A scalping-screener scan
calling get_historical() for ~275 symbols with zero throttling triggered
DH-904 Rate_Limit errors on nearly every call (each one returning None and
getting logged, so nothing crashed — but it meant DhanHQ was effectively
unusable under the unthrottled load). Every call here goes through the
shared get_dhan_data_api_limiter() (src/utils/rate_limiter.py) instead of a
local delay, since the limit is account-wide: a local-only throttle would
still be exceeded by other concurrent Dhan callers (e.g. the sector-movers
quote scan) drawing from the same account.
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from src.config import get_settings
from src.core.market_data import RawEventSink, RawEventType, RawMarketEvent, emit_raw_event
from src.core.models import Market, MarketProvider
from src.markets.nse.broker.dhan.auth import get_dhan_client, get_valid_access_token
from src.markets.nse.broker.dhan.instruments import fetch_security_id_map
from src.markets.nse.broker.dhan.rate_limits import get_dhan_data_api_limiter

logger = logging.getLogger(__name__)

MAX_REQUEST_DAYS = 90
MAX_LOOKBACK_DAYS = 5 * 365
EXCHANGE_SEGMENT = "NSE_EQ"
INSTRUMENT_TYPE = "EQUITY"
IST = "Asia/Kolkata"

# Period strings this codebase actually passes, mapped to a lookback in
# calendar days. Not clamped to MAX_REQUEST_DAYS here -- get_historical()
# paginates across multiple ≤MAX_REQUEST_DAYS calls for anything longer.
_PERIOD_DAYS: dict[str, int] = {
    "5d": 5,
    "10d": 10,
    "1mo": 30,
    "60d": 60,
    "3mo": 90,
    "6mo": 180,
    "730d": 730,
    "2y": 730,
}

# Interval string -> (Dhan minute interval, resample rule if not native).
# "1h"/"4h" are the canonical labels used everywhere else in this codebase
# (strategies.yaml, the eligibility registry, signal timeframes) -- "60m" was
# the only alias this map recognized, which made callers passing the
# canonical "1h" (e.g. scripts/validate_strategy.py --interval 1h) fail with
# "Unsupported interval" even though live trading fetches the same data fine
# via history_manager.py's own separate "60m"-then-resample call. "4h" mirrors
# history_manager.py:216-217's existing 60m->4h resample exactly.
_INTRADAY_INTERVAL_MAP: dict[str, tuple[int, str | None]] = {
    "5m": (5, None),
    "15m": (15, None),
    "30m": (15, "30min"),
    "60m": (60, None),
    "1h": (60, None),
    "4h": (60, "4h"),
}


def _period_to_days(period: str) -> int:
    normalized = period.strip().lower()
    known = _PERIOD_DAYS.get(normalized)
    if known is not None:
        return known
    # Incremental ingestion emits dynamic tails such as "3d". Accept bounded
    # calendar-day values while keeping arbitrary strings on the old safe fallback.
    if normalized.endswith("d") and normalized[:-1].isdigit():
        return min(MAX_LOOKBACK_DAYS, max(1, int(normalized[:-1])))
    return MAX_REQUEST_DAYS


def _parse_ohlcv_response(data: dict[str, Any] | None) -> pd.DataFrame | None:
    """Parse DhanHQ's parallel-array response into a standard OHLCV DataFrame."""
    if not data or "timestamp" not in data:
        return None
    try:
        index = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert(IST)
        df = pd.DataFrame(
            {
                "Open": data["open"],
                "High": data["high"],
                "Low": data["low"],
                "Close": data["close"],
                "Volume": data.get("volume", [0] * len(data["timestamp"])),
            },
            index=index,
        )
        return df if not df.empty else None
    except (KeyError, ValueError, TypeError):
        logger.warning("Unexpected DhanHQ historical response shape", exc_info=True)
        return None


class DhanHistoricalFeed:
    """Fetches OHLCV history from DhanHQ, matching the HistoricalFeed Protocol's contract."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        *,
        raw_event_sink: RawEventSink | None = None,
    ):
        settings = get_settings()
        self._client = get_dhan_client(settings.dhan_client_id, get_valid_access_token())
        # Pre-resolve for the given universe up front (one instrument-master fetch),
        # falling back to a lazy per-symbol resolve for anything requested later.
        self._security_ids: dict[str, str] = fetch_security_id_map(symbols) if symbols else {}
        self._raw_event_sink = raw_event_sink

    def _security_id(self, symbol: str) -> str | None:
        if symbol in self._security_ids:
            return self._security_ids[symbol]
        resolved = fetch_security_id_map([symbol]).get(symbol)
        if resolved:
            self._security_ids[symbol] = resolved
        return resolved

    def _intraday_response(
        self, security_id: str, from_date: datetime, to_date: datetime, interval: int
    ) -> dict[str, Any]:
        """Fetch Dhan intraday candles, backing off on its account-wide rate limit."""
        response: dict[str, Any] = {}
        for attempt in range(3):
            get_dhan_data_api_limiter().acquire_sync()
            response = self._client.intraday_minute_data(
                security_id=security_id,
                exchange_segment=EXCHANGE_SEGMENT,
                instrument_type=INSTRUMENT_TYPE,
                from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
                interval=interval,
            )
            remarks = response.get("remarks") or {}
            # Dhan's "remarks" field is a dict for structured errors (DH-904 rate
            # limit) but a plain string for others (e.g. "DH-901 invalid token") --
            # only a dict can carry an error_code to check against.
            error_code = remarks.get("error_code") if isinstance(remarks, dict) else None
            if response.get("status") == "success" or error_code != "DH-904":
                return response
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
        return response

    def _fetch_window(
        self,
        security_id: str,
        from_date: datetime,
        to_date: datetime,
        dhan_interval: int,
        symbol: str,
        interval_label: str,
    ) -> pd.DataFrame | None:
        """Fetch and parse a single ≤MAX_REQUEST_DAYS window. None on failure --
        callers treat a missing window as a gap, not a fatal error, so one bad
        window doesn't discard data already fetched for the rest of the range."""
        response = self._intraday_response(security_id, from_date, to_date, interval=dhan_interval)
        data = response.get("data")
        timestamps = data.get("timestamp", []) if isinstance(data, dict) else []
        source_time = (
            datetime.fromtimestamp(float(timestamps[-1]), tz=UTC)
            if timestamps
            else to_date.replace(tzinfo=to_date.tzinfo or UTC).astimezone(UTC)
        )
        emit_raw_event(
            self._raw_event_sink,
            RawMarketEvent.create(
                market=Market.NSE,
                provider=MarketProvider.DHAN,
                event_type=RawEventType.CANDLE,
                symbol=symbol,
                channel=f"intraday:{interval_label}",
                source_event_time=source_time,
                payload=response,
            ),
        )
        if response.get("status") != "success":
            logger.warning(
                "DhanHQ historical fetch failed for %s (%s) window %s..%s: %s",
                symbol,
                interval_label,
                from_date.date(),
                to_date.date(),
                response.get("remarks"),
            )
            return None
        return _parse_ohlcv_response(data)

    def get_historical(
        self, symbol: str, period: str = "1mo", interval: str = "1d"
    ) -> pd.DataFrame | None:
        """Get historical OHLCV data for a symbol.

        Periods longer than MAX_REQUEST_DAYS (e.g. the strategy validator's "2y"
        walk-forward window) are fetched as several sequential ≤MAX_REQUEST_DAYS
        windows and concatenated -- see the module docstring.
        """
        security_id = self._security_id(symbol)
        if security_id is None:
            logger.warning("No DhanHQ security ID for %s; cannot fetch historical data", symbol)
            return None

        days = _period_to_days(period)
        overall_to = datetime.now()
        overall_from = overall_to - timedelta(days=days)

        try:
            if interval == "1d":
                # DhanHQ's daily endpoint rejects valid security IDs such as
                # HDFCBANK with DH-905. Its 60-minute endpoint succeeds for the
                # same IDs, so build genuine daily candles from that source.
                dhan_interval = 60
                resample_rule: str | None = "1D"
            else:
                mapping = _INTRADAY_INTERVAL_MAP.get(interval)
                if mapping is None:
                    raise ValueError(f"Unsupported interval for DhanHistoricalFeed: {interval}")
                dhan_interval, resample_rule = mapping

            frames: list[pd.DataFrame] = []
            window_start = overall_from
            while window_start < overall_to:
                window_end = min(window_start + timedelta(days=MAX_REQUEST_DAYS), overall_to)
                try:
                    frame = self._fetch_window(
                        security_id, window_start, window_end, dhan_interval, symbol, interval
                    )
                except Exception:
                    # One window's unexpected failure shouldn't discard frames
                    # already fetched for the rest of the requested range.
                    logger.warning(
                        "Error fetching DhanHQ window for %s (%s) %s..%s",
                        symbol,
                        interval,
                        window_start.date(),
                        window_end.date(),
                        exc_info=True,
                    )
                    frame = None
                if frame is not None:
                    frames.append(frame)
                window_start = window_end

            if not frames:
                return None

            df = pd.concat(frames).sort_index()
            df = df[~df.index.duplicated(keep="last")]

            if resample_rule is not None:
                df = df.resample(resample_rule).agg(
                    {
                        "Open": "first",
                        "High": "max",
                        "Low": "min",
                        "Close": "last",
                        "Volume": "sum",
                    }
                ).dropna(subset=["Open", "High", "Low", "Close"])

            return df
        except Exception:
            logger.warning(
                "Error fetching DhanHQ historical data for %s (%s)", symbol, interval, exc_info=True
            )
            return None
