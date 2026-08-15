"""Poloniex public historical candle and WebSocket client."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from nanodelta.contracts import Market, Provider
from nanodelta.providers.base import (
    HistoricalRequest,
    HttpRequest,
    JsonTransport,
    ProviderClientError,
    RealtimeSubscription,
)
from nanodelta.providers.transports import HttpxJsonTransport, stream_json


class PoloniexClient:
    market = Market.CRYPTO
    provider = Provider.POLONIEX
    _INTERVAL = {
        "1m": "MINUTE_1",
        "5m": "MINUTE_5",
        "15m": "MINUTE_15",
        "30m": "MINUTE_30",
        "1h": "HOUR_1",
        "4h": "HOUR_4",
        "1d": "DAY_1",
    }

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or HttpxJsonTransport()

    def history_request(
        self, request: HistoricalRequest, *, start_ms: int | None = None
    ) -> HttpRequest:
        interval = self._INTERVAL.get(request.timeframe)
        if interval is None:
            raise ValueError(f"unsupported Poloniex timeframe: {request.timeframe}")
        return HttpRequest(
            method="GET",
            url=f"https://api.poloniex.com/markets/{request.symbol.replace('-', '_')}/candles",
            params={
                "interval": interval,
                "limit": min(request.limit, 500),
                "startTime": start_ms or int(request.start.timestamp() * 1000),
                "endTime": int(request.end.timestamp() * 1000),
            },
        )

    async def fetch_candles(self, request: HistoricalRequest) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = int(request.start.timestamp() * 1000)
        while len(rows) < request.limit:
            payload = await self.transport.request(self.history_request(request, start_ms=cursor))
            if not isinstance(payload, list) or not payload:
                break
            page = [self._row(row, request.timeframe) for row in payload]
            rows.extend(page)
            next_cursor = max(int(row["startTime"]) for row in page) + 1
            if next_cursor <= cursor or len(payload) < min(request.limit, 500):
                break
            cursor = next_cursor
        return rows[: request.limit]

    @staticmethod
    def _row(row: Any, timeframe: str) -> dict[str, Any]:
        if isinstance(row, dict):
            return {**row, "interval": timeframe, "settled": True}
        if not isinstance(row, list) or len(row) < 14:
            raise ProviderClientError("Poloniex candle row is malformed")
        return {
            "low": row[0],
            "high": row[1],
            "open": row[2],
            "close": row[3],
            "amount": row[4],
            "quantity": row[5],
            "buyTakerAmount": row[6],
            "buyTakerQuantity": row[7],
            "tradeCount": row[8],
            "ts": row[9],
            "weightedAverage": row[10],
            "interval": timeframe,
            "startTime": row[12],
            "closeTime": row[13],
            "settled": True,
        }

    def subscription(self, symbols: Sequence[str], channel: str) -> RealtimeSubscription:
        allowed = {
            "ticker",
            "trades",
            *{f"candles_{value.lower()}" for value in self._INTERVAL.values()},
        }
        if channel not in allowed:
            raise ValueError(f"unsupported Poloniex realtime channel: {channel}")
        return RealtimeSubscription(
            url="wss://ws.poloniex.com/ws/public",
            subscribe={"event": "subscribe", "channel": [channel], "symbols": list(symbols)},
        )

    async def stream(self, symbols: Sequence[str], channel: str) -> AsyncIterator[dict[str, Any]]:
        async for payload in stream_json(self.subscription(symbols, channel), self._parse_stream):
            yield payload

    @staticmethod
    def _parse_stream(message: Any) -> list[dict[str, Any]]:
        if not isinstance(message, dict) or not isinstance(message.get("data"), list):
            return []
        return [item for item in message["data"] if isinstance(item, dict)]
