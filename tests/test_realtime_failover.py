from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nanodelta.contracts import Market, Provider
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import RealtimeSubscription
from nanodelta.providers.registry import default_provider_registry
from nanodelta.runtime.realtime import (
    CandleBuilder,
    FeedState,
    QuoteEvent,
    RealtimeMarketCycle,
    normalize_quote,
)


class MemoryLake:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, **kwargs: Any) -> bool:
        self.records.append(kwargs)
        return True


class FixtureClient:
    def __init__(self, market: Market, provider: Provider, batches: list[object]) -> None:
        self.market, self.provider, self.batches = market, provider, batches
        self.calls = 0

    def subscription(self, symbols: Sequence[str], channel: str) -> RealtimeSubscription:
        return RealtimeSubscription("fixture://stream", {"symbols": symbols, "channel": channel})

    async def stream(self, symbols: Sequence[str], channel: str) -> AsyncIterator[dict[str, Any]]:
        del symbols, channel
        batch = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        if isinstance(batch, Exception):
            raise batch
        for item in batch:  # type: ignore[union-attr]
            yield item


@pytest.fixture
def streams() -> dict[str, dict[str, Any]]:
    path = Path(__file__).parent / "fixtures/providers/realtime_streams.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_normalizes_all_provider_quote_fixtures(streams: dict[str, dict[str, Any]]) -> None:
    received = datetime(2026, 8, 15, 9, tzinfo=UTC)
    cases = [
        (Market.NSE, Provider.DHAN, "RELIANCE", {"1333": "RELIANCE"}),
        (Market.NSE, Provider.TRUEDATA, "RELIANCE", {}),
        (Market.FOREX, Provider.OANDA, "EUR_USD", {}),
        (Market.CRYPTO, Provider.OKX, "BTC_USDT", {}),
        (Market.CRYPTO, Provider.POLONIEX, "BTC_USDT", {}),
    ]
    for market, provider, symbol, mapping in cases:
        quote = normalize_quote(
            market, provider, streams[provider.value], received_at=received, symbols=mapping
        )
        assert quote is not None
        assert quote.symbol == symbol
        assert quote.price > 0
        assert quote.event_time.tzinfo is UTC


def test_candle_builder_never_emits_forming_bar() -> None:
    builder = CandleBuilder(60)
    first = QuoteEvent(
        Market.NSE, Provider.DHAN, "RELIANCE", datetime(2026, 8, 15, 9, 0, 1, tzinfo=UTC), 100, 2
    )
    second = QuoteEvent(
        Market.NSE, Provider.DHAN, "RELIANCE", datetime(2026, 8, 15, 9, 0, 30, tzinfo=UTC), 102, 3
    )
    next_minute = QuoteEvent(
        Market.NSE, Provider.DHAN, "RELIANCE", datetime(2026, 8, 15, 9, 1, tzinfo=UTC), 101, 1
    )
    assert builder.add(first) is None
    assert builder.add(second) is None
    settled = builder.add(next_minute)
    assert settled is not None
    assert (settled.open, settled.high, settled.low, settled.close, settled.volume) == (
        100,
        102,
        100,
        102,
        5,
    )


@pytest.mark.asyncio
async def test_nse_primary_failure_fails_over_and_restores_with_hysteresis(
    streams: dict[str, dict[str, Any]],
) -> None:
    now = datetime(2026, 8, 15, 9, tzinfo=UTC)
    clock_value = [now]
    primary = FixtureClient(
        Market.NSE,
        Provider.TRUEDATA,
        [
            RuntimeError("socket lost"),
            [streams["truedata"]],
            [streams["truedata"]],
            [streams["truedata"]],
            [streams["truedata"]],
        ],
    )
    fallback = FixtureClient(Market.NSE, Provider.DHAN, [[streams["dhan"]]])
    cycle = RealtimeMarketCycle(
        Market.NSE,
        default_provider_registry(),
        {Provider.TRUEDATA: primary, Provider.DHAN: fallback},
        ["RELIANCE"],
        {Provider.TRUEDATA: "ticks", Provider.DHAN: "quote"},
        EtlPipeline(MemoryLake()),
        symbol_maps={Provider.DHAN: {"1333": "RELIANCE"}},
        max_events=1,
        recovery_successes=3,
        recovery_cooldown_seconds=10,
        clock=lambda: clock_value[0],
    )
    await cycle.run_once()
    assert cycle.snapshot.active_provider is Provider.DHAN
    assert cycle.snapshot.state is FeedState.FAILED_OVER
    assert cycle.snapshot.failover_count == 1

    clock_value[0] += timedelta(seconds=11)
    await cycle.run_once()
    await cycle.run_once()
    assert cycle.snapshot.active_provider is Provider.DHAN
    await cycle.run_once()
    assert cycle.snapshot.active_provider is Provider.TRUEDATA
    assert cycle.snapshot.state is FeedState.HEALTHY


@pytest.mark.asyncio
async def test_sequence_gap_is_recorded_and_only_settled_candle_reaches_silver(
    streams: dict[str, dict[str, Any]],
) -> None:
    first = streams["okx"]
    second = json.loads(json.dumps(first))
    second["data"].update(ts="1786784460000", last="65010", seqId="12")
    lake = MemoryLake()
    okx = FixtureClient(Market.CRYPTO, Provider.OKX, [[first, second]])
    poloniex = FixtureClient(Market.CRYPTO, Provider.POLONIEX, [[streams["poloniex"]]])
    cycle = RealtimeMarketCycle(
        Market.CRYPTO,
        default_provider_registry(),
        {Provider.OKX: okx, Provider.POLONIEX: poloniex},
        ["BTC_USDT"],
        {Provider.OKX: "tickers", Provider.POLONIEX: "ticker"},
        EtlPipeline(lake),
        max_events=2,
    )
    assert await cycle.run_once() == 2
    assert cycle.snapshot.gap_count == 1
    layers = [record["layer"] for record in lake.records]
    assert layers.count("bronze") == 3  # two quotes and one settled candle
    assert layers.count("silver") == 1


@pytest.mark.asyncio
async def test_consecutive_settled_candles_build_gold_and_invoke_decisions(
    streams: dict[str, dict[str, Any]],
) -> None:
    events = []
    for minute, price, sequence in ((0, "65000", "10"), (1, "65100", "11"), (2, "65200", "12")):
        event = json.loads(json.dumps(streams["okx"]))
        event["data"].update(
            ts=str(1_786_784_400_000 + minute * 60_000),
            last=price,
            seqId=sequence,
        )
        events.append(event)
    lake = MemoryLake()
    handled: list[object] = []
    cycle = RealtimeMarketCycle(
        Market.CRYPTO,
        default_provider_registry(),
        {
            Provider.OKX: FixtureClient(Market.CRYPTO, Provider.OKX, [events]),
            Provider.POLONIEX: FixtureClient(
                Market.CRYPTO, Provider.POLONIEX, [[streams["poloniex"]]]
            ),
        },
        ["BTC_USDT"],
        {Provider.OKX: "tickers", Provider.POLONIEX: "ticker"},
        EtlPipeline(lake),
        max_events=3,
        on_features=lambda features: handled.extend(features),
    )

    assert await cycle.run_once() == 3
    layers = [record["layer"] for record in lake.records]
    assert layers.count("silver") == 2
    assert layers.count("gold") == 1
    assert len(handled) == 1


@pytest.mark.asyncio
async def test_all_providers_failed_marks_stream_degraded(
    streams: dict[str, dict[str, Any]],
) -> None:
    del streams
    clients = {
        Provider.OKX: FixtureClient(Market.CRYPTO, Provider.OKX, [RuntimeError("okx down")]),
        Provider.POLONIEX: FixtureClient(
            Market.CRYPTO, Provider.POLONIEX, [RuntimeError("polo down")]
        ),
    }
    cycle = RealtimeMarketCycle(
        Market.CRYPTO,
        default_provider_registry(),
        clients,
        ["BTC_USDT"],
        {Provider.OKX: "tickers", Provider.POLONIEX: "ticker"},
        EtlPipeline(MemoryLake()),
        max_events=1,
    )
    with pytest.raises(RuntimeError, match="polo down"):
        await cycle.run_once()
    assert cycle.snapshot.state is FeedState.DEGRADED
