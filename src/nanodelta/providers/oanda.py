"""OANDA v20 historical candle and pricing-stream client."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from nanodelta.contracts import Market, Provider
from nanodelta.providers.base import (
    HistoricalRequest,
    HttpRequest,
    JsonTransport,
    RealtimeSubscription,
)
from nanodelta.providers.transports import HttpxJsonTransport, stream_http_json_lines


class OandaClient:
    market = Market.FOREX
    provider = Provider.OANDA
    _GRANULARITY = {
        "1m": "M1",
        "5m": "M5",
        "15m": "M15",
        "30m": "M30",
        "1h": "H1",
        "4h": "H4",
        "1d": "D",
    }

    def __init__(
        self,
        *,
        account_id: str,
        access_token: str,
        practice: bool = True,
        transport: JsonTransport | None = None,
    ) -> None:
        self.account_id = account_id
        self.access_token = access_token
        host = "api-fxpractice.oanda.com" if practice else "api-fxtrade.oanda.com"
        stream_host = "stream-fxpractice.oanda.com" if practice else "stream-fxtrade.oanda.com"
        self.base_url = f"https://{host}"
        self.stream_url = f"https://{stream_host}"
        self.transport = transport or HttpxJsonTransport()

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def history_request(self, request: HistoricalRequest) -> HttpRequest:
        granularity = self._GRANULARITY.get(request.timeframe)
        if granularity is None:
            raise ValueError(f"unsupported OANDA timeframe: {request.timeframe}")
        return HttpRequest(
            method="GET",
            url=f"{self.base_url}/v3/instruments/{request.symbol}/candles",
            headers=self.headers,
            params={
                "from": request.start.isoformat(),
                "to": request.end.isoformat(),
                "granularity": granularity,
                "price": "M",
            },
        )

    async def fetch_candles(self, request: HistoricalRequest) -> list[dict[str, Any]]:
        payload = await self.transport.request(self.history_request(request))
        candles = payload.get("candles", []) if isinstance(payload, dict) else []
        rows = [
            {**candle, "granularity": self._GRANULARITY[request.timeframe]}
            for candle in candles
            if isinstance(candle, dict)
        ]
        return rows[: request.limit]

    def subscription(self, symbols: Sequence[str], channel: str) -> RealtimeSubscription:
        if channel != "pricing":
            raise ValueError(f"unsupported OANDA realtime channel: {channel}")
        instruments = ",".join(symbols)
        return RealtimeSubscription(
            url=(
                f"{self.stream_url}/v3/accounts/{self.account_id}/pricing/stream"
                f"?instruments={instruments}"
            ),
            subscribe=None,
            headers=self.headers,
        )

    async def stream(self, symbols: Sequence[str], channel: str) -> AsyncIterator[dict[str, Any]]:
        async for payload in stream_http_json_lines(
            self.subscription(symbols, channel), self._parse_stream
        ):
            yield payload

    @staticmethod
    def _parse_stream(message: Any) -> list[dict[str, Any]]:
        return [message] if isinstance(message, dict) and message.get("type") == "PRICE" else []
