"""DhanHQ live quotes — current price/OHLC/change% via DhanHQ's batched
``quote_data`` endpoint.

Produces the ``Quote`` shape ``sector_movers.py`` consumes. Batches all
symbols into DhanHQ's ``quote_data`` endpoint (up to
100 security IDs per call — the same cap ``websocket_feed.py`` already works
around for its own ``subscribe()`` calls) instead of one HTTP round-trip per
symbol.

Confirmed via a live 276-symbol scan: DhanHQ enforces a single account-wide
cap of 5 requests/second across ALL Data APIs combined (quote, ohlc,
historical, intraday) — not a separate budget per endpoint. Calling
quote_data's 100-symbol batches back-to-back failed instantly on the 2nd/3rd
batch with an all-null error payload; separately, running this alongside the
scalping screener's historical fetches (dhan_historical_feed.py) at the same
time made the combined rate exceed the limit even though each looked fine
tested in isolation. Every DhanHQ call here goes through the shared
get_dhan_data_api_limiter() (src/utils/rate_limiter.py) rather than a local
delay, since a local-only throttle can't account for other concurrent
callers drawing from the same account-wide budget.

change_percent does NOT come from DhanHQ's quote ``net_change`` field —
verified live that it doesn't track day-over-day change at all: for a stock
that had genuinely moved from a ₹1,280 close to a ₹1,325 close (+3.5%,
cross-checked independently against yfinance's daily history), the live
quote's ``net_change`` read exactly 0. It appears to be a live-tick-derived
field DhanHQ's system doesn't retroactively populate once trading stops, not
a stored "vs previous close" value. Instead, this fetches each symbol's real
previous close via DhanHQ's ``historical_daily_data`` endpoint (unaffected by
that issue — cross-checked and confirmed accurate) and computes change%
itself. That endpoint has no batching (one call per symbol), and results are
cached for the IST calendar day since repeating ~275 rate-limited calls every
3-minute scan would be far too slow.

One more real quirk worth knowing: DhanHQ's daily bars are timestamped at
IST-midnight-of-the-bar's-date, which lands on the *next* calendar date when
read back as naive UTC — decoding a bar's timestamp naively as UTC and taking
its date will label it one day late. This module doesn't need to solve that
labelling problem at all: it only ever uses the *last* bar in the returned
series as "the most recent available close", regardless of what calendar date
it nominally carries, which is automatically the correct reference point for
computing change against the current last_price.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.core.market_data import RawEventSink, RawEventType, RawMarketEvent, emit_raw_event
from src.core.models import Market, MarketProvider
from src.core.paths import project_path
from src.markets.nse.broker.dhan.auth import get_dhan_client, get_valid_access_token
from src.markets.nse.broker.dhan.instruments import fetch_security_id_map
from src.markets.nse.broker.dhan.rate_limits import (
    get_dhan_data_api_limiter,
    get_dhan_quote_api_limiter,
)
from src.markets.nse.sessions.market_time import IST, now_ist

logger = logging.getLogger(__name__)

# DhanHQ v2 Market Quote accepts up to 1,000 instruments per request. Keeping the
# old subscription-message limit of 100 here made a 250-symbol REST refresh take
# three separately rate-limited calls even though it fits in one quote request.
MAX_SECURITIES_PER_REQUEST = 1000
PREVIOUS_CLOSE_LOOKBACK_DAYS = 10


@dataclass
class Quote:
    """A current market quote — symbol, LTP, OHLC, and change vs. previous close."""

    symbol: str
    last_price: float
    open: float
    high: float
    low: float
    close: float  # Previous close
    change: float
    change_percent: float
    volume: int
    timestamp: datetime = field(default_factory=now_ist)
    exchange_timestamp: datetime | None = None
    received_at: datetime = field(default_factory=now_ist)

    @property
    def is_bullish(self) -> bool:
        return self.change_percent > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "change": self.change,
            "change_percent": self.change_percent,
            "volume": self.volume,
            "timestamp": self.timestamp.isoformat(),
            "exchange_timestamp": (
                self.exchange_timestamp.isoformat() if self.exchange_timestamp else None
            ),
            "received_at": self.received_at.isoformat(),
            "is_live": False,
        }


def _fetch_previous_close(
    client: Any, exchange_segment: str, instrument_type: str, security_id: str
) -> float | None:
    """Return the most recent available daily close for one symbol, or None on
    any failure. See module docstring for why "most recent bar" is always the
    right reference regardless of what calendar date it's nominally labelled."""
    try:
        to_date = now_ist().strftime("%Y-%m-%d")
        from_date = (now_ist() - timedelta(days=PREVIOUS_CLOSE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        get_dhan_data_api_limiter().acquire_sync()
        response = client.historical_daily_data(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=from_date,
            to_date=to_date,
        )
        if response.get("status") != "success":
            return None
        closes = response.get("data", {}).get("close")
        if not closes:
            return None
        return float(closes[-1])
    except Exception:
        return None


def _load_previous_close_cache(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            cached: dict[str, Any] = json.load(f)
            return cached
    except (OSError, json.JSONDecodeError):
        return None


def _save_previous_close_cache(path: Path, date: str, closes: dict[str, float]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"date": date, "closes": closes}, f)
    except OSError:
        logger.warning("Could not write previous-close cache to %s", path, exc_info=True)


def _parse_quote(
    symbol: str, payload: dict[str, Any], previous_close: float | None
) -> Quote | None:
    """Parse one DhanHQ quote_data entry into a Quote.

    change/change_percent come from the caller-supplied previous_close (see
    module docstring for why that's fetched separately rather than trusting
    the quote's own net_change field), not anything in ``payload`` itself.
    """
    try:
        received_at = now_ist()
        exchange_timestamp: datetime | None = None
        raw_last_trade_time = payload.get("last_trade_time")
        if isinstance(raw_last_trade_time, str) and raw_last_trade_time.strip():
            try:
                parsed = datetime.strptime(raw_last_trade_time.strip(), "%d/%m/%Y %H:%M:%S")
                # Dhan's documented market-quote timestamp is exchange-local time.
                parsed = parsed.replace(tzinfo=IST)
                # 1980 is Dhan's documented/example sentinel for unavailable LTT, not
                # a legitimate current-session trade.
                if parsed.year >= 2000:
                    exchange_timestamp = parsed
            except ValueError:
                exchange_timestamp = None
        last_price = float(payload["last_price"])
        ohlc = payload.get("ohlc") or {}
        if previous_close:
            change = last_price - previous_close
            change_percent = change / previous_close * 100
        else:
            change = 0.0
            change_percent = 0.0
        return Quote(
            symbol=symbol,
            last_price=last_price,
            open=float(ohlc.get("open", 0.0)),
            high=float(ohlc.get("high", 0.0)),
            low=float(ohlc.get("low", 0.0)),
            close=round(previous_close, 2) if previous_close else 0.0,
            change=round(change, 2),
            change_percent=round(change_percent, 2),
            volume=int(payload.get("volume", 0)),
            timestamp=exchange_timestamp or received_at,
            exchange_timestamp=exchange_timestamp,
            received_at=received_at,
        )
    except (KeyError, ValueError, TypeError):
        logger.warning("Unexpected DhanHQ quote shape for %s", symbol, exc_info=True)
        return None


class DhanQuotesFeed:
    """Fetches current quotes for many symbols via a handful of batched DhanHQ calls."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        *,
        raw_event_sink: RawEventSink | None = None,
    ):
        settings = get_settings()
        self._client = get_dhan_client(settings.dhan_client_id, get_valid_access_token())
        self._exchange_segment = settings.dhan_exchange_segment
        self._security_ids: dict[str, str] = fetch_security_id_map(symbols) if symbols else {}
        self._cache_path = project_path(settings.dhan_previous_close_cache_file)
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._previous_closes: dict[str, float] = {}
        self._previous_closes_date: str | None = None
        self._fetch_previous_closes = settings.enable_dhan_historical_data
        self._raw_event_sink = raw_event_sink

    def _ensure_previous_closes(self) -> None:
        """Load or (re)fetch previous closes so they're fresh for today (IST)."""
        today = now_ist().strftime("%Y-%m-%d")
        if self._previous_closes_date == today and self._previous_closes:
            return

        cached = _load_previous_close_cache(self._cache_path)
        if cached and cached.get("date") == today and cached.get("closes"):
            self._previous_closes = cached["closes"]
            self._previous_closes_date = today
            return

        closes: dict[str, float] = {}
        for symbol, security_id in self._security_ids.items():
            previous_close = _fetch_previous_close(
                self._client, self._exchange_segment, "EQUITY", security_id
            )
            if previous_close is not None:
                closes[symbol] = previous_close

        self._previous_closes = closes
        self._previous_closes_date = today
        _save_previous_close_cache(self._cache_path, today, closes)
        logger.info(
            "Refreshed previous-close cache: %d/%d symbols", len(closes), len(self._security_ids)
        )

    def fetch_quotes(self) -> dict[str, Quote]:
        """Fetch current quotes for every resolved symbol.

        Symbols with no resolved DhanHQ security ID, or whose batch request
        fails, are simply absent from the result — a failure here never
        raises out of this method.
        """
        if self._fetch_previous_closes:
            self._ensure_previous_closes()

        quotes: dict[str, Quote] = {}
        symbols = list(self._security_ids.keys())
        id_to_symbol = {v: k for k, v in self._security_ids.items()}

        for i in range(0, len(symbols), MAX_SECURITIES_PER_REQUEST):
            batch_symbols = symbols[i : i + MAX_SECURITIES_PER_REQUEST]
            security_ids = [int(self._security_ids[s]) for s in batch_symbols]
            try:
                response: dict[str, Any] = {}
                for _ in range(3):
                    get_dhan_quote_api_limiter().acquire_sync()
                    response = self._client.quote_data({self._exchange_segment: security_ids})
                    if response.get("status") == "success":
                        break
                if response.get("status") != "success":
                    logger.warning("DhanHQ quote_data failed: %s", response.get("remarks"))
                    continue
                # Confirmed via a live call: quote_data double-nests under "data"
                # (unlike ohlc_data's single nesting) — {"data": {"data": {...}}}.
                segment_data = (
                    response.get("data", {}).get("data", {}).get(self._exchange_segment, {})
                )
                for sec_id_str, payload in segment_data.items():
                    symbol = id_to_symbol.get(str(sec_id_str))
                    if symbol is None:
                        continue
                    raw_time = now_ist()
                    raw_last_trade_time = payload.get("last_trade_time")
                    if isinstance(raw_last_trade_time, str) and raw_last_trade_time.strip():
                        try:
                            raw_time = datetime.strptime(
                                raw_last_trade_time.strip(), "%d/%m/%Y %H:%M:%S"
                            ).replace(tzinfo=IST)
                        except ValueError:
                            pass
                    emit_raw_event(
                        self._raw_event_sink,
                        RawMarketEvent.create(
                            market=Market.NSE,
                            provider=MarketProvider.DHAN,
                            event_type=RawEventType.QUOTE,
                            symbol=symbol,
                            channel="quote_data",
                            source_event_time=raw_time,
                            payload=payload,
                        ),
                    )
                    previous_close = self._previous_closes.get(symbol)
                    quote = _parse_quote(symbol, payload, previous_close)
                    if quote is not None:
                        quotes[symbol] = quote
            except Exception:
                logger.warning("Error fetching a DhanHQ quote batch", exc_info=True)

        return quotes


class QuotesFeed:
    """Thin wrapper around DhanQuotesFeed, gated by enable_dhan_quotes. Returns
    an empty dict (never raises) if disabled or if DhanQuotesFeed fails to
    initialize — callers already treat "no quotes" as a normal, poll-again-
    next-cycle outcome."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        *,
        raw_event_sink: RawEventSink | None = None,
    ):
        settings = get_settings()
        self._dhan: DhanQuotesFeed | None = None
        if settings.enable_dhan_quotes:
            try:
                self._dhan = DhanQuotesFeed(symbols=symbols, raw_event_sink=raw_event_sink)
            except Exception:
                logger.warning("Failed to initialize DhanQuotesFeed", exc_info=True)

    def fetch_quotes(self) -> dict[str, Quote]:
        if self._dhan is None:
            return {}
        try:
            return self._dhan.fetch_quotes()
        except Exception:
            logger.warning("DhanHQ quote fetch failed", exc_info=True)
            return {}
