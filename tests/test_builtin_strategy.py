from __future__ import annotations

from datetime import UTC, datetime

from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.strategies import StrategyContext, builtin_strategies


def _context(change: float, body: float) -> StrategyContext:
    return StrategyContext(
        Market.NSE,
        "RELIANCE",
        None,
        "1m",
        "intraday",
        1,
        datetime(2026, 8, 16, 10, tzinfo=UTC),
        ("gold-1",),
        {"close": 100.0, "return_1": change, "range_pct": 0.01, "body_pct": body},
    )


def test_builtin_strategy_emits_buy_and_sell_with_valid_geometry() -> None:
    strategy = next(
        item for item in builtin_strategies() if item.definition.identity.market is Market.NSE
    )

    buy = strategy.generate(_context(0.01, 0.008))
    sell = strategy.generate(_context(-0.01, -0.008))

    assert buy is not None and buy.action is AdvisoryAction.BUY
    assert buy.stop_price < buy.reference_price < buy.target_price
    assert sell is not None and sell.action is AdvisoryAction.SELL
    assert sell.target_price < sell.reference_price < sell.stop_price


def test_builtin_strategy_abstains_without_confirmed_momentum() -> None:
    strategy = next(
        item for item in builtin_strategies() if item.definition.identity.market is Market.NSE
    )
    assert strategy.generate(_context(0.0001, 0.0001)) is None
    assert strategy.generate(_context(0.01, -0.008)) is None
