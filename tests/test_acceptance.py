from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nanodelta.acceptance import (
    AcceptanceStatus,
    latency_check,
    percentile,
    recovery_check,
    report,
    scheduler_drift_check,
    worker_soak_check,
)


def test_percentile_uses_nearest_rank() -> None:
    assert percentile([5, 1, 4, 3, 2], 95) == 5
    with pytest.raises(ValueError):
        percentile([], 95)


@pytest.mark.asyncio
async def test_latency_check_reports_failures_without_hiding_them() -> None:
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected")

    result = await latency_check("api", operation, iterations=3, concurrency=1, p95_limit_ms=100)
    assert result.status is AcceptanceStatus.FAIL
    assert result.measured["success_ratio"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_provider_and_database_recovery_is_bounded() -> None:
    attempts = 0

    async def transient_failure() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("injected outage")

    result = await recovery_check("provider", transient_failure, maximum_attempts=3)
    assert result.status is AcceptanceStatus.PASS
    assert result.measured["attempts"] == 3


@pytest.mark.asyncio
async def test_scheduler_and_three_market_soak_quick_profile() -> None:
    drift = await scheduler_drift_check(interval_seconds=0.002, cycles=5, maximum_drift_ms=100)
    soak = await worker_soak_check(
        duration_seconds=0.03, interval_seconds=0.002, maximum_drain_ms=500
    )
    assert drift.status is AcceptanceStatus.PASS
    assert soak.status is AcceptanceStatus.PASS
    assert soak.measured["nse_cycles"] > 0
    assert soak.measured["forex_cycles"] > 0
    assert soak.measured["crypto_cycles"] > 0


def test_report_is_machine_readable_and_fails_on_failed_check(tmp_path: Path) -> None:
    failed = asyncio.run(recovery_check("database", _always_fails, maximum_attempts=2))
    acceptance = report("quick", "test", [failed])
    output = tmp_path / "report.json"
    acceptance.write(output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["passed"] is False


async def _always_fails() -> None:
    raise ConnectionError("database unavailable")


def test_full_profile_requires_explicit_opt_in() -> None:
    source = Path("scripts/run_acceptance.py").read_text(encoding="utf-8")
    assert "NANODELTA_RUN_FULL_ACCEPTANCE" in source
    restore = Path("scripts/verify-backup-restore.sh").read_text(encoding="utf-8")
    assert "docker run" in restore
    assert "docker rm -f" in restore
