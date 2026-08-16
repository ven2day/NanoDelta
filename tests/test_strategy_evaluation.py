from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from nanodelta.contracts import Market
from nanodelta.strategies import (
    StrategyApproval,
    StrategyRegistry,
    ValidationPolicy,
    builtin_strategies,
)
from nanodelta.strategies.evaluation import PostgresStrategyEvaluator


class Cursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        assert "_gold.feature_snapshots" in query
        assert params == ("1m", 1)

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class Connection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._cursor = Cursor(rows)
        self.closed = False

    def cursor(self) -> Any:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def test_gold_evaluation_persists_passing_cost_aware_evidence() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        (
            f"gold-{index}",
            "RELIANCE",
            "1m",
            start + timedelta(minutes=index),
            1,
            {
                "close": 100 * 1.01**index,
                "return_1": 0.01,
                "range_pct": 0.012,
                "body_pct": 0.009,
            },
        )
        for index in range(41)
    ]
    connection = Connection(rows)
    registry = StrategyRegistry()
    plugin = next(
        item for item in builtin_strategies() if item.definition.identity.market is Market.NSE
    )
    registry.register(plugin.definition)

    result = PostgresStrategyEvaluator(lambda: connection, registry).evaluate(
        plugin,
        policy=ValidationPolicy(),
        estimated_round_trip_cost=0.001,
        tested_hypotheses=3,
        evaluated_at=start + timedelta(days=1),
    )

    assert result.passed is True
    assert result.metrics.trade_count == 40
    assert result.metrics.profitable_windows == 3
    assert connection.closed is True
    approved_at = start + timedelta(days=1, minutes=1)
    approval = StrategyApproval.create(
        identity=plugin.definition.identity,
        validation_run_id=result.validation_run_id,
        approved_at=approved_at,
        expires_at=approved_at + timedelta(days=30),
        approved_by="operator@example.com",
        reason="reviewed validation evidence",
    )
    registry.record_approval(approval)
    assert registry.require_approval(plugin.definition.identity, at=approved_at) == approval
