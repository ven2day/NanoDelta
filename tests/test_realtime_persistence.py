from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from nanodelta.contracts import Market, Provider
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import RealtimeSubscription
from nanodelta.providers.registry import default_provider_registry
from nanodelta.runtime.feed_state import FeedStateRecord
from nanodelta.runtime.realtime import FeedState, RealtimeMarketCycle


class MemoryLake:
    def write(self, **kwargs: Any) -> bool:
        del kwargs
        return True


class MemoryFeedState:
    def __init__(self) -> None:
        self.record: FeedStateRecord | None = None
        self.sequences: dict[tuple[Provider, str], int] = {}
        self.sequence_writes: list[tuple[Provider, str, int, int]] = []

    def load(
        self, market: Market
    ) -> tuple[FeedStateRecord | None, dict[tuple[Provider, str], int]]:
        if self.record is not None and self.record.market is not market:
            return None, {}
        return self.record, dict(self.sequences)

    def save(self, record: FeedStateRecord) -> None:
        self.record = record

    def save_sequence(
        self, market: Market, provider: Provider, symbol: str, sequence: int, gap_delta: int
    ) -> None:
        del market
        self.sequences[(provider, symbol)] = max(
            sequence, self.sequences.get((provider, symbol), sequence)
        )
        self.sequence_writes.append((provider, symbol, sequence, gap_delta))


class LifecycleClient:
    def __init__(self, provider: Provider, batches: list[list[dict[str, Any]] | Exception]) -> None:
        self.market = (
            Market.CRYPTO
            if provider in {Provider.OKX, Provider.POLONIEX}
            else Market.FOREX
        )
        self.provider = provider
        self.batches = batches
        self.calls = 0
        self.closes = 0
        self.subscriptions: list[tuple[tuple[str, ...], str]] = []

    def subscription(self, symbols: Sequence[str], channel: str) -> RealtimeSubscription:
        return RealtimeSubscription("fixture://stream", {"symbols": symbols, "channel": channel})

    async def stream(self, symbols: Sequence[str], channel: str) -> AsyncIterator[dict[str, Any]]:
        self.subscriptions.append((tuple(symbols), channel))
        batch = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        try:
            if isinstance(batch, Exception):
                raise batch
            for item in batch:
                yield item
        finally:
            self.closes += 1


def okx(sequence: int, minute: int = 0) -> dict[str, Any]:
    return {
        "arg": {"instId": "BTC-USDT"},
        "data": {
            "last": "65000",
            "lastSz": "0.1",
            "ts": str(1_786_784_400_000 + minute * 60_000),
            "seqId": str(sequence),
        },
    }


def poloniex() -> dict[str, Any]:
    return {
        "symbol": "BTC_USDT",
        "price": "65000",
        "quantity": "0.1",
        "ts": "1786784400000",
        "sequence": "1",
    }


def crypto_cycle(
    primary: LifecycleClient,
    fallback: LifecycleClient,
    *,
    state: MemoryFeedState | None = None,
) -> RealtimeMarketCycle:
    return RealtimeMarketCycle(
        Market.CRYPTO,
        default_provider_registry(),
        {Provider.OKX: primary, Provider.POLONIEX: fallback},
        ["BTC_USDT"],
        {Provider.OKX: "tickers", Provider.POLONIEX: "ticker"},
        EtlPipeline(MemoryLake()),
        max_events=1,
        state_store=state,
    )


@pytest.mark.asyncio
async def test_bounded_slices_reuse_subscription_and_shutdown_closes_once() -> None:
    primary = LifecycleClient(Provider.OKX, [[okx(1), okx(2, 1)]])
    cycle = crypto_cycle(primary, LifecycleClient(Provider.POLONIEX, [[poloniex()]]))

    assert await cycle.run_once() == 1
    assert await cycle.run_once() == 1
    assert primary.calls == 1
    assert primary.subscriptions == [(('BTC_USDT',), "tickers")]
    assert primary.closes == 0

    await cycle.aclose()
    await cycle.aclose()
    assert primary.closes == 1


@pytest.mark.asyncio
async def test_failure_closes_and_next_attempt_restores_full_subscription() -> None:
    primary = LifecycleClient(Provider.OKX, [RuntimeError("socket lost"), [okx(2)]])
    fallback = LifecycleClient(Provider.POLONIEX, [[poloniex()]])
    cycle = crypto_cycle(primary, fallback)

    assert await cycle.run_once() == 1
    assert primary.closes == 1
    assert cycle.snapshot.active_provider is Provider.POLONIEX
    await cycle.aclose()

    cycle.active_index = 0
    assert await cycle.run_once() == 1
    assert primary.subscriptions == [
        (("BTC_USDT",), "tickers"),
        (("BTC_USDT",), "tickers"),
    ]


@pytest.mark.asyncio
async def test_sequence_and_failover_state_survive_cycle_restart() -> None:
    state = MemoryFeedState()
    first = crypto_cycle(
        LifecycleClient(Provider.OKX, [[okx(10)]]),
        LifecycleClient(Provider.POLONIEX, [[poloniex()]]),
        state=state,
    )
    assert await first.run_once() == 1
    await first.aclose()

    restarted = crypto_cycle(
        LifecycleClient(Provider.OKX, [[okx(12)]]),
        LifecycleClient(Provider.POLONIEX, [[poloniex()]]),
        state=state,
    )
    assert await restarted.run_once() == 1
    assert restarted.snapshot.gap_count == 1
    assert state.sequence_writes[-1] == (Provider.OKX, "BTC_USDT", 12, 1)
    assert state.record is not None
    assert state.record.gap_count == 1


@pytest.mark.asyncio
async def test_oanda_without_real_fallback_is_actionably_degraded() -> None:
    state = MemoryFeedState()
    oanda = LifecycleClient(Provider.OANDA, [RuntimeError("pricing unavailable")])
    cycle = RealtimeMarketCycle(
        Market.FOREX,
        default_provider_registry(),
        {Provider.OANDA: oanda},
        ["EUR_USD"],
        {Provider.OANDA: "pricing"},
        EtlPipeline(MemoryLake()),
        max_events=1,
        state_store=state,
    )

    assert cycle.snapshot.fallback_available is False
    assert cycle.snapshot.status_detail == "NO_REALTIME_FALLBACK_CONFIGURED"
    with pytest.raises(RuntimeError, match="pricing unavailable"):
        await cycle.run_once()
    assert cycle.snapshot.state is FeedState.DEGRADED
    assert cycle.snapshot.status_detail == (
        "NO_REALTIME_FALLBACK_CONFIGURED; operator action required"
    )
    assert state.record is not None
    assert state.record.state == "DEGRADED"
