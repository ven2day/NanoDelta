"""Reproducible operational acceptance harnesses.

The quick profile is intentionally bounded and dependency-free.  It proves the
test machinery and local runtime contracts; it is not evidence of VPS capacity
or live-provider availability.  Full profiles use the same report schema with
larger workloads and explicit external fixtures.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from nanodelta.contracts import Market
from nanodelta.runtime.supervisor import MarketWorker, MemoryRuntimeStateStore, RuntimeSupervisor

AsyncOperation = Callable[[], Awaitable[None]]


class AcceptanceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Thresholds:
    api_p95_ms: float = 100
    decision_p95_ms: float = 100
    minimum_success_ratio: float = 1.0
    maximum_scheduler_drift_ms: float = 40
    maximum_drain_ms: float = 500
    maximum_recovery_attempts: int = 3


@dataclass(frozen=True)
class AcceptanceResult:
    check: str
    status: AcceptanceStatus
    measured: dict[str, float | int | str | bool]
    threshold: dict[str, float | int | str | bool]
    detail: str


@dataclass(frozen=True)
class AcceptanceReport:
    schema_version: str
    profile: str
    generated_at: str
    environment: str
    results: tuple[AcceptanceResult, ...]

    @property
    def passed(self) -> bool:
        return all(item.status is not AcceptanceStatus.FAIL for item in self.results)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["passed"] = self.passed
        return payload

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")


def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        raise ValueError("percentile requires observations")
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between zero and 100")
    ordered = sorted(values)
    index = max(0, math.ceil(percent / 100 * len(ordered)) - 1)
    return ordered[index]


async def latency_check(
    name: str,
    operation: AsyncOperation,
    *,
    iterations: int,
    concurrency: int,
    p95_limit_ms: float,
) -> AcceptanceResult:
    if iterations <= 0 or concurrency <= 0:
        raise ValueError("iterations and concurrency must be positive")
    semaphore = asyncio.Semaphore(concurrency)

    async def measured_call() -> tuple[float, bool]:
        async with semaphore:
            started = time.perf_counter()
            try:
                await operation()
                succeeded = True
            except Exception:
                succeeded = False
            return (time.perf_counter() - started) * 1000, succeeded

    observations = await asyncio.gather(*(measured_call() for _ in range(iterations)))
    latencies = [latency for latency, _ in observations]
    success_ratio = sum(success for _, success in observations) / iterations
    p95 = percentile(latencies, 95)
    passed = p95 <= p95_limit_ms and success_ratio == 1
    return AcceptanceResult(
        name,
        AcceptanceStatus.PASS if passed else AcceptanceStatus.FAIL,
        {
            "iterations": iterations,
            "concurrency": concurrency,
            "p95_ms": round(p95, 3),
            "success_ratio": success_ratio,
        },
        {"p95_ms_lte": p95_limit_ms, "success_ratio_gte": 1.0},
        "bounded in-process workload; not a network capacity result",
    )


async def recovery_check(
    name: str,
    operation: AsyncOperation,
    *,
    maximum_attempts: int,
    backoff_seconds: float = 0,
) -> AcceptanceResult:
    attempts = 0
    recovered = False
    last_error = ""
    while attempts < maximum_attempts and not recovered:
        attempts += 1
        try:
            await operation()
            recovered = True
        except Exception as exc:
            last_error = type(exc).__name__
            if backoff_seconds:
                await asyncio.sleep(backoff_seconds)
    return AcceptanceResult(
        name,
        AcceptanceStatus.PASS if recovered else AcceptanceStatus.FAIL,
        {"attempts": attempts, "recovered": recovered, "last_error": last_error},
        {"maximum_attempts": maximum_attempts},
        "fault injection with deterministic recovery",
    )


async def scheduler_drift_check(
    *, interval_seconds: float, cycles: int, maximum_drift_ms: float
) -> AcceptanceResult:
    starts: list[float] = []
    target = time.perf_counter()
    for _ in range(cycles):
        await asyncio.sleep(max(0, target - time.perf_counter()))
        starts.append(time.perf_counter())
        target += interval_seconds
    drifts = [
        abs((starts[index] - starts[0]) - index * interval_seconds) * 1000
        for index in range(len(starts))
    ]
    maximum = max(drifts)
    return AcceptanceResult(
        "scheduler_drift",
        AcceptanceStatus.PASS if maximum <= maximum_drift_ms else AcceptanceStatus.FAIL,
        {"cycles": cycles, "maximum_drift_ms": round(maximum, 3)},
        {"maximum_drift_ms_lte": maximum_drift_ms},
        "monotonic-clock bounded scheduler probe",
    )


def report(profile: str, environment: str, results: Sequence[AcceptanceResult]) -> AcceptanceReport:
    return AcceptanceReport(
        schema_version="1.0",
        profile=profile,
        generated_at=datetime.now(UTC).isoformat(),
        environment=environment,
        results=tuple(results),
    )


async def worker_soak_check(
    *, duration_seconds: float, interval_seconds: float, maximum_drain_ms: float
) -> AcceptanceResult:
    """Exercise all three market workers and verify bounded graceful drain."""
    if duration_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("soak duration and interval must be positive")
    cycles = {market: 0 for market in Market}

    async def cycle(market: Market) -> None:
        cycles[market] += 1
        await asyncio.sleep(0)

    workers = {
        market: MarketWorker(
            market,
            "acceptance-soak",
            cycle,
            MemoryRuntimeStateStore(),
            interval_seconds=interval_seconds,
            heartbeat_seconds=max(interval_seconds, 0.001),
        )
        for market in Market
    }
    supervisor = RuntimeSupervisor(workers)
    await supervisor.start()
    await asyncio.sleep(duration_seconds)
    drain_started = time.perf_counter()
    await supervisor.shutdown(drain_timeout_seconds=max(1, maximum_drain_ms / 1000))
    drain_ms = (time.perf_counter() - drain_started) * 1000
    all_markets_ran = all(count > 0 for count in cycles.values())
    passed = all_markets_ran and drain_ms <= maximum_drain_ms
    return AcceptanceResult(
        "three_market_worker_soak",
        AcceptanceStatus.PASS if passed else AcceptanceStatus.FAIL,
        {
            "duration_seconds": duration_seconds,
            "nse_cycles": cycles[Market.NSE],
            "forex_cycles": cycles[Market.FOREX],
            "crypto_cycles": cycles[Market.CRYPTO],
            "drain_ms": round(drain_ms, 3),
        },
        {"all_markets_cycles_gt": 0, "drain_ms_lte": maximum_drain_ms},
        "bounded local scheduler soak; full profile is opt-in",
    )
