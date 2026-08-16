"""Dhan historical charts and realtime-feed subscription client."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import websockets

from nanodelta.contracts import Market, Provider
from nanodelta.providers.base import (
    HistoricalRequest,
    HttpRequest,
    JsonTransport,
    ProviderClientError,
    RealtimeSubscription,
)
from nanodelta.providers.transports import HttpxJsonTransport


class DhanClient:
    market = Market.NSE
    provider = Provider.DHAN
    _INTERVALS = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "15",  # deterministically derived from complete 15-minute pairs
        "1h": "60",
        "60m": "60",
    }
    SUPPORTED_HISTORY_TIMEFRAMES = frozenset((*_INTERVALS, "1d"))

    def __init__(
        self,
        *,
        client_id: str,
        access_token: str,
        security_id: str | None = None,
        security_ids: Mapping[str, str] | None = None,
        exchange_segment: str = "NSE_EQ",
        instrument: str = "EQUITY",
        transport: JsonTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.access_token = access_token
        if security_id is None and not security_ids:
            raise ValueError("security_id or security_ids mapping is required")
        self.security_id = security_id
        self.security_ids = {
            symbol.strip().upper(): value.strip() for symbol, value in (security_ids or {}).items()
        }
        if any(not symbol or not value for symbol, value in self.security_ids.items()):
            raise ValueError("Dhan security ID mappings cannot be empty")
        self.exchange_segment = exchange_segment
        self.instrument = instrument
        self.transport = transport or HttpxJsonTransport()

    def history_request(self, request: HistoricalRequest) -> HttpRequest:
        daily = request.timeframe == "1d"
        if not daily and request.timeframe not in self._INTERVALS:
            raise ValueError(f"Dhan does not directly support {request.timeframe}")
        body: dict[str, object] = {
            "securityId": self._security_id(request.symbol),
            "exchangeSegment": self.exchange_segment,
            "instrument": self.instrument,
            "fromDate": request.start.date().isoformat(),
            "toDate": request.end.date().isoformat(),
        }
        if not daily:
            body["interval"] = self._INTERVALS[request.timeframe]
        endpoint = "historical" if daily else "intraday"
        return HttpRequest(
            method="POST",
            url=f"https://api.dhan.co/v2/charts/{endpoint}",
            headers={
                "access-token": self.access_token,
                "client-id": self.client_id,
                "Content-Type": "application/json",
            },
            json_body=body,
        )

    def _security_id(self, symbol: str) -> str:
        mapped = self.security_ids.get(symbol.strip().upper())
        if mapped is not None:
            return mapped
        if self.security_id is not None:
            return self.security_id
        raise LookupError(f"no Dhan security ID mapping for {symbol}")

    async def fetch_candles(self, request: HistoricalRequest) -> list[dict[str, Any]]:
        payload = await self.transport.request(self.history_request(request))
        if not isinstance(payload, dict):
            raise ProviderClientError("Dhan candle response must be an object")
        fields = ("timestamp", "open", "high", "low", "close", "volume")
        columns = [payload.get(field) for field in fields]
        if not all(isinstance(column, list) for column in columns):
            raise ProviderClientError("Dhan candle response has missing arrays")
        lengths = {len(column) for column in columns if isinstance(column, list)}
        if len(lengths) != 1:
            raise ProviderClientError("Dhan candle arrays have inconsistent lengths")
        rows = [
            {
                **dict(zip(fields, values, strict=True)),
                "timeframe": request.timeframe,
                "settled": True,
            }
            for values in zip(*columns, strict=True)
        ]
        return self._resample_30m(rows) if request.timeframe == "30m" else rows

    @staticmethod
    def _resample_30m(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Aggregate complete 15m pairs on NSE's 09:15 IST session anchor."""
        anchor_seconds = 3 * 3600 + 45 * 60  # 09:15 IST expressed in UTC
        buckets: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        for row in rows:
            raw = row["timestamp"]
            if isinstance(raw, int | float) or str(raw).isdigit():
                timestamp = int(float(raw))
                if timestamp > 10_000_000_000:
                    timestamp //= 1000
            else:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ProviderClientError("Dhan timestamp must include timezone")
                timestamp = int(parsed.astimezone(UTC).timestamp())
            bucket = anchor_seconds + ((timestamp - anchor_seconds) // 1800) * 1800
            buckets.setdefault(bucket, []).append((timestamp, row))
        result = []
        for opened, group in sorted(buckets.items()):
            ordered = [row for _, row in sorted(group, key=lambda value: value[0])]
            if len(ordered) != 2:
                continue
            result.append(
                {
                    "timestamp": opened,
                    "open": ordered[0]["open"],
                    "high": max(float(value["high"]) for value in ordered),
                    "low": min(float(value["low"]) for value in ordered),
                    "close": ordered[-1]["close"],
                    "volume": sum(float(value["volume"]) for value in ordered),
                    "timeframe": "30m",
                    "settled": True,
                }
            )
        return result

    def subscription(self, symbols: Sequence[str], channel: str) -> RealtimeSubscription:
        if len(symbols) > 100:
            raise ValueError("Dhan accepts at most 100 instruments per subscription message")
        request_code = {"ticker": 15, "quote": 17, "full": 21}.get(channel)
        if request_code is None:
            raise ValueError(f"unsupported Dhan realtime channel: {channel}")
        instruments = [
            {"ExchangeSegment": self.exchange_segment, "SecurityId": symbol} for symbol in symbols
        ]
        return RealtimeSubscription(
            url=(
                "wss://api-feed.dhan.co?version=2&token="
                f"{self.access_token}&clientId={self.client_id}&authType=2"
            ),
            subscribe={
                "RequestCode": request_code,
                "InstrumentCount": len(instruments),
                "InstrumentList": instruments,
            },
        )

    async def stream(self, symbols: Sequence[str], channel: str) -> AsyncIterator[dict[str, Any]]:
        subscription = self.subscription(symbols, channel)
        failure: Exception | None = None
        for attempt in range(6):
            try:
                async with websockets.connect(
                    subscription.url, ping_interval=10, ping_timeout=40
                ) as socket:
                    await socket.send(json.dumps(subscription.subscribe, separators=(",", ":")))
                    async for packet in socket:
                        if not isinstance(packet, bytes):
                            continue
                        yield self.decode_packet(packet)
                    return
            except Exception as exc:
                failure = exc
                if attempt == 5:
                    break
                await asyncio.sleep(min(30.0, 0.5 * (2**attempt)))
        raise ProviderClientError(f"Dhan WebSocket stream failed: {failure}")

    @staticmethod
    def decode_packet(packet: bytes) -> dict[str, Any]:
        if len(packet) < 8:
            raise ProviderClientError("Dhan packet is shorter than its 8-byte header")
        response_code = packet[0]
        message_length = struct.unpack_from("<H", packet, 1)[0]
        exchange_segment = packet[3]
        security_id = struct.unpack_from("<I", packet, 4)[0]
        if message_length > len(packet):
            raise ProviderClientError("Dhan packet length header exceeds received bytes")
        result: dict[str, Any] = {
            "response_code": response_code,
            "message_length": message_length,
            "exchange_segment": exchange_segment,
            "security_id": str(security_id),
        }
        if response_code == 2 and len(packet) >= 16:
            result.update(
                ltp=struct.unpack_from("<f", packet, 8)[0],
                last_trade_time=struct.unpack_from("<I", packet, 12)[0],
            )
        elif response_code == 4 and len(packet) >= 50:
            result.update(
                ltp=struct.unpack_from("<f", packet, 8)[0],
                last_trade_quantity=struct.unpack_from("<H", packet, 12)[0],
                last_trade_time=struct.unpack_from("<I", packet, 14)[0],
                average_trade_price=struct.unpack_from("<f", packet, 18)[0],
                volume=struct.unpack_from("<I", packet, 22)[0],
                total_sell_quantity=struct.unpack_from("<I", packet, 26)[0],
                total_buy_quantity=struct.unpack_from("<I", packet, 30)[0],
                day_open=struct.unpack_from("<f", packet, 34)[0],
                day_close=struct.unpack_from("<f", packet, 38)[0],
                day_high=struct.unpack_from("<f", packet, 42)[0],
                day_low=struct.unpack_from("<f", packet, 46)[0],
            )
        elif response_code == 5 and len(packet) >= 12:
            result["open_interest"] = struct.unpack_from("<I", packet, 8)[0]
        elif response_code == 6 and len(packet) >= 16:
            result.update(
                previous_close=struct.unpack_from("<f", packet, 8)[0],
                previous_open_interest=struct.unpack_from("<I", packet, 12)[0],
            )
        elif response_code == 50 and len(packet) >= 10:
            result["disconnect_code"] = struct.unpack_from("<H", packet, 8)[0]
        return result
