"""Long-running NanoDelta worker process entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from pathlib import Path

import psycopg
from prometheus_client import start_http_server
from psycopg.conninfo import make_conninfo

from nanodelta.contracts import Market
from nanodelta.observability import RuntimeMetrics, configure_json_logging
from nanodelta.runtime.control import PostgresRuntimeCommandMailbox, RuntimeCommandConsumer
from nanodelta.runtime.postgres import PostgresRuntimeStateStore
from nanodelta.runtime.realtime import RealtimeMarketCycle
from nanodelta.runtime.realtime_config import build_index_cycle, build_realtime_cycles
from nanodelta.runtime.supervisor import MarketWorker, RuntimeSupervisor

logger = logging.getLogger("nanodelta.runtime.cli")


async def _run_index_feed(
    cycle: RealtimeMarketCycle, stop: asyncio.Event, *, interval_seconds: float
) -> None:
    """Continuous, always-on ingestion for the NIFTY/sector-index/VIX feed --
    intentionally not gated by the NSE start/stop command lifecycle, since
    regime classification should keep working regardless of whether new
    entries are currently allowed. Any single-cycle failure is logged and
    retried on the next tick rather than stopping the loop."""
    while not stop.is_set():
        try:
            await cycle.run_once()
        except Exception:
            logger.exception("index feed cycle failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def _database_url() -> str:
    configured = os.environ.get("DATABASE_URL", "").strip()
    if configured:
        return configured
    password_path = os.environ.get("POSTGRES_PASSWORD_FILE", "").strip()
    required = {
        "host": os.environ.get("POSTGRES_HOST", "").strip(),
        "dbname": os.environ.get("POSTGRES_DB", "").strip(),
        "user": os.environ.get("POSTGRES_USER", "").strip(),
        "password_file": password_path,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"database configuration missing: {', '.join(missing)}")
    password = Path(password_path).read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD_FILE points to an empty secret")
    return make_conninfo(
        host=required["host"],
        dbname=required["dbname"],
        user=required["user"],
        password=password,
    )


async def run() -> None:
    configure_json_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(event, stop.set)

    database_url = _database_url()
    store = PostgresRuntimeStateStore(lambda: psycopg.connect(database_url))
    instance_id = os.environ.get("NANODELTA_INSTANCE_ID", socket.gethostname())
    interval = float(os.environ.get("NANODELTA_CYCLE_SECONDS", "60"))
    heartbeat = float(os.environ.get("NANODELTA_HEARTBEAT_SECONDS", "10"))
    mode = os.environ.get("NANODELTA_RUNTIME_MODE", "").strip().lower()
    if mode != "realtime-paper":
        raise RuntimeError(
            "NANODELTA_RUNTIME_MODE must be 'realtime-paper'; "
            "an idle or live-order runtime is not supported"
        )
    metrics = RuntimeMetrics()
    metrics_port = int(os.environ.get("NANODELTA_RUNTIME_METRICS_PORT", "9101"))
    if not 1 <= metrics_port <= 65535:
        raise RuntimeError("NANODELTA_RUNTIME_METRICS_PORT must be between 1 and 65535")
    start_http_server(metrics_port, registry=metrics.registry)
    cycles = await build_realtime_cycles(database_url, metrics=metrics)
    index_cycle = await build_index_cycle(database_url, metrics=metrics)
    index_task = (
        asyncio.create_task(
            _run_index_feed(index_cycle, stop, interval_seconds=heartbeat)
        )
        if index_cycle is not None
        else None
    )
    workers = {
        market: MarketWorker(
            market,
            instance_id,
            cycles[market],
            store,
            interval_seconds=interval,
            heartbeat_seconds=heartbeat,
            continuous=True,
            metrics=metrics,
        )
        for market in Market
    }
    supervisor = RuntimeSupervisor(workers)
    drain_timeout = float(os.environ.get("NANODELTA_DRAIN_TIMEOUT_SECONDS", "30"))
    commands = RuntimeCommandConsumer(
        workers,
        PostgresRuntimeCommandMailbox(lambda: psycopg.connect(database_url), instance_id),
        transition_timeout_seconds=drain_timeout,
    )
    poll_seconds = float(os.environ.get("NANODELTA_COMMAND_POLL_SECONDS", "1"))
    if poll_seconds <= 0:
        raise RuntimeError("NANODELTA_COMMAND_POLL_SECONDS must be positive")
    while not stop.is_set():
        processed = await commands.process_one()
        if not processed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass
    await supervisor.shutdown(drain_timeout_seconds=drain_timeout)
    if index_task is not None:
        await index_task


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
