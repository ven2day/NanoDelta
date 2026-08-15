from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.core.market_data import (
    CanonicalDataIssue,
    InMemoryRawEventSink,
    RawEventType,
    RawMarketEvent,
    SchemaBoundRawMarketRepository,
    emit_raw_event,
    validate_canonical_candle,
)
from src.core.models import CanonicalCandle, Market, MarketProvider, VolumeType
from src.markets.forex.broker.oanda.provider import OandaV20Client
from src.markets.nse.broker.dhan.historical import DhanHistoricalFeed


def _raw_event(*, market: Market = Market.NSE) -> RawMarketEvent:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    return RawMarketEvent.create(
        market=market,
        provider=MarketProvider.DHAN if market is Market.NSE else MarketProvider.OANDA,
        event_type=RawEventType.QUOTE,
        symbol="RELIANCE" if market is Market.NSE else "EUR_USD",
        channel="pricing",
        source_event_time=now,
        received_at=now,
        payload={
            "price": "100",
            "nested": {"value": "original"},
            "access_token": "must-not-persist",
        },
    )


def test_raw_event_is_deterministic_redacted_and_immutable() -> None:
    first = _raw_event()
    second = _raw_event()

    assert first.event_id == second.event_id
    assert first.payload_hash == second.payload_hash
    assert first.payload["access_token"] == "********"
    with pytest.raises(TypeError):
        first.payload["price"] = "200"  # type: ignore[index]

    mutable_copy = first.payload
    mutable_copy["nested"]["value"] = "changed"
    assert first.payload["nested"]["value"] == "original"


def test_in_memory_raw_sink_is_idempotent() -> None:
    sink = InMemoryRawEventSink()
    event = _raw_event()

    assert sink.persist(event)
    assert not sink.persist(event)
    assert sink.events == (event,)


def test_raw_sink_failure_cannot_break_market_data_path() -> None:
    class BrokenSink:
        def persist(self, event: RawMarketEvent) -> bool:
            raise RuntimeError(event.event_id)

    assert not emit_raw_event(BrokenSink(), _raw_event())


def test_canonical_quality_rejects_negative_volume_and_noncanonical_symbol() -> None:
    candle = CanonicalCandle(
        symbol="eur_usd",
        market=Market.FOREX,
        provider=MarketProvider.OANDA,
        timeframe="15m",
        timestamp=datetime.now(UTC),
        open=1.1,
        high=1.2,
        low=1.0,
        close=1.15,
        volume=-1,
        complete=True,
        source="test",
        volume_type=VolumeType.OANDA_TICK_COUNT,
    )

    result = validate_canonical_candle(candle)

    assert not result.accepted
    assert result.issues == (
        CanonicalDataIssue.NON_CANONICAL_SYMBOL,
        CanonicalDataIssue.INVALID_VOLUME,
    )
    with pytest.raises(ValueError, match="NON_CANONICAL_SYMBOL,INVALID_VOLUME"):
        result.require_valid()


class _Result:
    rowcount = 1


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        self.calls.append((str(statement), params))
        return _Result()


class _PostgresEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self) -> None:
        self.connection = _Connection()

    @contextmanager
    def begin(self):  # type: ignore[no-untyped-def]
        yield self.connection


def test_raw_repository_creates_market_schema_and_persists_idempotently() -> None:
    engine = _PostgresEngine()
    repository = SchemaBoundRawMarketRepository(
        engine,  # type: ignore[arg-type]
        market=Market.NSE,
        provider=MarketProvider.DHAN,
    )

    assert repository.persist(_raw_event())
    sql = "\n".join(call[0] for call in engine.connection.calls)
    assert 'CREATE SCHEMA IF NOT EXISTS "nse"' in sql
    assert 'INSERT INTO "nse"."raw_market_events"' in sql
    assert "ON CONFLICT (event_id) DO NOTHING" in sql


def test_raw_repository_rejects_cross_market_event() -> None:
    repository = SchemaBoundRawMarketRepository(
        _PostgresEngine(),  # type: ignore[arg-type]
        market=Market.NSE,
        provider=MarketProvider.DHAN,
    )

    with pytest.raises(ValueError, match="cannot write FOREX"):
        repository.persist(_raw_event(market=Market.FOREX))


@pytest.mark.asyncio
async def test_oanda_candles_emit_raw_before_returning_canonical() -> None:
    sink = InMemoryRawEventSink()
    client = OandaV20Client(
        environment="practice",
        account_id="account",
        access_token="token",
        raw_event_sink=sink,
    )
    client._request_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "candles": [
                {
                    "time": "2026-08-15T12:00:00Z",
                    "mid": {"o": "1.10", "h": "1.20", "l": "1.00", "c": "1.15"},
                    "volume": 42,
                    "complete": True,
                }
            ]
        }
    )

    candles = await client.candles("EUR_USD", "15m")

    assert len(candles) == 1
    assert candles[0].symbol == "EUR_USD"
    assert len(sink.events) == 1
    assert sink.events[0].event_type is RawEventType.CANDLE
    assert sink.events[0].payload["mid"] == {
        "o": "1.10",
        "h": "1.20",
        "l": "1.00",
        "c": "1.15",
    }


def test_dhan_history_emits_exact_window_response() -> None:
    sink = InMemoryRawEventSink()
    feed = DhanHistoricalFeed.__new__(DhanHistoricalFeed)
    feed._raw_event_sink = sink
    response = {
        "status": "success",
        "data": {
            "timestamp": [1_765_800_000],
            "open": [100.0],
            "high": [102.0],
            "low": [99.0],
            "close": [101.0],
            "volume": [1000],
        },
    }
    feed._intraday_response = lambda *args, **kwargs: response  # type: ignore[method-assign]

    frame = feed._fetch_window(
        "2885",
        datetime(2026, 8, 14, tzinfo=UTC),
        datetime(2026, 8, 15, tzinfo=UTC),
        5,
        "RELIANCE",
        "5m",
    )

    assert frame is not None
    assert len(sink.events) == 1
    assert sink.events[0].provider is MarketProvider.DHAN
    assert sink.events[0].payload["data"] == response["data"]
