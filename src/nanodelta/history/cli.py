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
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from psycopg.conninfo import make_conninfo

from nanodelta.contracts import Market
from nanodelta.history.config import build_history_services, build_index_history_services
from nanodelta.history.engine import BackfillEngine, HistoryJob, HistoryRunState
from nanodelta.observability import configure_json_logging

logger = logging.getLogger("nanodelta.history.cli")


class _HealthRequestHandler(BaseHTTPRequestHandler):
    """Serves a bare liveness probe -- the history worker has no other HTTP surface."""

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/health/live":
            body = b'{"status": "alive"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def _start_health_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthRequestHandler)  # noqa: S104
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


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


async def _sync_one(
    engine: BackfillEngine,
    job: HistoryJob,
    *,
    market: Market,
    symbol: str,
    timeframe: str,
    now: datetime,
    semaphore: asyncio.Semaphore,
) -> bool:
    async with semaphore:
        try:
            run = await engine.sync(job, now=now)
        except Exception:
            logger.exception(
                "history sync raised",
                extra={"market": market.value, "symbol": symbol, "timeframe": timeframe},
            )
            return False
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
        return run.state is HistoryRunState.SUCCEEDED


async def sync_once(database_url: str) -> None:
    """One full pass over the configured universe. Rebuilds services each pass so
    a Dhan PIN/TOTP access token is refreshed before every sweep.

    Jobs run with bounded concurrency instead of one at a time -- a 272-symbol
    universe times 6 timeframes is ~1,600 jobs; sequentially, at the ~30-60s each a
    fine-grained 730-day pull takes, that's on the order of days. Every job opens its
    own DB connection and issues its own HTTP request (see BackfillEngine), so this
    is safe; the concurrency cap exists to stay polite to Dhan's historical API, not
    because of a shared-state hazard."""
    engines, jobs = await build_history_services(database_url)
    now = datetime.now(UTC)
    limit = int(os.environ.get("NANODELTA_HISTORY_CONCURRENCY", "20"))
    if limit <= 0:
        raise RuntimeError("NANODELTA_HISTORY_CONCURRENCY must be positive")
    semaphore = asyncio.Semaphore(limit)
    results = await asyncio.gather(
        *(
            _sync_one(
                engines[market],
                job,
                market=market,
                symbol=symbol,
                timeframe=timeframe,
                now=now,
                semaphore=semaphore,
            )
            for (market, symbol, timeframe), job in jobs.items()
        )
    )
    succeeded = sum(1 for result in results if result)
    logger.info(
        "history sync pass complete",
        extra={"jobs": len(jobs), "succeeded": succeeded, "failed": len(jobs) - succeeded},
    )
    index_services = await build_index_history_services(database_url)
    if index_services is not None:
        index_engine, index_jobs = index_services
        index_semaphore = asyncio.Semaphore(min(5, limit))
        index_results = await asyncio.gather(
            *(
                _sync_one(
                    index_engine,
                    job,
                    market=market,
                    symbol=symbol,
                    timeframe=timeframe,
                    now=now,
                    semaphore=index_semaphore,
                )
                for (market, symbol, timeframe), job in index_jobs.items()
            )
        )
        index_succeeded = sum(1 for result in index_results if result)
        logger.info(
            "index sync pass complete",
            extra={
                "jobs": len(index_jobs),
                "succeeded": index_succeeded,
                "failed": len(index_jobs) - index_succeeded,
            },
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
    health_port = int(os.environ.get("NANODELTA_HISTORY_HEALTH_PORT", "9102"))
    if not 1 <= health_port <= 65535:
        raise RuntimeError("NANODELTA_HISTORY_HEALTH_PORT must be between 1 and 65535")
    _start_health_server(health_port)

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
