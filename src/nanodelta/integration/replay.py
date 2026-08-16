"""Read-only deterministic provider client for captured fixture payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanodelta.contracts import Market, Provider
from nanodelta.providers.base import HistoricalRequest


class RecordedHistoricalClient:
    def __init__(self, market: Market, provider: Provider, fixture: Path) -> None:
        self.market = market
        self.provider = provider
        self._fixture = fixture

    async def fetch_candles(self, request: HistoricalRequest) -> list[dict[str, Any]]:
        document = json.loads(self._fixture.read_text(encoding="utf-8"))
        if document.get("market") != self.market.value:
            raise ValueError("fixture market does not match replay client")
        if document.get("provider") != self.provider.value:
            raise ValueError("fixture provider does not match replay client")
        if document.get("symbol") != request.symbol:
            raise ValueError("fixture symbol does not match historical request")
        records = document.get("records")
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ValueError("fixture records must be a list of objects")
        return records
