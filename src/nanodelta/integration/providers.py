"""Provider fallback composition with auditable ETL reconciliation.

This module deliberately works with historical, settled candles. Realtime clients
remain responsible for connection/reconnection; a stream packet must be converted
to a provider candle before it can enter this authoritative ETL path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from nanodelta.contracts import CanonicalCandle, EventType, Market, Provider, utc
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import HistoricalClient, HistoricalRequest
from nanodelta.providers.registry import ProviderRegistry


class ProviderFetchError(RuntimeError):
    """All configured providers failed without producing authoritative data."""


@dataclass(frozen=True)
class ProviderAttempt:
    provider: Provider
    fetched: int
    bronze_created: int
    silver_created: int
    silver_accepted: int
    rejected: int
    error: str | None = None


@dataclass(frozen=True)
class IngestionEvidence:
    market: Market
    symbol: str
    timeframe: str
    selected_provider: Provider
    attempts: tuple[ProviderAttempt, ...]
    candles: tuple[CanonicalCandle, ...]
    gold_created: int

    @property
    def reconciled(self) -> bool:
        selected = next(item for item in self.attempts if item.provider is self.selected_provider)
        return selected.fetched == selected.silver_accepted + selected.rejected


class ProviderComposition:
    """Fetch by capability route and stop at the first provider yielding Silver."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        clients: Mapping[Provider, HistoricalClient],
        pipeline: EtlPipeline,
    ) -> None:
        self._registry = registry
        self._clients = dict(clients)
        self._pipeline = pipeline

    async def ingest_history(
        self, market: Market, request: HistoricalRequest, *, received_at: datetime
    ) -> IngestionEvidence:
        from nanodelta.providers.base import ProviderCapability

        received_at = utc(received_at, "received_at")
        attempts: list[ProviderAttempt] = []
        for provider in self._registry.route(market, ProviderCapability.HISTORICAL_CANDLES):
            client = self._clients.get(provider)
            if client is None:
                attempts.append(
                    ProviderAttempt(provider, 0, 0, 0, 0, 0, "CLIENT_NOT_CONFIGURED")
                )
                continue
            if client.market is not market or client.provider is not provider:
                raise ValueError(f"client identity mismatch for {provider.value}")
            try:
                payloads = await client.fetch_candles(request)
            except Exception as exc:
                attempts.append(ProviderAttempt(provider, 0, 0, 0, 0, 0, type(exc).__name__))
                continue
            bronze = silver = accepted = rejected = 0
            candles: list[CanonicalCandle] = []
            for payload in payloads:
                result = self._pipeline.ingest(
                    market=market,
                    provider=provider,
                    event_type=EventType.CANDLE,
                    provider_symbol=request.symbol,
                    payload=payload,
                    received_at=received_at,
                )
                bronze += int(result.bronze_created)
                silver += int(result.silver_created)
                is_accepted = result.canonical is not None and result.rejection_reason is None
                accepted += int(is_accepted)
                rejected += int(not is_accepted)
                if is_accepted and result.canonical is not None:
                    candles.append(result.canonical)
            attempts.append(
                ProviderAttempt(provider, len(payloads), bronze, silver, accepted, rejected)
            )
            if candles:
                features = self._pipeline.build_gold(candles)
                return IngestionEvidence(
                    market,
                    request.symbol,
                    request.timeframe,
                    provider,
                    tuple(attempts),
                    tuple(candles),
                    len(features),
                )
        summary = ", ".join(f"{a.provider.value}:{a.error or a.fetched}" for a in attempts)
        raise ProviderFetchError(f"no provider produced Silver candles ({summary})")


class ProviderMarketCycle:
    """Callable composition seam accepted directly by ``MarketWorker``."""

    def __init__(
        self,
        composition: ProviderComposition,
        requests: Mapping[Market, Sequence[HistoricalRequest]],
        *,
        clock: object,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._composition = composition
        self._requests = {market: tuple(items) for market, items in requests.items()}
        self._clock = clock
        self.latest: dict[tuple[Market, str, str], IngestionEvidence] = {}

    async def __call__(self, market: Market) -> None:
        for request in self._requests.get(market, ()):
            evidence = await self._composition.ingest_history(
                market, request, received_at=self._clock()
            )
            self.latest[(market, request.symbol, request.timeframe)] = evidence
