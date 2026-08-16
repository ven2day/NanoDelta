"""latest_technical_features connects materialize_technical_features (pure indicator
math, already tested elsewhere) to real settled Silver candles. These tests prove the
connection itself: enough history produces every indicator VwapPullbackStrategy/
EmaRsiContinuationStrategy/SuperTrendAdxStrategy require, and too little history
returns None rather than a partial/broken feature set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nanodelta.contracts import Market
from nanodelta.runtime.technical_context import latest_technical_features
from nanodelta.strategies import (
    EmaRsiContinuationStrategy,
    SuperTrendAdxStrategy,
    VwapPullbackStrategy,
)

START = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)


class FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((query, params))

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor


def _trending_candle_rows(count: int) -> list[tuple[object, ...]]:
    """Settled candles in descending open_time order, matching the real query's
    ORDER BY -- a mild, steady uptrend so ADX/SuperTrend both warm up meaningfully."""
    rows = []
    price = 100.0
    for i in range(count):
        open_time = START + timedelta(minutes=i)
        price += 0.3
        row = (open_time, price - 0.3, price + 0.2, price - 0.4, price, 1_000.0 + i)
        rows.append(row)
    rows.reverse()
    return rows


def test_returns_none_without_enough_settled_history_to_warm_every_indicator() -> None:
    cursor = FakeCursor(_trending_candle_rows(10))
    result = latest_technical_features(FakeConnection(cursor), Market.NSE, "RELIANCE", "5m")
    assert result is None


def test_returns_every_feature_the_technical_strategies_require_once_warm() -> None:
    cursor = FakeCursor(_trending_candle_rows(60))
    result = latest_technical_features(FakeConnection(cursor), Market.NSE, "RELIANCE", "5m")

    assert result is not None
    required = (
        VwapPullbackStrategy.required_features
        | EmaRsiContinuationStrategy.required_features
        | SuperTrendAdxStrategy.required_features
    )
    assert required.issubset(result)
    assert all(isinstance(value, float) for value in result.values())


def test_query_filters_to_the_exact_market_symbol_timeframe_and_settled_only() -> None:
    cursor = FakeCursor(_trending_candle_rows(60))
    latest_technical_features(FakeConnection(cursor), Market.FOREX, "EUR_USD", "1h")

    query, params = cursor.calls[0]
    assert "forex_silver.candles" in query
    assert "is_settled=true" in query
    assert params[:2] == ("EUR_USD", "1h")


def test_returns_none_with_no_settled_candles_at_all() -> None:
    cursor = FakeCursor([])
    result = latest_technical_features(FakeConnection(cursor), Market.CRYPTO, "BTC_USDT", "15m")
    assert result is None
