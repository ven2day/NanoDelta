"""Capability-specific provider ownership and fallback ordering."""

from __future__ import annotations

from dataclasses import dataclass, field

from nanodelta.contracts import Market, Provider
from nanodelta.providers.base import ProviderCapability


@dataclass
class ProviderRegistry:
    _routes: dict[tuple[Market, ProviderCapability], tuple[Provider, ...]] = field(
        default_factory=dict
    )

    def register(
        self,
        market: Market,
        capability: ProviderCapability,
        providers: tuple[Provider, ...],
    ) -> None:
        if not providers or len(set(providers)) != len(providers):
            raise ValueError("provider route must be non-empty and contain no duplicates")
        self._routes[(market, capability)] = providers

    def route(self, market: Market, capability: ProviderCapability) -> tuple[Provider, ...]:
        try:
            return self._routes[(market, capability)]
        except KeyError as exc:
            raise LookupError(f"no provider route for {market.value}/{capability.value}") from exc


def default_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        Market.NSE,
        ProviderCapability.HISTORICAL_CANDLES,
        (Provider.DHAN, Provider.TRUEDATA),
    )
    registry.register(
        Market.NSE,
        ProviderCapability.REALTIME_QUOTES,
        (Provider.TRUEDATA, Provider.DHAN),
    )
    registry.register(
        Market.FOREX,
        ProviderCapability.HISTORICAL_CANDLES,
        (Provider.OANDA,),
    )
    registry.register(
        Market.FOREX,
        ProviderCapability.REALTIME_QUOTES,
        (Provider.OANDA,),
    )
    registry.register(
        Market.CRYPTO,
        ProviderCapability.HISTORICAL_CANDLES,
        (Provider.OKX, Provider.POLONIEX),
    )
    registry.register(
        Market.CRYPTO,
        ProviderCapability.REALTIME_QUOTES,
        (Provider.OKX, Provider.POLONIEX),
    )
    registry.register(
        Market.CRYPTO,
        ProviderCapability.ORDER_BOOK,
        (Provider.OKX, Provider.POLONIEX),
    )
    return registry
