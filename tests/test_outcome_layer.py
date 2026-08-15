from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.outcomes import OutcomeRecord, SchemaBoundOutcomeRepository
from src.core.pipeline import ProcessingRole


def _outcome(*, market: str = "NSE", provider: str = "DHAN") -> OutcomeRecord:
    return OutcomeRecord.create(
        market=market,
        provider=provider,
        trade_id="trade-1",
        decision_id="decision-1",
        entry_execution_id="execution-entry-1",
        exit_execution_id="execution-exit-1",
        feature_snapshot_id="feature-1",
        symbol="RELIANCE" if market == "NSE" else "EUR_USD",
        timeframe="15m",
        side="BUY",
        strategy="momentum",
        opened_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        closed_at=datetime(2026, 8, 15, 13, 0, tzinfo=UTC),
        entry_price=100,
        exit_price=105,
        quantity=10,
        net_pnl=50,
        return_pct=5,
        mae=10,
        mfe=60,
        hold_minutes=60,
        exit_reason="TARGET",
        attribution={"regime": "trending", "strategy": "momentum"},
        payload={"status": "CLOSED"},
    )


def test_outcome_links_lineage_and_is_offline_learning_eligible() -> None:
    first = _outcome()
    second = _outcome()

    assert first.outcome_id == second.outcome_id
    assert first.producer is ProcessingRole.OUTCOME_ENGINE
    assert first.is_winner
    assert first.learning_eligible
    assert first.decision_id == "decision-1"
    assert first.entry_execution_id == "execution-entry-1"
    assert first.exit_execution_id == "execution-exit-1"


def test_outcome_without_exact_feature_is_not_learning_eligible() -> None:
    record = OutcomeRecord.create(
        market="FOREX",
        provider="OANDA",
        trade_id="legacy-trade",
        symbol="EUR_USD",
        timeframe="15m",
        side="SELL",
        strategy="ema_adx_trend",
        closed_at=datetime.now(UTC),
        entry_price=1.1,
        exit_price=1.09,
        quantity=1_000,
        net_pnl=10,
        return_pct=0.9,
        exit_reason="TARGET",
        attribution={},
        payload={},
    )

    assert not record.learning_eligible


def test_outcome_rejects_open_or_invalid_trade_values() -> None:
    with pytest.raises(ValueError, match="prices and quantity"):
        OutcomeRecord.create(
            market="NSE",
            provider="DHAN",
            trade_id="trade-bad",
            symbol="RELIANCE",
            timeframe="15m",
            side="BUY",
            strategy="momentum",
            closed_at=datetime.now(UTC),
            entry_price=100,
            exit_price=0,
            quantity=10,
            net_pnl=0,
            return_pct=0,
            exit_reason="UNKNOWN",
            attribution={},
            payload={},
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


def test_outcome_repository_upserts_and_rejects_cross_market() -> None:
    engine = _PostgresEngine()
    repository = SchemaBoundOutcomeRepository(
        engine,  # type: ignore[arg-type]
        market="NSE",
        provider="DHAN",
    )

    assert repository.persist_many([_outcome()]) == 1
    sql = "\n".join(call[0] for call in engine.connection.calls)
    assert 'INSERT INTO "nse"."outcome_records"' in sql
    assert "ON CONFLICT (outcome_id) DO UPDATE" in sql

    with pytest.raises(ValueError, match="cannot write FOREX"):
        repository.persist_many([_outcome(market="FOREX", provider="OANDA")])
