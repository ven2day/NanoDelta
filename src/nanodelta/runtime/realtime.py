"""Realtime provider composition for supervised, paper-only market workers."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from nanodelta.contracts import EventType, Market, Provider
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import ProviderCapability, RealtimeClient
from nanodelta.providers.registry import ProviderRegistry


class FeedState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED_OVER = "FAILED_OVER"


@dataclass(frozen=True)
class QuoteEvent:
    market: Market
    provider: Provider
    symbol: str
    event_time: datetime
    price: float
    volume: float = 0
    sequence: int | None = None


@dataclass(frozen=True)
class StreamSnapshot:
    market: Market
    active_provider: Provider
    state: FeedState
    connected_at: datetime
    last_event_at: datetime | None = None
    gap_count: int = 0
    failover_count: int = 0
    last_error: str | None = None


@dataclass(frozen=True)
class SettledCandle:
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleBuilder:
    """Builds UTC-aligned bars and emits only a prior, therefore settled, bar."""

    def __init__(self, timeframe_seconds: int = 60) -> None:
        if timeframe_seconds <= 0:
            raise ValueError("timeframe_seconds must be positive")
        self._seconds = timeframe_seconds
        self._forming: dict[str, SettledCandle] = {}

    def add(self, quote: QuoteEvent) -> SettledCandle | None:
        timestamp = int(quote.event_time.astimezone(UTC).timestamp())
        opened = datetime.fromtimestamp(timestamp - timestamp % self._seconds, UTC)
        current = self._forming.get(quote.symbol)
        if current is None or opened > current.open_time:
            self._forming[quote.symbol] = SettledCandle(
                quote.symbol,
                f"{self._seconds // 60}m",
                opened,
                quote.price,
                quote.price,
                quote.price,
                quote.price,
                quote.volume,
            )
            return current
        if opened < current.open_time:
            return None
        self._forming[quote.symbol] = replace(
            current,
            high=max(current.high, quote.price),
            low=min(current.low, quote.price),
            close=quote.price,
            volume=current.volume + quote.volume,
        )
        return None


def _time(value: object, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if isinstance(value, int | float) or str(value).isdigit():
        raw = float(str(value))
        if raw > 10_000_000_000:
            raw /= 1000
        return datetime.fromtimestamp(raw, UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("provider event time must include a timezone")
    return parsed.astimezone(UTC)


def normalize_quote(
    market: Market,
    provider: Provider,
    payload: Mapping[str, Any],
    *,
    received_at: datetime,
    symbols: Mapping[str, str] | None = None,
) -> QuoteEvent | None:
    """Normalize supported quote payloads; heartbeats and control frames return None."""
    mapping = symbols or {}
    provider_symbol: object | None = None
    price: object | None = None
    volume: object = 0
    event_time: object | None = None
    sequence: object | None = None
    if provider is Provider.DHAN:
        provider_symbol = payload.get("security_id")
        price = payload.get("ltp")
        volume = payload.get("last_trade_quantity", 0)
        event_time = payload.get("last_trade_time")
    elif provider is Provider.TRUEDATA:
        provider_symbol = payload.get("symbol", payload.get("symbol_id"))
        price = payload.get("ltp")
        volume = payload.get("ltq", 0)
        event_time = payload.get("ltt")
    elif provider is Provider.OANDA:
        provider_symbol = payload.get("instrument")
        bids, asks = payload.get("bids", []), payload.get("asks", [])
        if bids and asks:
            price = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
        event_time = payload.get("time")
    elif provider is Provider.OKX:
        argument = payload.get("arg", {})
        data = payload.get("data", {})
        provider_symbol = argument.get("instId") if isinstance(argument, dict) else None
        if isinstance(data, dict):
            price, volume, event_time = data.get("last"), data.get("lastSz", 0), data.get("ts")
            sequence = data.get("seqId")
    elif provider is Provider.POLONIEX:
        provider_symbol = payload.get("symbol")
        price = payload.get("price", payload.get("close"))
        volume = payload.get("quantity", payload.get("amount", 0))
        event_time = payload.get("ts", payload.get("time"))
        sequence = payload.get("id", payload.get("sequence"))
    if provider_symbol is None or price is None:
        return None
    raw_symbol = str(provider_symbol)
    symbol = mapping.get(raw_symbol, raw_symbol.replace("-", "_"))
    return QuoteEvent(
        market,
        provider,
        symbol,
        _time(event_time, received_at),
        float(str(price)),
        float(str(volume)),
        int(str(sequence)) if sequence is not None else None,
    )


Clock = Callable[[], datetime]


class RealtimeMarketCycle:
    """Consumes a bounded stream slice with primary/fallback and recovery hysteresis."""

    def __init__(
        self,
        market: Market,
        registry: ProviderRegistry,
        clients: Mapping[Provider, RealtimeClient],
        symbols: Sequence[str],
        channels: Mapping[Provider, str],
        pipeline: EtlPipeline,
        *,
        symbol_maps: Mapping[Provider, Mapping[str, str]] | None = None,
        subscription_symbols: Mapping[Provider, Sequence[str]] | None = None,
        max_events: int = 100,
        staleness_seconds: float = 30,
        recovery_successes: int = 3,
        recovery_cooldown_seconds: float = 30,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        route = registry.route(market, ProviderCapability.REALTIME_QUOTES)
        if any(provider not in clients for provider in route):
            raise ValueError("every routed realtime provider requires a client")
        if max_events <= 0 or staleness_seconds <= 0 or recovery_successes <= 0:
            raise ValueError("realtime limits must be positive")
        self.market, self.route, self.clients = market, route, clients
        self.symbols, self.channels, self.pipeline = tuple(symbols), channels, pipeline
        self.symbol_maps = symbol_maps or {}
        self.subscription_symbols = subscription_symbols or {}
        self.max_events, self.staleness = max_events, staleness_seconds
        self.recovery_successes, self.recovery_cooldown = (
            recovery_successes,
            timedelta(seconds=recovery_cooldown_seconds),
        )
        self.clock, self.builder = clock, CandleBuilder()
        self.active_index = 0
        self._primary_successes = 0
        self._failed_over_at: datetime | None = None
        now = clock()
        self.snapshot = StreamSnapshot(market, route[0], FeedState.HEALTHY, now)
        self._sequences: dict[tuple[Provider, str], int] = {}

    async def __call__(self, market: Market) -> None:
        if market is not self.market:
            raise ValueError("market worker called a mismatched realtime cycle")
        await self.run_once()

    async def run_once(self) -> int:
        if self.active_index and self._recovery_due():
            if await self._probe_primary():
                self._primary_successes += 1
                if self._primary_successes >= self.recovery_successes:
                    self.active_index, self._primary_successes = 0, 0
            else:
                self._primary_successes = 0
        for index in range(self.active_index, len(self.route)):
            provider = self.route[index]
            try:
                count = await self._consume(provider)
                self.active_index = index
                state = FeedState.HEALTHY if index == 0 else FeedState.FAILED_OVER
                self.snapshot = replace(self.snapshot, active_provider=provider, state=state)
                return count
            except Exception as exc:
                if index + 1 == len(self.route):
                    self.snapshot = replace(
                        self.snapshot, state=FeedState.DEGRADED, last_error=str(exc)
                    )
                    raise
                self.active_index = index + 1
                self._failed_over_at = self.clock()
                self.snapshot = replace(
                    self.snapshot,
                    active_provider=self.route[index + 1],
                    state=FeedState.FAILED_OVER,
                    failover_count=self.snapshot.failover_count + 1,
                    last_error=str(exc),
                )
        return 0

    def _recovery_due(self) -> bool:
        return self._failed_over_at is not None and self.clock() >= (
            self._failed_over_at + self.recovery_cooldown
        )

    async def _probe_primary(self) -> bool:
        try:
            return await self._consume(self.route[0], limit=1, persist=False) == 1
        except Exception:
            return False

    async def _consume(
        self, provider: Provider, *, limit: int | None = None, persist: bool = True
    ) -> int:
        client = self.clients[provider]
        count = 0
        started = self.clock()
        subscribed = self.subscription_symbols.get(provider, self.symbols)
        iterator = client.stream(subscribed, self.channels[provider]).__aiter__()
        target = limit or self.max_events
        try:
            while count < target:
                try:
                    payload = await asyncio.wait_for(iterator.__anext__(), timeout=self.staleness)
                except StopAsyncIteration:
                    break
                quote = normalize_quote(
                    self.market,
                    provider,
                    payload,
                    received_at=self.clock(),
                    symbols=self.symbol_maps.get(provider),
                )
                if quote is None:
                    continue
                self._track_sequence(quote)
                if persist:
                    self._persist_quote_and_settled(quote)
                count += 1
                self.snapshot = replace(
                    self.snapshot,
                    active_provider=provider,
                    last_event_at=quote.event_time,
                    last_error=None,
                )
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()
        if count == 0 or (self.clock() - started).total_seconds() > self.staleness:
            raise TimeoutError(f"{provider.value} realtime stream is stale")
        return count

    def _track_sequence(self, quote: QuoteEvent) -> None:
        if quote.sequence is None:
            return
        key = (quote.provider, quote.symbol)
        previous = self._sequences.get(key)
        if previous is not None and quote.sequence > previous + 1:
            self.snapshot = replace(self.snapshot, gap_count=self.snapshot.gap_count + 1)
        if previous is None or quote.sequence > previous:
            self._sequences[key] = quote.sequence

    def _persist_quote_and_settled(self, quote: QuoteEvent) -> None:
        self.pipeline.ingest(
            market=quote.market,
            provider=quote.provider,
            event_type=EventType.QUOTE,
            provider_symbol=quote.symbol,
            payload={
                "symbol": quote.symbol,
                "time": quote.event_time.isoformat(),
                "price": quote.price,
                "volume": quote.volume,
                "sequence": quote.sequence,
            },
            received_at=self.clock(),
        )
        candle = self.builder.add(quote)
        if candle is None:
            return
        payload = self._candle_payload(quote.provider, candle)
        self.pipeline.ingest(
            market=quote.market,
            provider=quote.provider,
            event_type=EventType.CANDLE,
            provider_symbol=quote.symbol,
            payload=payload,
            received_at=self.clock(),
        )

    @staticmethod
    def _candle_payload(provider: Provider, candle: SettledCandle) -> dict[str, object]:
        common = {
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
        }
        if provider is Provider.DHAN:
            return {
                **common,
                "timestamp": candle.open_time.isoformat(),
                "volume": candle.volume,
                "timeframe": candle.timeframe,
                "settled": True,
            }
        if provider is Provider.TRUEDATA:
            return {
                "time": candle.open_time.isoformat(),
                "o": candle.open,
                "h": candle.high,
                "l": candle.low,
                "c": candle.close,
                "v": candle.volume,
                "interval": candle.timeframe,
                "complete": True,
            }
        if provider is Provider.OANDA:
            return {
                "time": candle.open_time.isoformat(),
                "mid": {"o": candle.open, "h": candle.high, "l": candle.low, "c": candle.close},
                "volume": candle.volume,
                "timeframe": candle.timeframe,
                "complete": True,
            }
        if provider is Provider.OKX:
            return {
                "ts": str(int(candle.open_time.timestamp() * 1000)),
                "o": candle.open,
                "h": candle.high,
                "l": candle.low,
                "c": candle.close,
                "vol": candle.volume,
                "bar": candle.timeframe,
                "confirm": "1",
            }
        return {
            **common,
            "quantity": candle.volume,
            "interval": candle.timeframe,
            "startTime": str(int(candle.open_time.timestamp() * 1000)),
            "settled": True,
        }


async def reconnect_delay(
    attempt: int, *, base_seconds: float = 0.5, cap_seconds: float = 30, jitter: float = 0.2
) -> None:
    """Injectable full-jitter backoff helper for provider transports."""
    delay = min(cap_seconds, base_seconds * (2**attempt))
    await asyncio.sleep(delay * random.uniform(1 - jitter, 1 + jitter))
