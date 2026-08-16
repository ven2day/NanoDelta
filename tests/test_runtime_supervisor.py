from __future__ import annotations

import asyncio

import pytest

from nanodelta.contracts import Market
from nanodelta.runtime.supervisor import (
    MarketWorker,
    MemoryRuntimeStateStore,
    RuntimeState,
    RuntimeSupervisor,
)


@pytest.mark.asyncio
async def test_supervisor_runs_every_market_and_drains_cleanly() -> None:
    store = MemoryRuntimeStateStore()
    called: list[Market] = []

    async def cycle(market: Market) -> None:
        called.append(market)

    workers = {
        market: MarketWorker(
            market,
            "test-instance",
            cycle,
            store,
            interval_seconds=0.01,
            heartbeat_seconds=0.01,
        )
        for market in Market
    }
    supervisor = RuntimeSupervisor(workers)
    await supervisor.start()
    await asyncio.sleep(0.04)
    await supervisor.shutdown(drain_timeout_seconds=1)

    assert set(called) == set(Market)
    assert all(snapshot.state is RuntimeState.STOPPED for snapshot in supervisor.snapshots.values())
    assert set(store.snapshots) == set(Market)


@pytest.mark.asyncio
async def test_drain_waits_for_inflight_cycle_and_starts_no_new_cycle() -> None:
    store = MemoryRuntimeStateStore()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def cycle(market: Market) -> None:
        nonlocal calls
        del market
        calls += 1
        entered.set()
        await release.wait()

    worker = MarketWorker(
        Market.NSE,
        "test-instance",
        cycle,
        store,
        interval_seconds=0.001,
        heartbeat_seconds=0.01,
    )
    await worker.start()
    await entered.wait()
    draining = asyncio.create_task(worker.drain())
    await asyncio.sleep(0)
    assert worker.snapshot.state is RuntimeState.DRAINING
    release.set()
    await draining

    assert calls == 1
    assert worker.snapshot.state is RuntimeState.STOPPED


@pytest.mark.asyncio
async def test_cycle_failure_does_not_kill_worker() -> None:
    store = MemoryRuntimeStateStore()
    calls = 0

    async def cycle(market: Market) -> None:
        nonlocal calls
        del market
        calls += 1
        if calls == 1:
            raise RuntimeError("provider unavailable")

    worker = MarketWorker(
        Market.FOREX,
        "test-instance",
        cycle,
        store,
        interval_seconds=0.01,
        heartbeat_seconds=0.01,
    )
    await worker.start()
    await asyncio.sleep(0.035)
    await worker.drain()

    assert calls >= 2
    assert worker.snapshot.last_cycle_finished is not None
    assert worker.snapshot.state is RuntimeState.STOPPED


def test_supervisor_requires_equal_market_coverage() -> None:
    with pytest.raises(ValueError, match="missing"):
        RuntimeSupervisor({})


def test_worker_rejects_invalid_intervals() -> None:
    async def cycle(market: Market) -> None:
        del market

    with pytest.raises(ValueError, match="positive"):
        MarketWorker(
            Market.CRYPTO,
            "test-instance",
            cycle,
            MemoryRuntimeStateStore(),
            interval_seconds=0,
        )


@pytest.mark.asyncio
async def test_supervisor_cancels_cycle_after_drain_deadline() -> None:
    async def cycle(market: Market) -> None:
        del market
        await asyncio.Event().wait()

    workers = {
        market: MarketWorker(
            market,
            "test-instance",
            cycle,
            MemoryRuntimeStateStore(),
            interval_seconds=1,
        )
        for market in Market
    }
    supervisor = RuntimeSupervisor(workers)
    await supervisor.start()
    await asyncio.sleep(0)
    await supervisor.shutdown(drain_timeout_seconds=0.01)

    assert all(snapshot.state is RuntimeState.STOPPED for snapshot in supervisor.snapshots.values())


@pytest.mark.asyncio
async def test_continuous_worker_does_not_apply_scheduled_cycle_delay() -> None:
    calls = 0
    reached = asyncio.Event()

    async def cycle(market: Market) -> None:
        nonlocal calls
        del market
        calls += 1
        if calls == 3:
            reached.set()

    worker = MarketWorker(
        Market.CRYPTO,
        "continuous-test",
        cycle,
        MemoryRuntimeStateStore(),
        interval_seconds=3600,
        heartbeat_seconds=1,
        continuous=True,
    )
    await worker.start()
    await asyncio.wait_for(reached.wait(), timeout=0.2)
    await worker.drain()
    assert calls >= 3


@pytest.mark.asyncio
async def test_worker_closes_persistent_cycle_on_drain() -> None:
    class ClosableCycle:
        def __init__(self) -> None:
            self.closed = False

        async def __call__(self, market: Market) -> None:
            del market

        async def aclose(self) -> None:
            self.closed = True

    cycle = ClosableCycle()
    worker = MarketWorker(
        Market.CRYPTO,
        "close-test",
        cycle,
        MemoryRuntimeStateStore(),
        interval_seconds=0.001,
        heartbeat_seconds=0.01,
    )
    await worker.start()
    await asyncio.sleep(0)
    await worker.drain()

    assert cycle.closed is True
