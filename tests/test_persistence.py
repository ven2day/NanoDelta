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
    assert [migration.version for migration in migrations] == [
        "0001_timescaledb_foundation",
        "0002_strategy_and_agent_governance",
    ]
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
    assert applied == (
        "0001_timescaledb_foundation",
        "0002_strategy_and_agent_governance",
    )
    assert any("pg_advisory_lock" in query for query, _ in cursor.calls)
    assert any("schema_migrations(version, checksum)" in query for query, _ in cursor.calls)
    assert connection.commits == 1
    assert connection.closed is True


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
