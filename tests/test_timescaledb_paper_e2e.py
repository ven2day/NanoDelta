"""Opt-in disposable TimescaleDB paper lifecycle proof; never a real-provider test."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

from nanodelta.api.read_models import PostgresAuthoritativeReadStore
from nanodelta.contracts import EventType, Market, Provider
from nanodelta.decisions_postgres import PostgresDecisionLedger
from nanodelta.orchestration import AllocationPolicy
from nanodelta.paper import ExecutionPolicy, PostgresPaperExecutionEngine
from nanodelta.paper.lifecycle import PaperPositionLifecycle
from nanodelta.paper.lifecycle_postgres import PostgresLifecycleStore
from nanodelta.persistence import MigrationRunner, load_migrations
from nanodelta.persistence.postgres import PostgresStore
from nanodelta.pipeline import EtlPipeline
from nanodelta.risk import RiskEngine, RiskLimits
from nanodelta.runtime.paper_decision import PaperDecisionService
from nanodelta.strategies import (
    PostgresStrategyRegistry,
    StrategyApproval,
    StrategyRuntimeCatalog,
    ValidationMetrics,
    ValidationPolicy,
    builtin_strategies,
    validate_strategy,
)

DATABASE_URL = os.environ.get("NANODELTA_E2E_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="NANODELTA_E2E_DATABASE_URL not set")


def _payload(
    at: datetime, open_: float, high: float, low: float, close: float
) -> dict[str, object]:
    return {
        "ts": str(int(at.timestamp() * 1000)),
        "o": open_,
        "h": high,
        "l": low,
        "c": close,
        "vol": 100,
        "bar": "1m",
        "confirm": "1",
    }


def test_disposable_timescaledb_full_paper_lifecycle() -> None:
    database = urlparse(DATABASE_URL).path.lstrip("/")
    if not database.startswith("nanodelta_e2e_"):
        pytest.fail("NANODELTA_E2E_DATABASE_URL database must start with nanodelta_e2e_")
    connect = lambda: psycopg.connect(DATABASE_URL)  # noqa: E731
    MigrationRunner(connect).apply(load_migrations(Path(__file__).parents[1] / "migrations"))
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM paper.orders")
            assert cursor.fetchone() == (0,), "use a fresh disposable E2E database"

    pipeline = EtlPipeline(PostgresStore(connect))
    t0 = datetime(2026, 8, 16, 10, tzinfo=UTC)
    first = pipeline.ingest(
        market=Market.CRYPTO,
        provider=Provider.OKX,
        event_type=EventType.CANDLE,
        provider_symbol="BTC-USDT",
        payload=_payload(t0, 100, 101, 99, 100),
        received_at=t0,
    ).canonical
    second = pipeline.ingest(
        market=Market.CRYPTO,
        provider=Provider.OKX,
        event_type=EventType.CANDLE,
        provider_symbol="BTC-USDT",
        payload=_payload(t0 + timedelta(minutes=1), 100, 103, 99, 102),
        received_at=t0 + timedelta(minutes=1),
    ).canonical
    assert first is not None and second is not None
    entry_features = tuple(pipeline.build_gold((first, second)))

    plugin = next(
        item for item in builtin_strategies() if item.definition.identity.market is Market.CRYPTO
    )
    registry = PostgresStrategyRegistry(connect)
    registry.register(plugin.definition)
    validation = validate_strategy(
        plugin.definition.identity,
        ValidationMetrics(100, 5, 4, 0.02, 0.001, 0.1, 0.001, 1),
        ValidationPolicy(),
        evaluated_at=t0 - timedelta(days=2),
    )
    registry.record_validation(validation)
    registry.record_approval(
        StrategyApproval.create(
            identity=plugin.definition.identity,
            validation_run_id=validation.validation_run_id,
            approved_at=t0 - timedelta(days=1),
            expires_at=t0 + timedelta(days=30),
            approved_by="disposable-e2e-fixture",
            reason="explicit test-only approval",
        )
    )
    catalog = StrategyRuntimeCatalog()
    catalog.register(plugin)
    ledger = PostgresDecisionLedger(connect)
    execution = PostgresPaperExecutionEngine(ExecutionPolicy(0, 0), connect)
    risk = RiskEngine(RiskLimits(1_000_000, 1_000_000, 2_000_000, 2_000_000, 100_000, 10))
    lifecycle = PaperPositionLifecycle(
        store=PostgresLifecycleStore(connect), execution=execution, risk=risk, ledger=ledger
    )
    clock = [t0 + timedelta(minutes=1)]
    service = PaperDecisionService(
        connect=connect,
        registry=registry,
        catalog=catalog,
        ledger=ledger,
        risk=risk,
        execution=execution,
        allocation=AllocationPolicy(100_000, 0.01, 50_000, 50_000, 10, 10),
        account_id="paper-e2e",
        equity=100_000,
        clock=lambda: clock[0],
        lifecycle=lifecycle,
    )
    service.process(entry_features)

    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT target_price FROM paper.exit_plans WHERE state='ACTIVE'")
            target = float(cursor.fetchone()[0])  # type: ignore[index]
    clock[0] = t0 + timedelta(minutes=2)
    third = pipeline.ingest(
        market=Market.CRYPTO,
        provider=Provider.OKX,
        event_type=EventType.CANDLE,
        provider_symbol="BTC-USDT",
        payload=_payload(clock[0], 102, target + 2, 101, target + 1),
        received_at=clock[0],
    ).canonical
    assert third is not None
    service.process(tuple(pipeline.build_gold((second, third))))

    authoritative = PostgresAuthoritativeReadStore(DATABASE_URL)
    assert (
        authoritative.page("signals", market=Market.CRYPTO, limit=20, offset=0, filters={}).total
        == 1
    )
    assert (
        authoritative.page("orders", market=Market.CRYPTO, limit=20, offset=0, filters={}).total
        == 2
    )
    positions = authoritative.page(
        "positions", market=Market.CRYPTO, limit=20, offset=0, filters={"state": "CLOSED"}
    )
    trades = authoritative.page("trades", market=Market.CRYPTO, limit=20, offset=0, filters={})
    assert positions.total == 1
    assert trades.total == 1
    assert trades.items[0]["net_pnl"] > 0
