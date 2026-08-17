"""Real, candle-backed pairwise correlation for AllocationPolicy.max_pairwise_correlation.

The correlation gate in decision_pipeline.py._correlated has always existed, but
paper_decision.py never passed it anything -- every cycle ran with an empty mapping,
so the check was structurally present but a permanent no-op. This computes real
pairwise correlation of daily returns from settled 1d candles already in Postgres
(populated by the history backfill service); symbol pairs without enough shared
history are omitted rather than assigned a fabricated correlation, which leaves the
existing "missing pair defaults to 0.0" behavior as the honest fallback.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import cast

from nanodelta.contracts import Market
from nanodelta.persistence.migrations import Connection

_LOOKBACK_BARS = 90
_MINIMUM_OVERLAP = 20


def _fetch_daily_closes(
    connection: Connection, market: Market, symbols: Sequence[str]
) -> dict[str, list[float]]:
    if not symbols:
        return {}
    cursor = connection.cursor()
    cursor.execute(
        f"SELECT symbol, close FROM {market.value}_silver.candles "
        "WHERE symbol = ANY(%s) AND timeframe='1d' AND is_settled=true "
        "ORDER BY symbol, open_time DESC",
        (list(symbols),),
    )
    closes: dict[str, list[float]] = {}
    for row in cursor.fetchall():
        symbol = cast(str, row[0])
        bucket = closes.setdefault(symbol, [])
        if len(bucket) < _LOOKBACK_BARS:
            bucket.append(float(cast(float, row[1])))
    for values in closes.values():
        values.reverse()
    return closes


def _pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    mean_x, mean_y = sum(x) / n, sum(y) / n
    covariance = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y, strict=True))
    variance_x = sum((xi - mean_x) ** 2 for xi in x)
    variance_y = sum((yi - mean_y) ** 2 for yi in y)
    denominator = math.sqrt(variance_x * variance_y)
    if denominator == 0:
        return None
    return covariance / denominator


def compute_return_correlations(
    closes_by_symbol: Mapping[str, Sequence[float]],
    *,
    minimum_overlap: int = _MINIMUM_OVERLAP,
) -> dict[tuple[str, str], float]:
    """Pure function: pairwise Pearson correlation of simple daily returns."""
    returns_by_symbol = {
        symbol: [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
        for symbol, closes in closes_by_symbol.items()
    }
    result: dict[tuple[str, str], float] = {}
    symbols = sorted(returns_by_symbol)
    for index, first in enumerate(symbols):
        for second in symbols[index + 1 :]:
            a, b = returns_by_symbol[first], returns_by_symbol[second]
            overlap = min(len(a), len(b))
            if overlap < minimum_overlap:
                continue
            correlation = _pearson(a[-overlap:], b[-overlap:])
            if correlation is not None:
                result[(first, second)] = correlation
    return result


def fetch_return_correlations(
    connection: Connection, market: Market, symbols: Sequence[str]
) -> dict[tuple[str, str], float]:
    closes = _fetch_daily_closes(connection, market, symbols)
    return compute_return_correlations(closes)
