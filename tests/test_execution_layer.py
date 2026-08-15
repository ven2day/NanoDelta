from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.execution import (
    ExecutionMode,
    ExecutionRecord,
    ExecutionStatus,
    SchemaBoundExecutionRepository,
    execution_record_from_result,
)
from src.core.pipeline import ProcessingRole


def _filled_record(*, market: str = "NSE", provider: str = "DHAN") -> ExecutionRecord:
    symbol = "RELIANCE" if market == "NSE" else "EUR_USD"
    return execution_record_from_result(
        market=market,
        provider=provider,
        intent_id="intent-1",
        requested_price=100.0,
        order_type="MARKET",
        decision_id="decision-1",
        result={
            "status": "FILLED",
            "symbol": symbol,
            "side": "BUY",
            "quantity": 10,
            "fill_price": 100.1,
            "mode": "local_paper",
            "order_id": "order-1",
            "position_id": "position-1",
            "trade_id": "trade-1",
        },
    )


def test_execution_record_links_intent_fill_position_and_decision() -> None:
    first = _filled_record()
    second = _filled_record()

    assert first.execution_id == second.execution_id
    assert first.producer is ProcessingRole.EXECUTION_ENGINE
    assert first.mode is ExecutionMode.PAPER
    assert first.status is ExecutionStatus.FILLED
    assert first.decision_id == "decision-1"
    assert first.fill is not None and first.fill.order_id == "order-1"
    assert first.position is not None and first.position.position_id == "position-1"


def test_filled_execution_requires_real_fill_details() -> None:
    with pytest.raises(ValueError, match="require order ID"):
        ExecutionRecord.create(
            market="NSE",
            provider="DHAN",
            mode="PAPER",
            status="FILLED",
            intent_id="intent-1",
            symbol="RELIANCE",
            side="BUY",
            requested_quantity=1,
            requested_price=100,
            order_type="MARKET",
            payload={},
        )


def test_execution_journal_refuses_live_result_mislabeling() -> None:
    with pytest.raises(ValueError, match="paper/shadow"):
        execution_record_from_result(
            market="NSE",
            provider="DHAN",
            intent_id="intent-live",
            requested_price=100,
            order_type="MARKET",
            result={
                "status": "FILLED",
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 1,
                "fill_price": 100,
                "mode": "live",
                "order_id": "broker-order",
            },
        )


class _Result:
    rowcount = 1


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> _Result:
        self.calls.append((str(statement), params))
        return _Result()


class _PostgresEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self) -> None:
        self.connection = _Connection()

    @contextmanager
    def begin(self):  # type: ignore[no-untyped-def]
        yield self.connection


def test_execution_repository_upserts_and_rejects_cross_market() -> None:
    engine = _PostgresEngine()
    repository = SchemaBoundExecutionRepository(
        engine,  # type: ignore[arg-type]
        market="NSE",
        provider="DHAN",
    )

    assert repository.persist_many([_filled_record()]) == 1
    sql = "\n".join(call[0] for call in engine.connection.calls)
    assert 'INSERT INTO "nse"."execution_records"' in sql
    assert "ON CONFLICT (execution_id) DO UPDATE" in sql

    with pytest.raises(ValueError, match="cannot write FOREX"):
        repository.persist_many([_filled_record(market="FOREX", provider="OANDA")])
