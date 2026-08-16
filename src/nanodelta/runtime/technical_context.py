"""Connects materialize_technical_features to real settled Silver candles.

technical_strategies() (VWAP pullback, EMA/RSI continuation, SuperTrend/ADX) require
vwap/ema_9/ema_21/rsi_14/atr_14/adx_14/supertrend/supertrend_direction/volume_ratio_20
in a StrategyContext's features -- values nothing computed from real candle history
before this module. materialize_technical_features (nanodelta.strategies.technical_features)
already does the deterministic indicator math correctly; it was simply never called with
real data. This module is that connection: read a window of settled candles for one
symbol/timeframe from Postgres, run the existing indicator math, and return the latest
warm snapshot, or None when there isn't yet enough settled history for every indicator
to warm up (EMA-21 alone needs at least 21 settled bars) -- that's an expected, not
exceptional, state for a symbol that just started trading or just started being tracked.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast

from nanodelta.contracts import Market
from nanodelta.persistence.migrations import Connection
from nanodelta.strategies import TechnicalCandle, materialize_technical_features

CANDLE_WINDOW = 100


def latest_technical_features(
    connection: Connection,
    market: Market,
    symbol: str,
    timeframe: str,
) -> Mapping[str, float] | None:
    cursor = connection.cursor()
    cursor.execute(
        f"SELECT open_time,open,high,low,close,volume FROM {market.value}_silver.candles "
        "WHERE symbol=%s AND timeframe=%s AND is_settled=true "
        "ORDER BY open_time DESC LIMIT %s",
        (symbol, timeframe, CANDLE_WINDOW),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    candles = [
        TechnicalCandle(
            cast(datetime, row[0]),
            float(cast(float, row[1])),
            float(cast(float, row[2])),
            float(cast(float, row[3])),
            float(cast(float, row[4])),
            float(cast(float, row[5])),
        )
        for row in rows
    ]
    snapshots = materialize_technical_features(candles)
    if not snapshots:
        return None
    return snapshots[-1].values
