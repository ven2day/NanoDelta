"""Immutable records shared by the three ETL layers."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Market(StrEnum):
    NSE = "nse"
    FOREX = "forex"
    CRYPTO = "crypto"


class Provider(StrEnum):
    DHAN = "dhan"
    TRUEDATA = "truedata"
    OANDA = "oanda"
    OKX = "okx"
    POLONIEX = "poloniex"


class AdvisoryAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    ABSTAIN = "ABSTAIN"


class EventType(StrEnum):
    CANDLE = "candle"
    QUOTE = "quote"
    TRADE = "trade"
    ORDER_BOOK = "order_book"
    INSTRUMENT = "instrument"


def utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def stable_id(*parts: object) -> str:
    text = "|".join(str(part) for part in parts)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class RawRecord:
    record_id: str
    market: Market
    provider: Provider
    event_type: EventType
    provider_symbol: str
    received_at: datetime
    payload: dict[str, Any] = field(repr=False)
    schema_version: int = 1

    @classmethod
    def create(
        cls,
        *,
        market: Market,
        provider: Provider,
        event_type: EventType,
        provider_symbol: str,
        received_at: datetime,
        payload: dict[str, Any],
    ) -> RawRecord:
        received = utc(received_at, "received_at")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        record_id = stable_id(
            market.value,
            provider.value,
            event_type.value,
            provider_symbol,
            encoded,
        )
        return cls(
            record_id=record_id,
            market=market,
            provider=provider,
            event_type=event_type,
            provider_symbol=provider_symbol,
            received_at=received,
            payload=json.loads(encoded),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["market"] = self.market.value
        result["provider"] = self.provider.value
        result["event_type"] = self.event_type.value
        result["received_at"] = self.received_at.isoformat()
        return result


@dataclass(frozen=True)
class CanonicalCandle:
    record_id: str
    raw_record_id: str
    market: Market
    provider: Provider
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_settled: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        utc(self.open_time, "open_time")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("OHLCV values must be finite")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        if self.low > self.high:
            raise ValueError("low cannot exceed high")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["market"] = self.market.value
        result["provider"] = self.provider.value
        result["open_time"] = self.open_time.isoformat()
        return result


@dataclass(frozen=True)
class FeatureRecord:
    record_id: str
    candle_record_id: str
    market: Market
    symbol: str
    timeframe: str
    event_time: datetime
    close: float
    return_1: float
    range_pct: float
    body_pct: float
    volume_change: float | None
    feature_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["market"] = self.market.value
        result["event_time"] = self.event_time.isoformat()
        return result
