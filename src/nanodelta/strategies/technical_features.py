"""Pure, deterministic technical features built from settled candles only.

The caller supplies candles in any order.  This module sorts them by event time,
rejects forming bars, and emits a snapshot only after every indicator is warm.
No function reads a candle after the snapshot timestamp.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast


@dataclass(frozen=True)
class TechnicalCandle:
    event_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    settled: bool = True

    def __post_init__(self) -> None:
        if self.event_time.tzinfo is None:
            raise ValueError("technical candle timestamp must be timezone-aware")
        if self.open <= 0 or self.close <= 0 or self.low <= 0 or self.high < self.low:
            raise ValueError("technical candle prices are invalid")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("technical candle OHLC is inconsistent")
        if self.volume < 0:
            raise ValueError("technical candle volume cannot be negative")


@dataclass(frozen=True)
class TechnicalFeatureSnapshot:
    event_time: datetime
    values: Mapping[str, float]
    feature_set_version: int = 2


def _ema(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        current = current + alpha * (values[index] - current)
        result[index] = current
    return result


def _wilder(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    for index in range(period, len(values)):
        current = (current * (period - 1) + values[index]) / period
        result[index] = current
    return result


def _rsi(closes: Sequence[float], period: int) -> list[float | None]:
    gains = [0.0]
    losses = [0.0]
    for previous, current in zip(closes, closes[1:], strict=False):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = _wilder(gains[1:], period)
    average_loss = _wilder(losses[1:], period)
    result: list[float | None] = [None] * len(closes)
    for index, (gain, loss) in enumerate(zip(average_gain, average_loss, strict=True), start=1):
        if gain is not None and loss is not None:
            result[index] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return result


def _atr_adx(
    candles: Sequence[TechnicalCandle], atr_period: int, adx_period: int
) -> tuple[list[float | None], list[float | None]]:
    ranges: list[float] = []
    positive: list[float] = []
    negative: list[float] = []
    for index, candle in enumerate(candles):
        previous = candles[index - 1] if index else candle
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous.close),
                abs(candle.low - previous.close),
            )
        )
        up = candle.high - previous.high
        down = previous.low - candle.low
        positive.append(up if index and up > down and up > 0 else 0.0)
        negative.append(down if index and down > up and down > 0 else 0.0)
    atr = _wilder(ranges, atr_period)
    directional_range = _wilder(ranges, adx_period)
    plus = _wilder(positive, adx_period)
    minus = _wilder(negative, adx_period)
    dx: list[float | None] = [None] * len(candles)
    for index, average_range in enumerate(directional_range):
        if average_range in {None, 0}:
            continue
        plus_di = 100 * (plus[index] or 0) / average_range
        minus_di = 100 * (minus[index] or 0) / average_range
        total = plus_di + minus_di
        dx[index] = 0.0 if total == 0 else 100 * abs(plus_di - minus_di) / total
    adx: list[float | None] = [None] * len(candles)
    available = [(index, value) for index, value in enumerate(dx) if value is not None]
    if len(available) >= adx_period:
        seed_index = available[adx_period - 1][0]
        current = sum(value for _, value in available[:adx_period]) / adx_period
        adx[seed_index] = current
        for index, value in available[adx_period:]:
            current = (current * (adx_period - 1) + value) / adx_period
            adx[index] = current
    return atr, adx


def _supertrend(
    candles: Sequence[TechnicalCandle], atr: Sequence[float | None], multiplier: float
) -> tuple[list[float | None], list[float | None]]:
    trend: list[float | None] = [None] * len(candles)
    direction: list[float | None] = [None] * len(candles)
    upper: float | None = None
    lower: float | None = None
    previous_trend: float | None = None
    for index, (candle, average_range) in enumerate(zip(candles, atr, strict=True)):
        if average_range is None:
            continue
        midpoint = (candle.high + candle.low) / 2
        basic_upper = midpoint + multiplier * average_range
        basic_lower = midpoint - multiplier * average_range
        previous_close = candles[index - 1].close if index else candle.close
        previous_upper = upper
        upper = (
            basic_upper
            if upper is None or basic_upper < upper or previous_close > upper
            else upper
        )
        lower = (
            basic_lower
            if lower is None or basic_lower > lower or previous_close < lower
            else lower
        )
        if previous_trend is None:
            previous_trend = lower if candle.close >= midpoint else upper
        elif previous_trend == previous_upper:
            previous_trend = lower if candle.close > upper else upper
        else:
            previous_trend = upper if candle.close < lower else lower
        trend[index] = previous_trend
        direction[index] = 1.0 if candle.close > previous_trend else -1.0
    return trend, direction


def _session_values(
    candles: Sequence[TechnicalCandle], volume_period: int
) -> tuple[list[float | None], list[float | None]]:
    vwap: list[float | None] = []
    volume_ratio: list[float | None] = []
    session = None
    weighted = cumulative_volume = 0.0
    for index, candle in enumerate(candles):
        current_session = candle.event_time.date()
        if current_session != session:
            session, weighted, cumulative_volume = current_session, 0.0, 0.0
        typical = (candle.high + candle.low + candle.close) / 3
        weighted += typical * candle.volume
        cumulative_volume += candle.volume
        vwap.append(weighted / cumulative_volume if cumulative_volume > 0 else None)
        start = max(0, index - volume_period + 1)
        window = candles[start : index + 1]
        average = sum(item.volume for item in window) / len(window)
        volume_ratio.append(candle.volume / average if average > 0 else None)
    return vwap, volume_ratio


def _rolling_range(
    candles: Sequence[TechnicalCandle], period: int
) -> tuple[list[float | None], list[float | None]]:
    """Donchian-style rolling high/low over the PRIOR `period` bars (excluding
    the current one) -- looking at the range up to but not including today
    avoids a big move including itself in its own "recent range" and never
    registering as a breakout."""
    highs: list[float | None] = [None] * len(candles)
    lows: list[float | None] = [None] * len(candles)
    for index in range(len(candles)):
        if index < period:
            continue
        window = candles[index - period : index]
        highs[index] = max(candle.high for candle in window)
        lows[index] = min(candle.low for candle in window)
    return highs, lows


_NSE_SESSION_OPEN_UTC_SECONDS = 3 * 3600 + 45 * 60  # 09:15 IST


def _opening_range(
    candles: Sequence[TechnicalCandle], minutes: int
) -> tuple[list[float | None], list[float | None]]:
    """Today's opening-range high/low (first `minutes` of the NSE session,
    09:15 IST) for each snapshot, using bars strictly before the current one
    within the same session -- ORB trades the breakout of that range, so a
    bar still inside the opening range itself has nothing to break out of
    yet."""
    highs: list[float | None] = [None] * len(candles)
    lows: list[float | None] = [None] * len(candles)
    for index, candle in enumerate(candles):
        session_date = candle.event_time.date()
        session_open = candle.event_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(seconds=_NSE_SESSION_OPEN_UTC_SECONDS)
        window_end = session_open + timedelta(minutes=minutes)
        window = [
            prior
            for prior in candles[:index]
            if prior.event_time.date() == session_date
            and session_open <= prior.event_time < window_end
        ]
        if window and candle.event_time >= window_end:
            highs[index] = max(item.high for item in window)
            lows[index] = min(item.low for item in window)
    return highs, lows


def materialize_technical_features(
    candles: Sequence[TechnicalCandle],
    *,
    rsi_period: int = 14,
    atr_period: int = 14,
    adx_period: int = 14,
    supertrend_multiplier: float = 3.0,
) -> tuple[TechnicalFeatureSnapshot, ...]:
    """Return warm snapshots; every snapshot uses its candle and earlier candles only."""
    if min(rsi_period, atr_period, adx_period) < 2 or supertrend_multiplier <= 0:
        raise ValueError("technical feature periods and multiplier are invalid")
    ordered = sorted(
        (candle for candle in candles if candle.settled),
        key=lambda row: row.event_time,
    )
    closes = [candle.close for candle in ordered]
    ema_9, ema_21 = _ema(closes, 9), _ema(closes, 21)
    rsi = _rsi(closes, rsi_period)
    atr, adx = _atr_adx(ordered, atr_period, adx_period)
    supertrend, direction = _supertrend(ordered, atr, supertrend_multiplier)
    vwap, volume_ratio = _session_values(ordered, 20)
    range_high_20, range_low_20 = _rolling_range(ordered, 20)
    range_high_10, range_low_10 = _rolling_range(ordered, 10)
    range_high_5, range_low_5 = _rolling_range(ordered, 5)
    opening_range_high, opening_range_low = _opening_range(ordered, 15)
    snapshots = []
    for index, candle in enumerate(ordered):
        indicators = (
            ema_9[index], ema_21[index], rsi[index], atr[index], adx[index],
            supertrend[index], direction[index], vwap[index], volume_ratio[index],
        )
        if any(value is None for value in indicators):
            continue
        values: dict[str, float] = {
            "close": candle.close,
            "high": candle.high,
            "low": candle.low,
            "body_pct": (candle.close - candle.open) / candle.open,
            "ema_9": cast(float, ema_9[index]),
            "ema_21": cast(float, ema_21[index]),
            "rsi_14": cast(float, rsi[index]),
            "atr_14": cast(float, atr[index]),
            "adx_14": cast(float, adx[index]),
            "supertrend": cast(float, supertrend[index]),
            "supertrend_direction": cast(float, direction[index]),
            "vwap": cast(float, vwap[index]),
            "volume_ratio_20": cast(float, volume_ratio[index]),
        }
        optional = {
            "range_high_20": range_high_20[index],
            "range_low_20": range_low_20[index],
            "range_high_10": range_high_10[index],
            "range_low_10": range_low_10[index],
            "range_high_5": range_high_5[index],
            "range_low_5": range_low_5[index],
            "opening_range_high": opening_range_high[index],
            "opening_range_low": opening_range_low[index],
        }
        values.update({key: value for key, value in optional.items() if value is not None})
        snapshots.append(
            TechnicalFeatureSnapshot(
                candle.event_time,
                values,
            )
        )
    return tuple(snapshots)
