"""Long-running NanoDelta worker process entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from nanodelta.contracts import Market
from nanodelta.runtime.postgres import PostgresRuntimeStateStore
from nanodelta.runtime.realtime_config import build_realtime_cycles
from nanodelta.runtime.supervisor import MarketWorker, RuntimeSupervisor


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
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
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
    cycles = build_realtime_cycles(database_url)
    workers = {
        market: MarketWorker(
            market,
            instance_id,
            cycles[market],
            store,
            interval_seconds=interval,
            heartbeat_seconds=heartbeat,
            continuous=True,
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
