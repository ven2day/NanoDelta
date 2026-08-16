from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from nanodelta.api import ApiServices, create_app
from nanodelta.contracts import Market
from nanodelta.operations import (
    Actor,
    AuditRecord,
    Command,
    PostgresOperationalStore,
    RuntimeController,
    WorkerState,
)


class FakeCursor:
    def __init__(self, *, fetchone_result: tuple[object, ...] | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchone_result = fetchone_result

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((query, params))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.fetchone_result

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class FailingCursor(FakeCursor):
    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        super().execute(query, params)
        raise RuntimeError("boom")


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


def sample_audit() -> AuditRecord:
    return AuditRecord(
        "audit-1",
        "idem-1",
        Market.NSE,
        Command.START,
        "operator-1",
        WorkerState.STOPPED,
        WorkerState.RUNNING,
        datetime.now(UTC),
        "",
    )


def test_worker_state_falls_back_to_in_memory_default_when_no_row() -> None:
    connection = FakeConnection(FakeCursor(fetchone_result=None))
    store = PostgresOperationalStore(lambda: connection)
    assert store.worker_state(Market.NSE) is WorkerState.STOPPED
    assert connection.closed is True


def test_worker_state_reads_persisted_row() -> None:
    connection = FakeConnection(FakeCursor(fetchone_result=("RUNNING",)))
    store = PostgresOperationalStore(lambda: connection)
    assert store.worker_state(Market.NSE) is WorkerState.RUNNING


def test_runtime_health_reads_durable_heartbeat_and_provider_state() -> None:
    heartbeat = datetime.now(UTC)
    heartbeat_connection = FakeConnection(FakeCursor(fetchone_result=(heartbeat,)))
    heartbeat_store = PostgresOperationalStore(lambda: heartbeat_connection)
    assert heartbeat_store.latest_heartbeat(Market.NSE) == heartbeat

    provider_row = (
        "truedata",
        "HEALTHY",
        heartbeat,
        heartbeat,
        0,
        1,
        None,
        None,
        True,
        None,
        heartbeat,
    )
    provider_connection = FakeConnection(FakeCursor(fetchone_result=provider_row))
    health = PostgresOperationalStore(lambda: provider_connection).market_provider_health(
        Market.NSE
    )
    assert health["active_provider"] == "truedata"
    assert health["state"] == "HEALTHY"
    assert health["failover_count"] == 1


def test_set_worker_state_persists_commits_and_updates_in_memory_view() -> None:
    connection = FakeConnection(FakeCursor())
    store = PostgresOperationalStore(lambda: connection)
    store.set_worker_state(Market.CRYPTO, WorkerState.DRAINING)
    assert "runtime_workers" in connection._cursor.calls[0][0]
    assert connection.commits == 1
    assert connection.closed is True
    assert store.workers[Market.CRYPTO] is WorkerState.DRAINING


def test_set_worker_state_rolls_back_and_reraises_on_failure() -> None:
    connection = FakeConnection(FailingCursor())
    store = PostgresOperationalStore(lambda: connection)
    with pytest.raises(RuntimeError, match="boom"):
        store.set_worker_state(Market.NSE, WorkerState.RUNNING)
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert connection.closed is True
    assert store.workers[Market.NSE] is WorkerState.STOPPED


def test_audit_record_returns_none_when_absent() -> None:
    connection = FakeConnection(FakeCursor(fetchone_result=None))
    store = PostgresOperationalStore(lambda: connection)
    assert store.audit_record("missing") is None


def test_audit_record_reconstructs_persisted_row() -> None:
    record = sample_audit()
    row = (
        record.audit_id,
        record.market.value,
        record.command.value,
        record.actor_id,
        record.previous_state.value,
        record.resulting_state.value,
        record.requested_at,
        record.detail,
    )
    connection = FakeConnection(FakeCursor(fetchone_result=row))
    store = PostgresOperationalStore(lambda: connection)
    found = store.audit_record(record.idempotency_key)
    assert found == record


def test_save_audit_persists_commits_and_updates_in_memory_view() -> None:
    connection = FakeConnection(FakeCursor())
    record = sample_audit()
    store = PostgresOperationalStore(lambda: connection)
    store.save_audit(record)
    assert "operational_audit" in connection._cursor.calls[0][0]
    assert connection.commits == 1
    assert store.audit[record.idempotency_key] == record


def test_commit_transition_writes_audit_and_worker_state_in_one_commit() -> None:
    connection = FakeConnection(FakeCursor())
    record = sample_audit()
    store = PostgresOperationalStore(lambda: connection)
    store.commit_transition(record)
    queries = [query for query, _ in connection._cursor.calls]
    assert any("operational_audit" in query for query in queries)
    assert any("runtime_workers" in query for query in queries)
    assert connection.commits == 1
    assert connection.closed is True
    assert store.workers[record.market] is record.resulting_state
    assert store.audit[record.idempotency_key] == record


def test_commit_transition_rolls_back_and_reraises_on_failure() -> None:
    connection = FakeConnection(FailingCursor())
    record = sample_audit()
    store = PostgresOperationalStore(lambda: connection)
    with pytest.raises(RuntimeError, match="boom"):
        store.commit_transition(record)
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert store.audit == {}


def test_read_endpoints_reflect_persisted_state_not_stale_in_memory_default() -> None:
    """Regression test: /api/overview and /api/{market}/health must read through
    worker_state() so they reflect Postgres after a restart or on another replica,
    instead of the freshly-initialized in-memory default of STOPPED."""
    connection = FakeConnection(FakeCursor(fetchone_result=("RUNNING",)))
    class ApiStore(PostgresOperationalStore):
        def latest_heartbeat(self, market: Market) -> datetime | None:
            del market
            return None

        def market_provider_health(self, market: Market) -> dict[str, object]:
            del market
            return {}

    store = ApiStore(lambda: connection)
    assert store.workers[Market.NSE] is WorkerState.STOPPED  # stale in-memory default

    services = ApiServices(
        store,
        RuntimeController(store),
        {},
        {},
        {"viewer-key": Actor("viewer", "viewer")},
    )
    api = TestClient(create_app(services))
    headers = {"X-API-Key": "viewer-key"}

    assert api.get("/api/nse/health", headers=headers).json()["worker_state"] == "RUNNING"
    overview = api.get("/api/overview", headers=headers).json()
    assert overview["markets"]["nse"]["worker_state"] == "RUNNING"
