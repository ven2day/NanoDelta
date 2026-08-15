from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nanodelta.contracts import Market, Provider
from nanodelta.providers import (
    DhanClient,
    HistoricalRequest,
    OandaClient,
    OkxClient,
    PoloniexClient,
    TrueDataClient,
    default_provider_registry,
)
from nanodelta.providers.base import HttpRequest, ProviderCapability

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)


class FakeTransport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    async def request(self, request: HttpRequest) -> Any:
        self.requests.append(request)
        return self.responses.pop(0)


def history(symbol: str = "BTC_USDT", timeframe: str = "5m") -> HistoricalRequest:
    return HistoricalRequest(
        symbol=symbol,
        timeframe=timeframe,
        start=NOW - timedelta(days=1),
        end=NOW,
        limit=100,
    )


def test_historical_request_requires_aware_ordered_window() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        HistoricalRequest(symbol="X", timeframe="1m", start=datetime(2026, 1, 1), end=NOW)
    with pytest.raises(ValueError, match="before"):
        HistoricalRequest(symbol="X", timeframe="1m", start=NOW, end=NOW)


@pytest.mark.asyncio
async def test_dhan_builds_authenticated_request_and_unzips_arrays() -> None:
    transport = FakeTransport(
        {
            "timestamp": [1],
            "open": [100],
            "high": [110],
            "low": [90],
            "close": [105],
            "volume": [20],
        }
    )
    client = DhanClient(
        client_id="client",
        access_token="token",
        security_id="1333",
        transport=transport,
    )
    rows = await client.fetch_candles(history("RELIANCE", "5m"))
    assert rows == [
        {
            "timestamp": 1,
            "open": 100,
            "high": 110,
            "low": 90,
            "close": 105,
            "volume": 20,
            "timeframe": "5m",
            "settled": True,
        }
    ]
    request = transport.requests[0]
    assert request.url.endswith("/charts/intraday")
    assert request.headers["access-token"] == "token"


def test_dhan_decodes_little_endian_ticker_packet() -> None:
    packet = bytearray(16)
    packet[0] = 2
    struct.pack_into("<H", packet, 1, 16)
    packet[3] = 1
    struct.pack_into("<I", packet, 4, 1333)
    struct.pack_into("<f", packet, 8, 2500.5)
    struct.pack_into("<I", packet, 12, 1_786_752_000)
    decoded = DhanClient.decode_packet(bytes(packet))
    assert decoded["security_id"] == "1333"
    assert decoded["ltp"] == pytest.approx(2500.5)


@pytest.mark.asyncio
async def test_oanda_uses_v20_candles_and_preserves_complete_flag() -> None:
    transport = FakeTransport(
        {"candles": [{"time": NOW.isoformat(), "complete": True, "mid": {"o": "1"}}]}
    )
    client = OandaClient(
        account_id="account", access_token="token", practice=True, transport=transport
    )
    rows = await client.fetch_candles(history("EUR_USD", "1h"))
    assert rows[0]["complete"] is True
    assert rows[0]["granularity"] == "H1"
    assert rows[0]["timeframe"] == "1h"
    assert "/v3/instruments/EUR_USD/candles" in transport.requests[0].url
    subscription = client.subscription(["EUR_USD"], "pricing")
    assert "/pricing/stream?instruments=EUR_USD" in subscription.url


@pytest.mark.asyncio
async def test_okx_maps_documented_nine_field_candle() -> None:
    transport = FakeTransport(
        {
            "code": "0",
            "data": [["1000", "1", "2", "0.5", "1.5", "10", "10", "15", "1"]],
        }
    )
    rows = await OkxClient(transport).fetch_candles(history())
    assert rows[0]["ts"] == "1000"
    assert rows[0]["confirm"] == "1"
    assert rows[0]["volCcyQuote"] == "15"
    request = transport.requests[0]
    assert request.url.endswith("/api/v5/market/history-candles")
    assert request.params["instId"] == "BTC-USDT"


@pytest.mark.asyncio
async def test_poloniex_uses_forward_window_and_base_volume() -> None:
    row = ["0.5", "2", "1", "1.5", "15", "10", "7", "5", 3, 1001, "1.2", "MINUTE_5", 1000, 1299]
    transport = FakeTransport([row])
    rows = await PoloniexClient(transport).fetch_candles(history())
    assert rows[0]["quantity"] == "10"
    assert rows[0]["amount"] == "15"
    assert transport.requests[0].params["startTime"] < transport.requests[0].params["endTime"]


class FakeHistorySdk:
    def get_historic_data(
        self, symbol: str, start_time: str, end_time: str, bar_size: str
    ) -> list[dict[str, object]]:
        del symbol, start_time, end_time, bar_size
        return [
            {
                "timestamp": NOW.isoformat(),
                "open": 100,
                "high": 110,
                "low": 90,
                "close": 105,
                "volume": 20,
            }
        ]


@pytest.mark.asyncio
async def test_truedata_sdk_adapter_normalizes_historical_rows() -> None:
    client = TrueDataClient(
        username="user",
        password="password",
        history_factory=lambda _user, _password: FakeHistorySdk(),
    )
    rows = await client.fetch_candles(history("RELIANCE", "5m"))
    assert rows[0]["o"] == 100
    assert rows[0]["complete"] is True


def test_default_registry_is_capability_specific_and_market_isolated() -> None:
    registry = default_provider_registry()
    assert registry.route(Market.NSE, ProviderCapability.HISTORICAL_CANDLES) == (
        Provider.DHAN,
        Provider.TRUEDATA,
    )
    assert registry.route(Market.NSE, ProviderCapability.REALTIME_QUOTES) == (
        Provider.TRUEDATA,
        Provider.DHAN,
    )
    assert registry.route(Market.CRYPTO, ProviderCapability.HISTORICAL_CANDLES) == (
        Provider.OKX,
        Provider.POLONIEX,
    )
