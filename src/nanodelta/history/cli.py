"""Long-running historical backfill process entry point.

Keeps the configured universe's Bronze/Silver/Gold history within HistoryJob's
target_days window by repeatedly syncing every configured (market, symbol,
timeframe) job on an interval, pulling from each market's historical provider.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import UTC, datetime
from pathlib import Path

from psycopg.conninfo import make_conninfo

from nanodelta.history.config import build_history_services
from nanodelta.history.engine import HistoryRunState
from nanodelta.observability import configure_json_logging

logger = logging.getLogger("nanodelta.history.cli")


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


async def sync_once(database_url: str) -> None:
    """One full pass over the configured universe. Rebuilds services each pass so
    a Dhan PIN/TOTP access token is refreshed before every sweep."""
    engines, jobs = await build_history_services(database_url)
    now = datetime.now(UTC)
    succeeded = failed = 0
    for (market, symbol, timeframe), job in jobs.items():
        engine = engines[market]
        try:
            run = await engine.sync(job, now=now)
        except Exception:
            failed += 1
            logger.exception(
                "history sync raised",
                extra={"market": market.value, "symbol": symbol, "timeframe": timeframe},
            )
            continue
        if run.state is HistoryRunState.SUCCEEDED:
            succeeded += 1
        else:
            failed += 1
        logger.info(
            "history sync complete",
            extra={
                "market": market.value,
                "symbol": symbol,
                "timeframe": timeframe,
                "state": run.state.value,
                "rows_received": run.rows_received,
                "bronze_created": run.bronze_created,
                "silver_created": run.silver_created,
                "error": run.error,
            },
        )
    logger.info(
        "history sync pass complete",
        extra={"jobs": len(jobs), "succeeded": succeeded, "failed": failed},
    )


async def run() -> None:
    configure_json_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(event, stop.set)

    database_url = _database_url()
    interval = float(os.environ.get("NANODELTA_HISTORY_SYNC_INTERVAL_SECONDS", "86400"))
    if interval <= 0:
        raise RuntimeError("NANODELTA_HISTORY_SYNC_INTERVAL_SECONDS must be positive")

    while not stop.is_set():
        try:
            await sync_once(database_url)
        except Exception:
            logger.exception("history sync pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
