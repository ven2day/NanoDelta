"""OKX public historical candle and WebSocket client."""

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


class OkxClient:
    market = Market.CRYPTO
    provider = Provider.OKX
    _BAR = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1H",
        "4h": "4H",
        "1d": "1Dutc",
    }

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or HttpxJsonTransport()

    def history_request(
        self, request: HistoricalRequest, *, after: str | None = None
    ) -> HttpRequest:
        bar = self._BAR.get(request.timeframe)
        if bar is None:
            raise ValueError(f"unsupported OKX timeframe: {request.timeframe}")
        params: dict[str, object] = {
            "instId": request.symbol.replace("_", "-"),
            "bar": bar,
            "limit": min(request.limit, 300),
            "before": str(int(request.start.timestamp() * 1000)),
        }
        if after is not None:
            params["after"] = after
        else:
            params["after"] = str(int(request.end.timestamp() * 1000))
        return HttpRequest(
            method="GET", url="https://www.okx.com/api/v5/market/history-candles", params=params
        )

    async def fetch_candles(self, request: HistoricalRequest) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        after: str | None = None
        while len(rows) < request.limit:
            payload = await self.transport.request(self.history_request(request, after=after))
            if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                raise ProviderClientError("OKX returned an unsuccessful candle response")
            data = payload.get("data", [])
            if not isinstance(data, list) or not data:
                break
            page = [self._row(row, request.timeframe) for row in data]
            rows.extend(page)
            next_after = str(data[-1][0])
            if next_after == after or len(data) < min(request.limit, 300):
                break
            after = next_after
        return rows[: request.limit]

    @staticmethod
    def _row(row: Any, timeframe: str) -> dict[str, Any]:
        if not isinstance(row, list) or len(row) < 9:
            raise ProviderClientError("OKX candle row is malformed")
        ts, open_, high, low, close, volume, volume_ccy, volume_quote, confirm = row[:9]
        return {
            "ts": ts,
            "o": open_,
            "h": high,
            "l": low,
            "c": close,
            "vol": volume,
            "volCcy": volume_ccy,
            "volCcyQuote": volume_quote,
            "confirm": confirm,
            "bar": timeframe,
        }

    def subscription(self, symbols: Sequence[str], channel: str) -> RealtimeSubscription:
        allowed = {"tickers", "books5", *{f"candle{bar}" for bar in self._BAR.values()}}
        if channel not in allowed:
            raise ValueError(f"unsupported OKX realtime channel: {channel}")
        return RealtimeSubscription(
            url="wss://ws.okx.com:8443/ws/v5/public",
            subscribe={
                "op": "subscribe",
                "args": [
                    {"channel": channel, "instId": symbol.replace("_", "-")} for symbol in symbols
                ],
            },
        )

    async def stream(self, symbols: Sequence[str], channel: str) -> AsyncIterator[dict[str, Any]]:
        async for payload in stream_json(self.subscription(symbols, channel), self._parse_stream):
            yield payload

    @staticmethod
    def _parse_stream(message: Any) -> list[dict[str, Any]]:
        if not isinstance(message, dict) or not isinstance(message.get("data"), list):
            return []
        argument = message.get("arg", {})
        return [{"arg": argument, "data": item} for item in message["data"]]
