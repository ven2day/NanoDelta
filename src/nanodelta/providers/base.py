"""Transport-neutral provider client contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from nanodelta.contracts import Market, Provider, utc


class ProviderCapability(StrEnum):
    HISTORICAL_CANDLES = "historical_candles"
    REALTIME_QUOTES = "realtime_quotes"
    REALTIME_CANDLES = "realtime_candles"
    ORDER_BOOK = "order_book"


class ProviderClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoricalRequest:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    limit: int = 500

    def __post_init__(self) -> None:
        start = utc(self.start, "start")
        end = utc(self.end, "end")
        if start >= end:
            raise ValueError("historical start must be before end")
        if not self.symbol.strip() or not self.timeframe.strip():
            raise ValueError("symbol and timeframe are required")
        if not 1 <= self.limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    params: Mapping[str, object] = field(default_factory=dict, repr=False)
    json_body: Mapping[str, object] | None = None


@dataclass(frozen=True)
class RealtimeSubscription:
    url: str = field(repr=False)
    subscribe: Mapping[str, Any] | None
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


class JsonTransport(Protocol):
    async def request(self, request: HttpRequest) -> Any: ...


class HistoricalClient(Protocol):
    market: Market
    provider: Provider

    async def fetch_candles(self, request: HistoricalRequest) -> list[dict[str, Any]]: ...


class RealtimeClient(Protocol):
    market: Market
    provider: Provider

    def subscription(self, symbols: Sequence[str], channel: str) -> RealtimeSubscription: ...

    def stream(self, symbols: Sequence[str], channel: str) -> AsyncIterator[dict[str, Any]]: ...
