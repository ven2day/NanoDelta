"""Long-running NanoDelta worker process entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

import psycopg

from nanodelta.contracts import Market
from nanodelta.runtime.postgres import PostgresRuntimeStateStore
from nanodelta.runtime.supervisor import MarketWorker, RuntimeSupervisor


async def _paper_cycle(market: Market) -> None:
    """Composition seam for market services; intentionally creates no live orders.

    Provider streams and approved strategy runners are connected here in later
    checkpoints.  Keeping the idle cycle explicit is safer than fabricating
    provider readiness or trading from incomplete realtime data.
    """

    logging.getLogger(__name__).debug("market scheduler tick", extra={"market": market.value})


async def run() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(event, stop.set)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    store = PostgresRuntimeStateStore(lambda: psycopg.connect(database_url))
    instance_id = os.environ.get("NANODELTA_INSTANCE_ID", socket.gethostname())
    interval = float(os.environ.get("NANODELTA_CYCLE_SECONDS", "60"))
    heartbeat = float(os.environ.get("NANODELTA_HEARTBEAT_SECONDS", "10"))
    workers = {
        market: MarketWorker(
            market,
            instance_id,
            _paper_cycle,
            store,
            interval_seconds=interval,
            heartbeat_seconds=heartbeat,
        )
        for market in Market
    }
    supervisor = RuntimeSupervisor(workers)
    await supervisor.start()
    await stop.wait()
    await supervisor.shutdown(
        drain_timeout_seconds=float(os.environ.get("NANODELTA_DRAIN_TIMEOUT_SECONDS", "30"))
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
