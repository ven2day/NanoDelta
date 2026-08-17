from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nanodelta.strategies import TechnicalCandle, materialize_technical_features

NSE_OPEN = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)  # 09:15 IST


def rising_candles(count: int, *, start: datetime = NSE_OPEN) -> list[TechnicalCandle]:
    """Monotonically increasing 1-minute bars so rolling-window extremes are
    exactly predictable: bar i has close=100+i, high=101+i, low=99+i."""
    result = []
    for index in range(count):
        close = 100.0 + index
        result.append(
            TechnicalCandle(
                start + timedelta(minutes=index),
                close - 0.5,
                close + 1.0,
                close - 1.0,
                close,
                1000.0,
            )
        )
    return result


def test_rolling_range_uses_only_prior_bars() -> None:
    snapshots = materialize_technical_features(rising_candles(45))
    by_time = {snapshot.event_time: snapshot.values for snapshot in snapshots}
    at_index_30 = by_time[NSE_OPEN + timedelta(minutes=30)]
    # window = bars[10:30] -> high of bar 29 (129+1=130), low of bar 10 (99+10=109)
    assert at_index_30["range_high_20"] == 130.0
    assert at_index_30["range_low_20"] == 109.0
    # window = bars[20:30] -> high of bar 29 (130), low of bar 20 (99+20=119)
    assert at_index_30["range_high_10"] == 130.0
    assert at_index_30["range_low_10"] == 119.0
    # window = bars[25:30] -> high of bar 29 (130), low of bar 25 (99+25=124)
    assert at_index_30["range_high_5"] == 130.0
    assert at_index_30["range_low_5"] == 124.0


def test_range_fields_present_once_indicators_are_warm() -> None:
    # Base indicator warmup (EMA-21/ADX-14 compound smoothing) already needs
    # more prior bars than any rolling-range window here (max 20), so every
    # emitted snapshot already has the range fields -- there is no reachable
    # "warm but no range data yet" state to test separately.
    snapshots = materialize_technical_features(rising_candles(45))
    assert snapshots
    assert all("range_high_5" in snapshot.values for snapshot in snapshots)
    assert all("range_high_20" in snapshot.values for snapshot in snapshots)


def test_opening_range_is_the_first_15_minutes_of_the_nse_session() -> None:
    snapshots = materialize_technical_features(rising_candles(45))
    by_time = {snapshot.event_time: snapshot.values for snapshot in snapshots}
    at_minute_30 = by_time[NSE_OPEN + timedelta(minutes=30)]
    # opening window = bars[0:15] (minutes 0..14) -> high of bar 14 (114+1=115),
    # low of bar 0 (99+0=99)
    assert at_minute_30["opening_range_high"] == 115.0
    assert at_minute_30["opening_range_low"] == 99.0


def test_opening_range_absent_for_candles_outside_nse_hours() -> None:
    off_session_start = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)  # well after NSE close
    snapshots = materialize_technical_features(rising_candles(45, start=off_session_start))
    assert all("opening_range_high" not in snapshot.values for snapshot in snapshots)
