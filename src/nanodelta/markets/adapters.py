"""Canonical candle adapters for every supported market provider."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from nanodelta.contracts import CanonicalCandle, Market, Provider, RawRecord, stable_id

Adapter = Callable[[RawRecord], CanonicalCandle]


def _float(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if value is None or value == "":
        raise ValueError(f"missing required field: {key}")
    return float(value)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("provider timestamp must be timezone-aware")
        return value.astimezone(UTC)
    if isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=UTC)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("provider timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _candle(
    raw: RawRecord,
    *,
    symbol: str,
    timeframe: str,
    open_time: datetime,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    settled: bool,
) -> CanonicalCandle:
    identity = stable_id(raw.market.value, symbol, timeframe, open_time.isoformat())
    return CanonicalCandle(
        record_id=identity,
        raw_record_id=raw.record_id,
        market=raw.market,
        provider=raw.provider,
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        is_settled=settled,
    )


def _dhan(raw: RawRecord) -> CanonicalCandle:
    p = raw.payload
    return _candle(
        raw,
        symbol=raw.provider_symbol.upper(),
        timeframe=str(p.get("timeframe", "1m")),
        open_time=_timestamp(p["timestamp"]),
        open_price=_float(p, "open"),
        high=_float(p, "high"),
        low=_float(p, "low"),
        close=_float(p, "close"),
        volume=_float(p, "volume"),
        settled=bool(p.get("settled", True)),
    )


def _truedata(raw: RawRecord) -> CanonicalCandle:
    p = raw.payload
    return _candle(
        raw,
        symbol=raw.provider_symbol.upper(),
        timeframe=str(p.get("interval", "1m")),
        open_time=_timestamp(p["time"]),
        open_price=_float(p, "o"),
        high=_float(p, "h"),
        low=_float(p, "l"),
        close=_float(p, "c"),
        volume=_float(p, "v"),
        settled=bool(p.get("complete", True)),
    )


def _oanda(raw: RawRecord) -> CanonicalCandle:
    p = raw.payload
    mid = p.get("mid")
    if not isinstance(mid, dict):
        raise ValueError("missing required field: mid")
    return _candle(
        raw,
        symbol=raw.provider_symbol.upper().replace("-", "_"),
        timeframe=str(p.get("timeframe", "1m")),
        open_time=_timestamp(p["time"]),
        open_price=_float(mid, "o"),
        high=_float(mid, "h"),
        low=_float(mid, "l"),
        close=_float(mid, "c"),
        volume=_float(p, "volume"),
        settled=bool(p.get("complete", False)),
    )


def _okx(raw: RawRecord) -> CanonicalCandle:
    p = raw.payload
    return _candle(
        raw,
        symbol=raw.provider_symbol.upper().replace("-", "_"),
        timeframe=str(p.get("bar", "1m")),
        open_time=_timestamp(p["ts"]),
        open_price=_float(p, "o"),
        high=_float(p, "h"),
        low=_float(p, "l"),
        close=_float(p, "c"),
        volume=_float(p, "vol"),
        settled=str(p.get("confirm", "0")) == "1",
    )


def _poloniex(raw: RawRecord) -> CanonicalCandle:
    p = raw.payload
    return _candle(
        raw,
        symbol=raw.provider_symbol.upper().replace("-", "_"),
        timeframe=str(p.get("interval", "MINUTE_1")).lower(),
        open_time=_timestamp(p["startTime"]),
        open_price=_float(p, "open"),
        high=_float(p, "high"),
        low=_float(p, "low"),
        close=_float(p, "close"),
        volume=_float(p, "quantity"),
        settled=bool(p.get("settled", True)),
    )


_ADAPTERS: dict[Provider, tuple[Market, Adapter]] = {
    Provider.DHAN: (Market.NSE, _dhan),
    Provider.TRUEDATA: (Market.NSE, _truedata),
    Provider.OANDA: (Market.FOREX, _oanda),
    Provider.OKX: (Market.CRYPTO, _okx),
    Provider.POLONIEX: (Market.CRYPTO, _poloniex),
}


def adapter_for(market: Market, provider: Provider) -> Adapter:
    expected_market, adapter = _ADAPTERS[provider]
    if market is not expected_market:
        raise ValueError(f"{provider.value} cannot ingest {market.value} data")
    return adapter
