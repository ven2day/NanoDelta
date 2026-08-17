from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nanodelta.strategies import TechnicalCandle, TradeabilityLimits, evaluate_tradeability

LIMITS = TradeabilityLimits(
    minimum_price=20.0,
    minimum_average_volume=10_000.0,
    minimum_average_traded_value=1_000_000.0,
    minimum_atr_pct=0.001,
    maximum_atr_pct=0.08,
    maximum_gap_pct=0.05,
    average_window=5,
)


def candle(
    minute: int, close: float, volume: float = 50_000, open_: float | None = None
) -> TechnicalCandle:
    at = datetime(2026, 8, 17, 9, minute, tzinfo=UTC)
    return TechnicalCandle(
        at, open_ if open_ is not None else close, close + 1, close - 1, close, volume
    )


def liquid_window(price: float = 1000.0, volume: float = 50_000.0) -> list[TechnicalCandle]:
    return [candle(minute, price, volume) for minute in range(6)]


def test_limits_reject_invalid_ranges() -> None:
    with pytest.raises(ValueError, match="positive"):
        TradeabilityLimits(0, 1, 1, 0.001, 0.08, 0.05)
    with pytest.raises(ValueError, match="minimum_atr_pct must be below"):
        TradeabilityLimits(20, 10_000, 1_000_000, 0.08, 0.08, 0.05)
    with pytest.raises(ValueError, match="average_window"):
        TradeabilityLimits(20, 10_000, 1_000_000, 0.001, 0.08, 0.05, average_window=1)


def test_insufficient_history_is_not_tradeable() -> None:
    tradeable, reason = evaluate_tradeability([candle(0, 1000.0)], atr_14=5.0, limits=LIMITS)
    assert not tradeable
    assert reason == "INSUFFICIENT_HISTORY"


def test_liquid_symbol_within_range_is_tradeable() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(candles, atr_14=10.0, limits=LIMITS)
    assert tradeable
    assert reason == "TRADEABLE"


def test_below_minimum_price_is_rejected() -> None:
    candles = liquid_window(price=5.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(candles, atr_14=0.1, limits=LIMITS)
    assert not tradeable
    assert reason == "BELOW_MINIMUM_PRICE"


def test_below_minimum_volume_is_rejected() -> None:
    candles = liquid_window(price=1000.0, volume=100.0)
    tradeable, reason = evaluate_tradeability(candles, atr_14=10.0, limits=LIMITS)
    assert not tradeable
    assert reason == "BELOW_MINIMUM_VOLUME"


def test_below_minimum_traded_value_is_rejected() -> None:
    # High price, tiny volume: passes price and volume floors individually in some
    # configs, but the combined traded-value floor still catches it here.
    limits = TradeabilityLimits(20, 1, 1_000_000, 0.001, 0.08, 0.05, average_window=5)
    candles = liquid_window(price=100.0, volume=50.0)
    tradeable, reason = evaluate_tradeability(candles, atr_14=1.0, limits=limits)
    assert not tradeable
    assert reason == "BELOW_MINIMUM_TRADED_VALUE"


def test_range_too_tight_is_rejected() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(candles, atr_14=0.05, limits=LIMITS)
    assert not tradeable
    assert reason == "RANGE_TOO_TIGHT"


def test_range_too_wide_is_rejected() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(candles, atr_14=200.0, limits=LIMITS)
    assert not tradeable
    assert reason == "RANGE_TOO_WIDE"


def test_gap_too_wide_is_rejected() -> None:
    candles = [candle(minute, 1000.0, 50_000) for minute in range(5)]
    candles.append(candle(5, 1100.0, 50_000, open_=1100.0))  # ~10% gap from prior close
    tradeable, reason = evaluate_tradeability(candles, atr_14=10.0, limits=LIMITS)
    assert not tradeable
    assert reason == "GAP_TOO_WIDE"


def test_sustained_reprice_without_a_gap_is_rejected() -> None:
    # Open matches prior close (no GAP_TOO_WIDE), but the close has moved far
    # from the recent average -- the signature of a mid-session corporate
    # action (split/bonus) rather than a fabricated calendar lookup.
    candles = [candle(minute, 1000.0, 50_000) for minute in range(5)]
    repriced = TechnicalCandle(
        datetime(2026, 8, 17, 9, 5, tzinfo=UTC), 1000.0, 1500.0, 1000.0, 1500.0, 50_000
    )
    tradeable, reason = evaluate_tradeability([*candles, repriced], atr_14=10.0, limits=LIMITS)
    assert not tradeable
    assert reason == "PRICE_DISCONTINUITY_SUSPECTED"


def test_normal_price_movement_within_reprice_band_is_tradeable() -> None:
    candles = [candle(minute, 1000.0 + minute, 50_000) for minute in range(6)]
    tradeable, reason = evaluate_tradeability(candles, atr_14=10.0, limits=LIMITS)
    assert tradeable
    assert reason == "TRADEABLE"


def test_missing_bar_is_rejected_when_timeframe_given() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    gapped = candle(20, 1000.0, 50_000)  # jumps from minute 5 to minute 20 on a 1m timeframe
    tradeable, reason = evaluate_tradeability(
        [*candles, gapped], atr_14=10.0, limits=LIMITS, timeframe="1m"
    )
    assert not tradeable
    assert reason == "MISSING_BAR_DETECTED"


def test_no_gap_is_tradeable_when_timeframe_given() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(
        candles, atr_14=10.0, limits=LIMITS, timeframe="1m"
    )
    assert tradeable
    assert reason == "TRADEABLE"


def test_unknown_timeframe_skips_gap_check() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    gapped = candle(59, 1000.0, 50_000)
    tradeable, reason = evaluate_tradeability(
        [*candles, gapped], atr_14=10.0, limits=LIMITS, timeframe="3m"
    )
    assert tradeable
    assert reason == "TRADEABLE"


def test_unsettled_candles_are_ignored() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    unsettled = TechnicalCandle(
        datetime(2026, 8, 17, 9, 6, tzinfo=UTC),
        1000.0,
        1001.0,
        999.0,
        1000.0,
        50_000.0,
        settled=False,
    )
    tradeable, reason = evaluate_tradeability([*candles, unsettled], atr_14=10.0, limits=LIMITS)
    assert tradeable
    assert reason == "TRADEABLE"


def test_near_upper_circuit_is_rejected() -> None:
    # band = 1003-800 = 203; proximity threshold = 203*0.02 ~= 4.06; distance
    # from upper = 1003-1000 = 3, inside the threshold.
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(
        candles, atr_14=10.0, limits=LIMITS, circuit_limits=(800.0, 1003.0)
    )
    assert not tradeable
    assert reason == "NEAR_UPPER_CIRCUIT"


def test_near_lower_circuit_is_rejected() -> None:
    # band = 1200-997 = 203; proximity threshold ~= 4.06; distance from lower
    # = 1000-997 = 3, inside the threshold.
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(
        candles, atr_14=10.0, limits=LIMITS, circuit_limits=(997.0, 1200.0)
    )
    assert not tradeable
    assert reason == "NEAR_LOWER_CIRCUIT"


def test_mid_band_circuit_limits_are_tradeable() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(
        candles, atr_14=10.0, limits=LIMITS, circuit_limits=(800.0, 1200.0)
    )
    assert tradeable
    assert reason == "TRADEABLE"


def test_wide_spread_is_rejected() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(
        candles, atr_14=10.0, limits=LIMITS, best_bid=1000.0, best_ask=1020.0
    )
    assert not tradeable
    assert reason == "SPREAD_TOO_WIDE"


def test_tight_spread_is_tradeable() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(
        candles, atr_14=10.0, limits=LIMITS, best_bid=1000.0, best_ask=1002.0
    )
    assert tradeable
    assert reason == "TRADEABLE"


def test_missing_quote_data_skips_circuit_and_spread_checks() -> None:
    candles = liquid_window(price=1000.0, volume=50_000.0)
    tradeable, reason = evaluate_tradeability(candles, atr_14=10.0, limits=LIMITS)
    assert tradeable
    assert reason == "TRADEABLE"
