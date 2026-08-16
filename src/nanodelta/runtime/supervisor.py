"""Async scheduler and supervisor for the three equal market runtimes.

The supervisor owns process lifecycle only.  A ``cycle`` callback composes the
market-specific data, decision, risk and paper-execution services.  This keeps
provider code replaceable and prevents the supervisor from obtaining order
authority.  Live broker execution is deliberately not represented here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nanodelta.contracts import Market

if TYPE_CHECKING:
    from nanodelta.observability import RuntimeMetrics

LOGGER = logging.getLogger(__name__)
Clock = Callable[[], datetime]
Cycle = Callable[[Market], Awaitable[None]]


@runtime_checkable
class AsyncClosable(Protocol):
    def aclose(self) -> Awaitable[None]: ...


class RuntimeState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class WorkerSnapshot:
    market: Market
    instance_id: str
    state: RuntimeState
    last_heartbeat: datetime
    last_cycle_started: datetime | None = None
    last_cycle_finished: datetime | None = None
    last_error: str | None = None


class RuntimeStateStore(Protocol):
    async def save(self, snapshot: WorkerSnapshot) -> None: ...


class MemoryRuntimeStateStore:
    def __init__(self) -> None:
        self.snapshots: dict[Market, WorkerSnapshot] = {}

    async def save(self, snapshot: WorkerSnapshot) -> None:
        self.snapshots[snapshot.market] = snapshot


def _now() -> datetime:
    return datetime.now(UTC)


class MarketWorker:
    """Runs one market serially and drains without starting another cycle."""

    def __init__(
        self,
        market: Market,
        instance_id: str,
        cycle: Cycle,
        store: RuntimeStateStore,
        *,
        interval_seconds: float,
        heartbeat_seconds: float = 10,
        continuous: bool = False,
        clock: Clock = _now,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        if interval_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("worker intervals must be positive")
        self.market = market
        self.instance_id = instance_id
        self._cycle = cycle
        self._store = store
        self._interval = interval_seconds
        self._heartbeat = heartbeat_seconds
        self._continuous = continuous
        self._clock = clock
        self._metrics = metrics
        self._stop = asyncio.Event()
        self._drain = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._snapshot = WorkerSnapshot(market, instance_id, RuntimeState.STOPPED, clock())

    @property
    def snapshot(self) -> WorkerSnapshot:
        return self._snapshot

    async def _publish(
        self,
        state: RuntimeState | None = None,
        *,
        last_cycle_started: datetime | None = None,
        last_cycle_finished: datetime | None = None,
        last_error: str | None = None,
        update_started: bool = False,
        update_finished: bool = False,
        update_error: bool = False,
    ) -> None:
        current = self._snapshot
        self._snapshot = WorkerSnapshot(
            market=current.market,
            instance_id=current.instance_id,
            state=state or current.state,
            last_heartbeat=self._clock(),
            last_cycle_started=(
                last_cycle_started if update_started else current.last_cycle_started
            ),
            last_cycle_finished=(
                last_cycle_finished if update_finished else current.last_cycle_finished
            ),
            last_error=last_error if update_error else current.last_error,
        )
        await self._store.save(self._snapshot)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._drain.clear()
        await self._publish(RuntimeState.STARTING, last_error=None, update_error=True)
        self._task = asyncio.create_task(self._run(), name=f"nanodelta-{self.market.value}")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"nanodelta-{self.market.value}-heartbeat"
        )

    async def drain(self) -> None:
        if self._task is None or self._task.done():
            return
        self._drain.set()
        await self._publish(RuntimeState.DRAINING)
        await self._task
        await self._finish_heartbeat()
        await self._close_cycle()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        await self._finish_heartbeat()
        await self._close_cycle()

    async def cancel(self) -> None:
        """Last-resort termination after the supervisor's drain deadline."""
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._finish_heartbeat()
        await self._close_cycle()

    async def _close_cycle(self) -> None:
        if isinstance(self._cycle, AsyncClosable):
            await self._cycle.aclose()

    async def _finish_heartbeat(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set() and self.snapshot.state is not RuntimeState.STOPPED:
            await self._publish()
            await asyncio.sleep(self._heartbeat)

    async def _run(self) -> None:
        await self._publish(RuntimeState.RUNNING)
        try:
            while not self._stop.is_set() and not self._drain.is_set():
                started = self._clock()
                await self._publish(
                    last_cycle_started=started,
                    last_error=None,
                    update_started=True,
                    update_error=True,
                )
                try:
                    timer = time.perf_counter()
                    await self._cycle(self.market)
                    if self._metrics is not None:
                        self._metrics.observe_cycle(
                            self.market, "success", time.perf_counter() - timer
                        )
                    await self._publish(
                        last_cycle_finished=self._clock(),
                        last_error=None,
                        update_finished=True,
                        update_error=True,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._metrics is not None:
                        self._metrics.observe_cycle(
                            self.market, "error", time.perf_counter() - timer
                        )
                    LOGGER.exception("market cycle failed", extra={"market": self.market.value})
                    await self._publish(
                        last_cycle_finished=self._clock(),
                        last_error=str(exc),
                        update_finished=True,
                        update_error=True,
                    )
                if self._continuous:
                    await asyncio.sleep(0)
                else:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            await self._publish(
                RuntimeState.FAILED, last_error="worker cancelled", update_error=True
            )
            raise
        finally:
            await self._publish(RuntimeState.STOPPED)


class RuntimeSupervisor:
    """Starts and stops exactly one worker for every configured market."""

    def __init__(self, workers: Mapping[Market, MarketWorker]) -> None:
        if set(workers) != set(Market):
            missing = sorted(m.value for m in set(Market) - set(workers))
            extra = sorted(m.value for m in set(workers) - set(Market))
            raise ValueError(f"workers must cover all markets; missing={missing}, extra={extra}")
        self._workers = dict(workers)

    @property
    def snapshots(self) -> dict[Market, WorkerSnapshot]:
        return {market: worker.snapshot for market, worker in self._workers.items()}

    async def start(self) -> None:
        await asyncio.gather(*(worker.start() for worker in self._workers.values()))

    async def shutdown(self, *, drain_timeout_seconds: float = 30) -> None:
        if drain_timeout_seconds <= 0:
            raise ValueError("drain timeout must be positive")
        try:
            async with asyncio.timeout(drain_timeout_seconds):
                await asyncio.gather(*(worker.drain() for worker in self._workers.values()))
        except TimeoutError:
            LOGGER.error("drain timed out; cancelling remaining worker cycles")
            await asyncio.gather(*(worker.cancel() for worker in self._workers.values()))
