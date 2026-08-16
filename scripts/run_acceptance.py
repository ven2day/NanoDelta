#!/usr/bin/env python3
"""Run CI-safe or opt-in local NanoDelta operational acceptance probes."""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from nanodelta.acceptance import (
    Thresholds,
    latency_check,
    recovery_check,
    report,
    scheduler_drift_check,
    worker_soak_check,
)
from nanodelta.api.app import ApiServices, create_app
from nanodelta.contracts import Market
from nanodelta.decisions import (
    Decision,
    DecisionStage,
    DecisionStatus,
    InMemoryDecisionLedger,
)
from nanodelta.operations import OperationalStore, RuntimeController


async def run(profile: str, output: Path) -> bool:
    thresholds = Thresholds()
    iterations = 100 if profile == "quick" else 10_000
    concurrency = 5 if profile == "quick" else 50

    operations = OperationalStore()
    app = create_app(ApiServices(operations, RuntimeController(operations), {}, {}, {}))
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://acceptance.local")

    async def api_operation() -> None:
        response = await client.get("/api/overview")
        response.raise_for_status()

    ledger = InMemoryDecisionLedger()
    decision_index = 0

    async def decision_operation() -> None:
        nonlocal decision_index
        decision_index += 1
        ledger.append(
            Decision.create(
                cycle_id=f"acceptance-{decision_index}",
                market=Market.NSE,
                symbol="RELIANCE",
                timeframe="15m",
                stage=DecisionStage.SCORING,
                status=DecisionStatus.PASSED,
                reason_code="ACCEPTANCE_PROBE",
                occurred_at=datetime.now(UTC),
            )
        )
        await asyncio.sleep(0)

    provider_attempts = 0

    async def recovering_provider() -> None:
        nonlocal provider_attempts
        provider_attempts += 1
        if provider_attempts < 2:
            raise ConnectionError("injected provider outage")

    database_attempts = 0

    async def reconnecting_database() -> None:
        nonlocal database_attempts
        database_attempts += 1
        if database_attempts < 2:
            raise ConnectionError("injected database outage")

    results = [
        await latency_check(
            "api_latency",
            api_operation,
            iterations=iterations,
            concurrency=concurrency,
            p95_limit_ms=thresholds.api_p95_ms,
        ),
        await latency_check(
            "decision_latency",
            decision_operation,
            iterations=iterations,
            concurrency=concurrency,
            p95_limit_ms=thresholds.decision_p95_ms,
        ),
        await recovery_check(
            "provider_recovery",
            recovering_provider,
            maximum_attempts=thresholds.maximum_recovery_attempts,
        ),
        await recovery_check(
            "database_reconnect",
            reconnecting_database,
            maximum_attempts=thresholds.maximum_recovery_attempts,
        ),
        await scheduler_drift_check(
            interval_seconds=0.005 if profile == "quick" else 0.05,
            cycles=10 if profile == "quick" else 1_000,
            maximum_drift_ms=thresholds.maximum_scheduler_drift_ms,
        ),
        await worker_soak_check(
            duration_seconds=0.05 if profile == "quick" else 3_600,
            interval_seconds=0.005 if profile == "quick" else 1,
            maximum_drain_ms=thresholds.maximum_drain_ms,
        ),
    ]
    acceptance = report(profile, os.getenv("NANODELTA_ACCEPTANCE_ENV", "local"), results)
    await client.aclose()
    acceptance.write(output)
    return acceptance.passed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    parser.add_argument("--output", type=Path, default=Path("artifacts/acceptance-report.json"))
    arguments = parser.parse_args()
    if arguments.profile == "full" and os.getenv("NANODELTA_RUN_FULL_ACCEPTANCE") != "1":
        parser.error("full profile requires NANODELTA_RUN_FULL_ACCEPTANCE=1")
    raise SystemExit(0 if asyncio.run(run(arguments.profile, arguments.output)) else 1)


if __name__ == "__main__":
    main()
