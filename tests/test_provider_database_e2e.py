from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from nanodelta.contracts import Market, Provider
from nanodelta.integration import (
    ProviderComposition,
    ProviderMarketCycle,
    RecordedHistoricalClient,
    run_recorded_paper_session,
)
from nanodelta.persistence.migrations import MigrationRunner, load_migrations
from nanodelta.persistence.postgres import PostgresStore
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import HistoricalRequest, ProviderClientError
from nanodelta.providers.registry import ProviderRegistry, default_provider_registry
from nanodelta.runtime.supervisor import MarketWorker, MemoryRuntimeStateStore
from nanodelta.storage import FileLake

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/providers/dhan_reliance_15m.json"
NOW = datetime(2026, 8, 15, 4, 30, tzinfo=UTC)
REQUEST = HistoricalRequest("RELIANCE", "15m", NOW - timedelta(hours=1), NOW, 10)


@pytest.mark.asyncio
async def test_recorded_provider_to_paper_order_has_complete_lineage(tmp_path: Path) -> None:
    client = RecordedHistoricalClient(Market.NSE, Provider.DHAN, FIXTURE)
    composition = ProviderComposition(
        registry=default_provider_registry(),
        clients={Provider.DHAN: client},
        pipeline=EtlPipeline(FileLake(tmp_path)),
    )
    ingestion = await composition.ingest_history(Market.NSE, REQUEST, received_at=NOW)
    evidence = run_recorded_paper_session(ingestion)

    assert ingestion.reconciled
    assert evidence.bronze_created == 2
    assert evidence.silver_created == 2
    assert evidence.gold_created == 1
    assert evidence.candidate_count == 1
    assert evidence.risk_approved_count == 1
    assert evidence.paper_order_count == 1
    assert evidence.live_execution_interfaces == 0
    assert "PAPER_ORDER_CREATED" in evidence.decision_reasons


class _FailingClient:
    market = Market.NSE
    provider = Provider.TRUEDATA

    async def fetch_candles(self, request: HistoricalRequest) -> list[dict[str, Any]]:
        del request
        raise ProviderClientError("recorded outage")


@pytest.mark.asyncio
async def test_capability_route_falls_back_and_records_failed_attempt(tmp_path: Path) -> None:
    registry = ProviderRegistry()
    from nanodelta.providers.base import ProviderCapability

    registry.register(
        Market.NSE,
        ProviderCapability.HISTORICAL_CANDLES,
        (Provider.TRUEDATA, Provider.DHAN),
    )
    composition = ProviderComposition(
        registry=registry,
        clients={
            Provider.TRUEDATA: _FailingClient(),
            Provider.DHAN: RecordedHistoricalClient(Market.NSE, Provider.DHAN, FIXTURE),
        },
        pipeline=EtlPipeline(FileLake(tmp_path)),
    )
    evidence = await composition.ingest_history(Market.NSE, REQUEST, received_at=NOW)

    assert evidence.selected_provider is Provider.DHAN
    assert evidence.attempts[0].provider is Provider.TRUEDATA
    assert evidence.attempts[0].error == "ProviderClientError"


@pytest.mark.asyncio
async def test_provider_cycle_is_directly_supervisable(tmp_path: Path) -> None:
    composition = ProviderComposition(
        registry=default_provider_registry(),
        clients={
            Provider.DHAN: RecordedHistoricalClient(Market.NSE, Provider.DHAN, FIXTURE)
        },
        pipeline=EtlPipeline(FileLake(tmp_path)),
    )
    cycle = ProviderMarketCycle(composition, {Market.NSE: (REQUEST,)}, clock=lambda: NOW)
    worker = MarketWorker(
        Market.NSE,
        "test",
        cycle,
        MemoryRuntimeStateStore(),
        interval_seconds=0.01,
        heartbeat_seconds=60,
    )
    await worker.start()
    for _ in range(100):
        if cycle.latest:
            break
        import asyncio

        await asyncio.sleep(0.001)
    await worker.drain()
    assert (Market.NSE, "RELIANCE", "15m") in cycle.latest


@pytest.mark.asyncio
async def test_timescaledb_reconciliation_is_explicitly_opt_in() -> None:
    database_url = os.environ.get("NANODELTA_INTEGRATION_DATABASE_URL")
    if not database_url:
        pytest.skip("set NANODELTA_INTEGRATION_DATABASE_URL for isolated TimescaleDB test")
    connect = lambda: psycopg.connect(database_url)  # noqa: E731
    MigrationRunner(connect).apply(load_migrations(ROOT / "migrations"))
    composition = ProviderComposition(
        registry=default_provider_registry(),
        clients={
            Provider.DHAN: RecordedHistoricalClient(Market.NSE, Provider.DHAN, FIXTURE)
        },
        pipeline=EtlPipeline(PostgresStore(connect)),
    )
    ingestion = await composition.ingest_history(Market.NSE, REQUEST, received_at=NOW)
    assert ingestion.reconciled
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT "
            "(SELECT count(*) FROM nse_bronze.raw_events WHERE provider='dhan'), "
            "(SELECT count(*) FROM nse_silver.candles WHERE symbol='RELIANCE'), "
            "(SELECT count(*) FROM nse_gold.feature_snapshots WHERE symbol='RELIANCE')"
        )
        counts = cursor.fetchone()
    assert counts is not None
    assert all(int(value) >= minimum for value, minimum in zip(counts, (2, 2, 1), strict=True))


def test_fixture_is_labelled_and_does_not_claim_live_capture() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert document["fixture_version"] == 1
    assert document["captured"] is False
    assert "synthetic" in document["source"]
