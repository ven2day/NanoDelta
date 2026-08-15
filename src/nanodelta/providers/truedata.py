"""Adapter around TrueData's official Python historical and realtime SDKs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC
from typing import Any, Protocol, cast

from nanodelta.contracts import Market, Provider
from nanodelta.providers.base import HistoricalRequest, ProviderClientError, RealtimeSubscription


class HistorySdk(Protocol):
    def get_historic_data(
        self, symbol: str, start_time: str, end_time: str, bar_size: str
    ) -> Any: ...


class LiveSdk(Protocol):
    def start_live_data(self, symbols: list[str]) -> list[int]: ...

    def disconnect(self) -> None: ...


class TrueDataClient:
    market = Market.NSE
    provider = Provider.TRUEDATA

    def __init__(
        self,
        *,
        username: str,
        password: str,
        history_factory: Callable[[str, str], HistorySdk] | None = None,
        live_factory: Callable[[str, str], LiveSdk] | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self._history_factory = history_factory or self._default_history_factory
        self._live_factory = live_factory or self._default_live_factory

    @staticmethod
    def _default_history_factory(username: str, password: str) -> HistorySdk:
        try:
            from truedata import TD_hist  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ProviderClientError("install NanoDelta with the 'truedata' extra") from exc
        return cast(HistorySdk, TD_hist(username, password))

    @staticmethod
    def _default_live_factory(username: str, password: str) -> LiveSdk:
        try:
            from truedata import TD_live
        except ImportError as exc:
            raise ProviderClientError("install NanoDelta with the 'truedata' extra") from exc
        return cast(LiveSdk, TD_live(username, password))

    async def fetch_candles(self, request: HistoricalRequest) -> list[dict[str, Any]]:
        sdk = self._history_factory(self.username, self.password)
        result = await asyncio.to_thread(
            sdk.get_historic_data,
            request.symbol,
            request.start.astimezone(UTC).isoformat(),
            request.end.astimezone(UTC).isoformat(),
            request.timeframe,
        )
        records = result.to_dict("records") if hasattr(result, "to_dict") else result
        if not isinstance(records, list):
            raise ProviderClientError("TrueData historical response must be tabular")
        return [self._normalize_row(row, request.timeframe) for row in records]

    @staticmethod
    def _normalize_row(row: Any, timeframe: str) -> dict[str, Any]:
        if not isinstance(row, dict):
            raise ProviderClientError("TrueData historical row must be an object")
        aliases = {
            "time": row.get("time", row.get("timestamp", row.get("datetime"))),
            "o": row.get("o", row.get("open")),
            "h": row.get("h", row.get("high")),
            "l": row.get("l", row.get("low")),
            "c": row.get("c", row.get("close")),
            "v": row.get("v", row.get("volume", 0)),
            "interval": timeframe,
            "complete": True,
        }
        if any(aliases[key] is None for key in ("time", "o", "h", "l", "c")):
            raise ProviderClientError("TrueData historical row is missing OHLC/time fields")
        return aliases

    def subscription(self, symbols: Sequence[str], channel: str) -> RealtimeSubscription:
        if channel not in {"ticks", "bars_1m", "bars_5m"}:
            raise ValueError(f"unsupported TrueData realtime channel: {channel}")
        return RealtimeSubscription(
            url="sdk://truedata",
            subscribe={"channel": channel, "symbols": list(symbols)},
        )

    async def stream(self, symbols: Sequence[str], channel: str) -> AsyncIterator[dict[str, Any]]:
        self.subscription(symbols, channel)
        sdk = self._live_factory(self.username, self.password)
        await asyncio.to_thread(sdk.start_live_data, list(symbols))
        source_name = "live_data" if channel == "ticks" else "min_live_data"
        seen: dict[object, tuple[tuple[str, object], ...]] = {}
        try:
            while True:
                live_data = getattr(sdk, source_name, {})
                if isinstance(live_data, dict):
                    for symbol_id, tick in list(live_data.items()):
                        payload = self._tick(symbol_id, tick)
                        fingerprint = tuple(sorted(payload.items(), key=lambda item: item[0]))
                        if seen.get(symbol_id) != fingerprint:
                            seen[symbol_id] = fingerprint
                            yield payload
                await asyncio.sleep(0.25)
        finally:
            await asyncio.to_thread(sdk.disconnect)

    @staticmethod
    def _tick(symbol_id: object, tick: object) -> dict[str, Any]:
        fields = ("symbol", "ltp", "ltt", "ltq", "volume", "bid", "ask")
        result = {"symbol_id": symbol_id}
        for field in fields:
            value = getattr(tick, field, None)
            if value is not None:
                result[field] = value
        return result
