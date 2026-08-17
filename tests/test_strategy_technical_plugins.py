from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.strategies import (
    EmaRsiContinuationStrategy,
    RegimeEvidence,
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
    required_labels = getattr(plugin, "required_regime_labels", frozenset())
    regime_label = next(iter(required_labels), "UNKNOWN")
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
        regime=RegimeEvidence(symbol_regime_label=regime_label),
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


def test_mean_reversion_fades_stretched_extremes_back_toward_the_mean() -> None:
    strategy = plugin("mean_reversion_rsi", Market.NSE)
    buy = strategy.generate(
        context(
            strategy,
            {"close": 96.0, "ema_21": 100.0, "rsi_14": 25.0, "atr_14": 2.0},
        )
    )
    sell = strategy.generate(
        context(
            strategy,
            {"close": 104.0, "ema_21": 100.0, "rsi_14": 75.0, "atr_14": 2.0},
        )
    )
    not_stretched = strategy.generate(
        context(
            strategy,
            {"close": 99.5, "ema_21": 100.0, "rsi_14": 25.0, "atr_14": 2.0},
        )
    )
    assert buy is not None and buy.action is AdvisoryAction.BUY
    assert buy.target_price == 100.0
    assert sell is not None and sell.action is AdvisoryAction.SELL
    assert sell.target_price == 100.0
    assert not_stretched is None


def test_range_reversal_fades_a_rejected_touch_of_the_range_edge() -> None:
    strategy = plugin("range_reversal", Market.NSE)
    buy = strategy.generate(
        context(
            strategy,
            {
                "close": 101.0,
                "high": 101.5,
                "low": 99.9,
                "range_high_20": 110.0,
                "range_low_20": 100.0,
                "atr_14": 1.0,
            },
        )
    )
    no_touch = strategy.generate(
        context(
            strategy,
            {
                "close": 105.0,
                "high": 105.5,
                "low": 104.5,
                "range_high_20": 110.0,
                "range_low_20": 100.0,
                "atr_14": 1.0,
            },
        )
    )
    assert buy is not None and buy.action is AdvisoryAction.BUY
    assert no_touch is None


def test_support_reversal_requires_volume_confirmation() -> None:
    strategy = plugin("support_reversal", Market.NSE)
    common = {
        "close": 101.0,
        "high": 101.5,
        "low": 99.95,
        "range_high_10": 110.0,
        "range_low_10": 100.0,
        "atr_14": 1.0,
    }
    confirmed = strategy.generate(context(strategy, {**common, "volume_ratio_20": 1.5}))
    unconfirmed = strategy.generate(context(strategy, {**common, "volume_ratio_20": 0.9}))
    assert confirmed is not None and confirmed.action is AdvisoryAction.BUY
    assert unconfirmed is None


def test_breakout_requires_range_expansion_with_volume() -> None:
    strategy = plugin("breakout", Market.NSE)
    common = {"range_high_20": 110.0, "range_low_20": 100.0, "atr_14": 1.0}
    buy = strategy.generate(
        context(strategy, {**common, "close": 111.0, "volume_ratio_20": 1.5})
    )
    low_volume = strategy.generate(
        context(strategy, {**common, "close": 111.0, "volume_ratio_20": 0.8})
    )
    inside_range = strategy.generate(
        context(strategy, {**common, "close": 105.0, "volume_ratio_20": 1.5})
    )
    assert buy is not None and buy.action is AdvisoryAction.BUY
    assert low_volume is None
    assert inside_range is None


def test_opening_range_breakout_trades_the_first_15_minute_range() -> None:
    strategy = plugin("opening_range_breakout", Market.NSE)
    common = {"opening_range_high": 105.0, "opening_range_low": 100.0, "atr_14": 1.0}
    buy = strategy.generate(context(strategy, {**common, "close": 106.0}))
    inside = strategy.generate(context(strategy, {**common, "close": 102.0}))
    assert buy is not None and buy.action is AdvisoryAction.BUY
    assert inside is None


def test_volume_breakout_requires_a_strong_volume_surge() -> None:
    strategy = plugin("volume_breakout", Market.NSE)
    common = {"range_high_5": 105.0, "range_low_5": 100.0, "atr_14": 1.0}
    buy = strategy.generate(
        context(strategy, {**common, "close": 106.0, "volume_ratio_20": 2.5})
    )
    weak_volume = strategy.generate(
        context(strategy, {**common, "close": 106.0, "volume_ratio_20": 1.1})
    )
    assert buy is not None and buy.action is AdvisoryAction.BUY
    assert weak_volume is None


def test_definitions_are_unique_versioned_and_never_auto_approved() -> None:
    strategies = technical_strategies()
    identities = {item.definition.identity for item in strategies}
    registry = StrategyRegistry()
    for item in strategies:
        registry.register(item.definition)

    assert len(strategies) == len(identities) == 14
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
