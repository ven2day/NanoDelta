from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.strategies import (
    EmaRsiContinuationStrategy,
    StrategyContext,
    StrategyRegistry,
    SuperTrendAdxStrategy,
    TechnicalCandle,
    TechnicalStrategy,
    VwapPullbackStrategy,
    materialize_technical_features,
    technical_strategies,
)

START = datetime(2026, 1, 5, 9, 15, tzinfo=UTC)


def candles(count: int, *, forming_last: bool = False) -> list[TechnicalCandle]:
    result = []
    price = 100.0
    for index in range(count):
        move = 0.35 if index % 5 else -0.12
        open_price = price
        price += move
        result.append(
            TechnicalCandle(
                START + timedelta(minutes=5 * index),
                open_price,
                max(open_price, price) + 0.2,
                min(open_price, price) - 0.2,
                price,
                1000 + index * 7,
                settled=not (forming_last and index == count - 1),
            )
        )
    return result


def context(plugin: TechnicalStrategy, values: dict[str, float]) -> StrategyContext:
    definition = plugin.definition
    identity = definition.identity
    return StrategyContext(
        identity.market,
        "TEST_SYMBOL",
        None,
        identity.timeframe,
        identity.trade_horizon,
        identity.feature_set_version,
        START,
        ("gold-fixture-1",),
        values,
    )


def plugin(strategy_id: str, market: Market) -> TechnicalStrategy:
    return next(
        item
        for item in technical_strategies()
        if item.definition.identity.strategy_id == strategy_id
        and item.definition.identity.market is market
    )


def test_features_require_warmup_ignore_forming_bar_and_do_not_peek() -> None:
    assert materialize_technical_features(candles(20)) == ()
    prefix = materialize_technical_features(candles(45))
    extended = materialize_technical_features(candles(60))
    with_forming = materialize_technical_features(candles(46, forming_last=True))

    assert prefix
    assert extended[: len(prefix)] == prefix
    assert with_forming == prefix
    assert all(snapshot.feature_set_version == 2 for snapshot in prefix)
    assert all(snapshot.event_time <= prefix[-1].event_time for snapshot in prefix)


def test_vwap_pullback_emits_buy_sell_and_abstains_without_touch() -> None:
    strategy = plugin("vwap_pullback", Market.NSE)
    assert isinstance(strategy, VwapPullbackStrategy)
    common = {
        "ema_21": 100.0,
        "atr_14": 1.0,
        "volume_ratio_20": 1.1,
    }
    buy = strategy.generate(
        context(
            strategy,
            {**common, "close": 101.0, "high": 101.2, "low": 100.05, "vwap": 100.1},
        )
    )
    sell = strategy.generate(
        context(
            strategy,
            {
                **common,
                "ema_21": 102.0,
                "close": 99.0,
                "high": 99.95,
                "low": 98.8,
                "vwap": 99.9,
            },
        )
    )
    no_touch = strategy.generate(
        context(
            strategy,
            {**common, "close": 103.0, "high": 103.2, "low": 102.5, "vwap": 100.0},
        )
    )

    assert buy is not None and buy.action is AdvisoryAction.BUY
    assert sell is not None and sell.action is AdvisoryAction.SELL
    assert no_touch is None


def test_ema_rsi_continuation_requires_trend_rsi_and_candle_alignment() -> None:
    strategy = plugin("ema_rsi_continuation", Market.FOREX)
    assert isinstance(strategy, EmaRsiContinuationStrategy)
    buy = strategy.generate(
        context(
            strategy,
            {
                "close": 1.1,
                "ema_9": 1.101,
                "ema_21": 1.099,
                "rsi_14": 60,
                "atr_14": 0.003,
                "body_pct": 0.002,
            },
        )
    )
    sell = strategy.generate(
        context(
            strategy,
            {
                "close": 1.1,
                "ema_9": 1.099,
                "ema_21": 1.101,
                "rsi_14": 40,
                "atr_14": 0.003,
                "body_pct": -0.002,
            },
        )
    )
    misaligned = strategy.generate(
        context(
            strategy,
            {
                "close": 1.1,
                "ema_9": 1.101,
                "ema_21": 1.099,
                "rsi_14": 60,
                "atr_14": 0.003,
                "body_pct": -0.002,
            },
        )
    )
    assert buy is not None and buy.action is AdvisoryAction.BUY
    assert sell is not None and sell.action is AdvisoryAction.SELL
    assert misaligned is None


def test_supertrend_adx_requires_direction_price_and_trend_strength() -> None:
    strategy = plugin("supertrend_adx", Market.CRYPTO)
    assert isinstance(strategy, SuperTrendAdxStrategy)
    buy = strategy.generate(
        context(
            strategy,
            {
                "close": 101.0,
                "supertrend": 98.0,
                "supertrend_direction": 1.0,
                "adx_14": 32.0,
                "atr_14": 2.0,
            },
        )
    )
    sell = strategy.generate(
        context(
            strategy,
            {
                "close": 95.0,
                "supertrend": 98.0,
                "supertrend_direction": -1.0,
                "adx_14": 32.0,
                "atr_14": 2.0,
            },
        )
    )
    weak = strategy.generate(
        context(
            strategy,
            {
                "close": 101.0,
                "supertrend": 98.0,
                "supertrend_direction": 1.0,
                "adx_14": 20.0,
                "atr_14": 2.0,
            },
        )
    )
    assert buy is not None and buy.action is AdvisoryAction.BUY
    assert sell is not None and sell.action is AdvisoryAction.SELL
    assert weak is None


def test_definitions_are_unique_versioned_and_never_auto_approved() -> None:
    strategies = technical_strategies()
    identities = {item.definition.identity for item in strategies}
    registry = StrategyRegistry()
    for item in strategies:
        registry.register(item.definition)

    assert len(strategies) == len(identities) == 8
    assert {identity.feature_set_version for identity in identities} == {2}
    assert registry.eligible(
        market=Market.NSE,
        timeframe="15m",
        trade_horizon="intraday",
        feature_set_version=2,
        at=START,
    ) == ()


def test_compatibility_enforces_declared_feature_contract_and_identity() -> None:
    strategy = plugin("supertrend_adx", Market.NSE)
    complete = {
        "close": 101.0,
        "supertrend": 98.0,
        "supertrend_direction": 1.0,
        "adx_14": 32.0,
        "atr_14": 2.0,
    }
    valid = context(strategy, complete)
    missing = context(
        strategy,
        {name: value for name, value in complete.items() if name != "adx_14"},
    )
    wrong_version = StrategyContext(
        valid.market,
        valid.symbol,
        valid.sector,
        valid.timeframe,
        valid.trade_horizon,
        1,
        valid.event_time,
        valid.gold_snapshot_ids,
        complete,
    )

    assert strategy.compatibility(valid) == (True, "COMPATIBLE")
    assert strategy.compatibility(missing) == (False, "REQUIRED_FEATURES_MISSING")
    assert strategy.compatibility(wrong_version) == (False, "FEATURE_VERSION_MISMATCH")
