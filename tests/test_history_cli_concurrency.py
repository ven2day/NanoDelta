"""history/cli.py's sync_once() runs jobs with bounded concurrency instead of one at
a time -- a 272-symbol universe at ~30-60s per fine-grained job would otherwise take
days. This tests the concurrency bound itself, not the real BackfillEngine/Dhan I/O
underneath it (covered elsewhere)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest

from nanodelta.contracts import Market
from nanodelta.history.cli import _sync_one
from nanodelta.history.engine import BackfillEngine, HistoryJob, HistoryRun, HistoryRunState

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)


class TrackingFakeEngine:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def sync(self, job: HistoryJob, *, now: datetime) -> HistoryRun:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)  # yield so overlapping calls are actually observed
        self.active -= 1
        return HistoryRun(
            f"run-{job.symbol}-{job.timeframe}",
            job.market,
            job.symbol,
            job.timeframe,
            HistoryRunState.SUCCEEDED,
            now,
            now,
            None,
            10,
            10,
            10,
            None,
        )


def job(symbol: str) -> HistoryJob:
    return HistoryJob(Market.NSE, symbol, "5m", {})


@pytest.mark.asyncio
async def test_sync_one_respects_the_concurrency_semaphore() -> None:
    fake = TrackingFakeEngine()
    engine = cast(BackfillEngine, fake)
    semaphore = asyncio.Semaphore(2)

    await asyncio.gather(
        *(
            _sync_one(
                engine,
                job(symbol),
                market=Market.NSE,
                symbol=symbol,
                timeframe="5m",
                now=NOW,
                semaphore=semaphore,
            )
            for symbol in ("A", "B", "C", "D", "E")
        )
    )

    assert fake.max_active <= 2


@pytest.mark.asyncio
async def test_sync_one_reports_success_and_swallows_exceptions_as_failure() -> None:
    class RaisingEngine:
        async def sync(self, job: HistoryJob, *, now: datetime) -> HistoryRun:
            del job, now
            raise RuntimeError("provider unavailable")

    semaphore = asyncio.Semaphore(1)
    ok = await _sync_one(
        cast(BackfillEngine, TrackingFakeEngine()),
        job("A"),
        market=Market.NSE,
        symbol="A",
        timeframe="5m",
        now=NOW,
        semaphore=semaphore,
    )
    failed = await _sync_one(
        cast(BackfillEngine, RaisingEngine()),
        job("B"),
        market=Market.NSE,
        symbol="B",
        timeframe="5m",
        now=NOW,
        semaphore=semaphore,
    )
    assert ok is True
    assert failed is False
