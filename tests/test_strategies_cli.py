"""nanodelta-strategy's plugin lookup previously only searched builtin_strategies(),
so it could validate/approve momentum but raised LookupError for any technical
strategy id even after the strategy existed in code -- this is the exact CLI gap
flagged alongside "wire the technical strategies into the runtime".
"""

from __future__ import annotations

import pytest

from nanodelta.contracts import Market
from nanodelta.strategies import (
    EmaRsiContinuationStrategy,
    MomentumContinuationStrategy,
    SuperTrendAdxStrategy,
    VwapPullbackStrategy,
)
from nanodelta.strategies.cli import _plugin


def test_plugin_lookup_resolves_momentum() -> None:
    plugin = _plugin(Market.NSE, "momentum_continuation")
    assert isinstance(plugin, MomentumContinuationStrategy)


@pytest.mark.parametrize(
    ("market", "strategy_id", "expected_type"),
    [
        (Market.NSE, "vwap_pullback", VwapPullbackStrategy),
        (Market.NSE, "ema_rsi_continuation", EmaRsiContinuationStrategy),
        (Market.NSE, "supertrend_adx", SuperTrendAdxStrategy),
        (Market.FOREX, "ema_rsi_continuation", EmaRsiContinuationStrategy),
        (Market.FOREX, "supertrend_adx", SuperTrendAdxStrategy),
        (Market.CRYPTO, "vwap_pullback", VwapPullbackStrategy),
        (Market.CRYPTO, "ema_rsi_continuation", EmaRsiContinuationStrategy),
        (Market.CRYPTO, "supertrend_adx", SuperTrendAdxStrategy),
    ],
)
def test_plugin_lookup_resolves_every_technical_strategy(
    market: Market, strategy_id: str, expected_type: type
) -> None:
    plugin = _plugin(market, strategy_id)
    assert isinstance(plugin, expected_type)


def test_plugin_lookup_rejects_unknown_strategy() -> None:
    with pytest.raises(LookupError, match="exact built-in strategy plugin"):
        _plugin(Market.NSE, "does_not_exist")


def test_plugin_lookup_rejects_vwap_for_forex_since_it_is_not_defined_there() -> None:
    with pytest.raises(LookupError, match="exact built-in strategy plugin"):
        _plugin(Market.FOREX, "vwap_pullback")
