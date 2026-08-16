from __future__ import annotations

from pathlib import Path

import pytest

from nanodelta.contracts import Market
from nanodelta.persistence.migrations import MigrationRunner, load_migrations
from nanodelta.persistence.postgres import PostgresStore


class FakeCursor:
    def __init__(self, *, existing: tuple[object, ...] | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.existing = existing
        self.returning = False

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((query, params))
        self.returning = "RETURNING" in query

    def fetchone(self) -> tuple[object, ...] | None:
        if self.returning:
            return ("created",)
        result, self.existing = self.existing, None
        return result

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def migration_directory() -> Path:
    return Path(__file__).parents[1] / "migrations"


def test_foundation_migration_creates_every_market_layer() -> None:
    migrations = load_migrations(migration_directory())
    versions = [migration.version for migration in migrations]
    assert versions[:12] == [
        "0001_timescaledb_foundation",
        "0002_strategy_and_agent_governance",
        "0003_paper_execution_and_outcomes",
        "0004_history_and_operations",
        "0005_qwen_finops",
        "0006_staged_decision_pipeline",
        "0007_executable_runtime",
        "0008_authoritative_ui_read_models",
        "0009_paper_realization_events",
        "0010_paper_order_position_snapshots",
        "0011_runtime_command_mailbox",
        "0012_identity_and_access",
    ]
    assert versions[-1] == "0017_nse_continuous_paper_session"
    assert "0015_authoritative_signal_universe" in versions
    assert "0016_nse_strategy_validation_evidence" in versions
    assert len(versions) == len(set(versions))
    sql = migrations[0].sql
    assert "CREATE EXTENSION IF NOT EXISTS timescaledb" in sql
    assert "ARRAY['nse', 'forex', 'crypto']" in sql
    assert "raw_events" in sql
    assert "candles" in sql
    assert "feature_snapshots" in sql
    assert "provider_watermarks" in sql
    assert sql.count("create_hypertable") == 2


def test_migration_runner_records_checksum_and_uses_lock() -> None:
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    runner = MigrationRunner(lambda: connection)
    applied = runner.apply(load_migrations(migration_directory()))
    assert applied == tuple(
        migration.version for migration in load_migrations(migration_directory())
    )
    assert any("pg_advisory_lock" in query for query, _ in cursor.calls)
    assert any("schema_migrations(version, checksum)" in query for query, _ in cursor.calls)
    assert connection.commits == 1
    assert connection.closed is True


def test_paper_migration_enforces_paper_only_and_lineage_tables() -> None:
    migration = load_migrations(migration_directory())[2]

    assert "execution_mode = 'PAPER'" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS paper.decisions" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS paper.orders" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS paper.fills" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS paper.positions" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS paper.outcomes" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS research.learning_assessments" in migration.sql


def test_operations_migration_creates_durable_history_and_audit_state() -> None:
    migration = load_migrations(migration_directory())[3]

    assert "CREATE TABLE IF NOT EXISTS control.history_runs" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS control.runtime_workers" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS control.operational_audit" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS control.history_repair_queue" in migration.sql


def test_finops_migration_creates_usage_alert_and_kill_switch_state() -> None:
    migration = load_migrations(migration_directory())[4]

    assert "CREATE TABLE IF NOT EXISTS control.llm_usage" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS control.llm_finops_alerts" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS control.llm_kill_switch" in migration.sql


def test_decision_pipeline_migration_creates_ledger_and_funnel() -> None:
    migration = load_migrations(migration_directory())[5]

    assert "CREATE TABLE IF NOT EXISTS control.decision_events" in migration.sql
    assert "CREATE OR REPLACE VIEW control.decision_funnel" in migration.sql


def test_authoritative_signal_migration_preserves_candidates_and_runtime_universe() -> None:
    migration = next(
        migration
        for migration in load_migrations(migration_directory())
        if migration.version == "0015_authoritative_signal_universe"
    )

    assert "CREATE TABLE IF NOT EXISTS control.signal_candidates" in migration.sql
    assert "CHECK (action IN ('BUY', 'SELL'))" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS control.market_universe" in migration.sql


def test_migration_runner_rejects_changed_applied_migration() -> None:
    cursor = FakeCursor(existing=("wrong-checksum",))
    connection = FakeConnection(cursor)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        MigrationRunner(lambda: connection).apply(load_migrations(migration_directory()))
    assert connection.rollbacks == 1


def test_postgres_store_routes_to_market_layer_schema() -> None:
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    store = PostgresStore(lambda: connection)
    created = store.write(
        market=Market.FOREX,
        layer="bronze",
        event_time=__import__("datetime").datetime.now(__import__("datetime").UTC),
        record_id="raw-1",
        record={
            "provider": "oanda",
            "event_type": "candle",
            "provider_symbol": "EUR_USD",
            "received_at": "2026-08-15T12:00:00+00:00",
            "schema_version": 1,
            "payload": {"complete": True},
        },
    )
    assert created is True
    assert "INSERT INTO forex_bronze.raw_events" in cursor.calls[0][0]
    assert "nse_" not in cursor.calls[0][0]


def test_postgres_store_rejects_unknown_layer() -> None:
    store = PostgresStore(lambda: FakeConnection(FakeCursor()))
    with pytest.raises(ValueError, match="unsupported layer"):
        store.write(
            market=Market.NSE,
            layer="decision",
            event_time=__import__("datetime").datetime.now(__import__("datetime").UTC),
            record_id="x",
            record={},
        )
