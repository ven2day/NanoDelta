from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nanodelta.contracts import Market, Provider
from nanodelta.history import (
    BackfillEngine,
    HistoryJob,
    HistoryRunState,
    InMemoryHistoryState,
    MarketCalendar,
    ReadinessState,
)
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import HistoricalRequest, ProviderCapability
from nanodelta.providers.registry import ProviderRegistry
from nanodelta.storage import FileLake

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)


class FakeHistoryClient:
    market = Market.CRYPTO

    def __init__(
        self, provider: Provider, rows: list[dict[str, Any]], *, fail: bool = False
    ) -> None:
        self.provider = provider
        self.rows = rows
        self.fail = fail
        self.requests: list[HistoricalRequest] = []

    async def fetch_candles(self, request: HistoricalRequest) -> list[dict[str, Any]]:
        self.requests.append(request)
        if self.fail:
            raise TimeoutError("provider unavailable")
        return self.rows


def candle(day: int) -> dict[str, Any]:
    opened = datetime(2026, 8, day, tzinfo=UTC)
    return {
        "ts": str(int(opened.timestamp() * 1000)),
        "o": "100",
        "h": "110",
        "l": "90",
        "c": "105",
        "vol": "10",
        "confirm": "1",
        "bar": "1d",
    }


def setup_engine(tmp_path: Path, primary: FakeHistoryClient, fallback: FakeHistoryClient):
    registry = ProviderRegistry()
    registry.register(
        Market.CRYPTO,
        ProviderCapability.HISTORICAL_CANDLES,
        (Provider.OKX, Provider.POLONIEX),
    )
    state = InMemoryHistoryState()
    engine = BackfillEngine(
        pipeline=EtlPipeline(FileLake(tmp_path)),
        registry=registry,
        clients={Provider.OKX: primary, Provider.POLONIEX: fallback},
        state=state,
        calendars={Market.CRYPTO: MarketCalendar(Market.CRYPTO)},
    )
    job = HistoryJob(
        Market.CRYPTO,
        "BTC_USDT",
        "1d",
        {Provider.OKX: "BTC-USDT", Provider.POLONIEX: "BTC_USDT"},
        target_days=2,
    )
    return engine, state, job


@pytest.mark.asyncio
async def test_backfill_falls_back_commits_watermark_and_uses_actual_coverage(
    tmp_path: Path,
) -> None:
    primary = FakeHistoryClient(Provider.OKX, [], fail=True)
    fallback = FakeHistoryClient(Provider.POLONIEX, [candle(12), candle(13), candle(14)])
    fallback.rows = [
        {
            "startTime": row["ts"],
            "open": row["o"],
            "high": row["h"],
            "low": row["l"],
            "close": row["c"],
            "quantity": row["vol"],
            "settled": True,
            "interval": "1d",
        }
        for row in fallback.rows
    ]
    engine, state, job = setup_engine(tmp_path, primary, fallback)

    run = await engine.sync(job, now=NOW)
    status = engine.status(job, now=NOW)

    assert run.state is HistoryRunState.SUCCEEDED
    assert run.provider is Provider.POLONIEX
    assert state.watermark(Market.CRYPTO, Provider.POLONIEX, "BTC_USDT", "1d") is not None
    assert status.state is ReadinessState.READY
    assert status.actual_count == status.expected_count == 3


@pytest.mark.asyncio
async def test_incremental_load_uses_overlap_and_repair_uses_target_window(
    tmp_path: Path,
) -> None:
    primary = FakeHistoryClient(Provider.OKX, [candle(14)])
    fallback = FakeHistoryClient(Provider.POLONIEX, [])
    engine, state, job = setup_engine(tmp_path, primary, fallback)
    await engine.sync(job, now=NOW)

    await engine.sync(job, now=NOW)
    # Overlap is bounded by the configured 730-day (two-day here) target horizon.
    assert primary.requests[-1].start == datetime(2026, 8, 12, tzinfo=UTC)

    missing = datetime(2026, 8, 13, tzinfo=UTC)
    await engine.repair(job, (missing,), now=NOW)
    assert primary.requests[-1].start == missing
    assert primary.requests[-1].end == datetime(2026, 8, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_run_started_and_finished_at_reflect_real_time_not_the_now_parameter(
    tmp_path: Path,
) -> None:
    # NOW is a fixed, far-in-the-past window reference (used to define the
    # backfill horizon consistently across a batch); started_at/finished_at
    # must NOT collapse to that shared value, or every run in one sync pass
    # looks identical regardless of when it actually executed.
    primary = FakeHistoryClient(Provider.OKX, [candle(14)])
    fallback = FakeHistoryClient(Provider.POLONIEX, [])
    engine, _, job = setup_engine(tmp_path, primary, fallback)

    before = datetime.now(UTC)
    run = await engine.sync(job, now=NOW)
    after = datetime.now(UTC)

    assert run.started_at != NOW
    assert run.finished_at != NOW
    assert before <= run.started_at <= after
    assert run.finished_at is not None
    assert before <= run.finished_at <= after
    assert run.started_at <= run.finished_at


def test_market_calendar_never_counts_weekends_for_forex() -> None:
    calendar = MarketCalendar(Market.FOREX)
    opens = calendar.expected_opens(
        datetime(2026, 8, 14, tzinfo=UTC),
        datetime(2026, 8, 17, tzinfo=UTC),
        "1d",
    )
    assert opens == (
        datetime(2026, 8, 14, tzinfo=UTC),
        datetime(2026, 8, 17, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_backfill_chunks_full_horizon_in_chronological_windows(
    tmp_path: Path,
) -> None:
    primary = FakeHistoryClient(Provider.OKX, [candle(12), candle(14)])
    fallback = FakeHistoryClient(Provider.POLONIEX, [])
    engine, _, job = setup_engine(tmp_path, primary, fallback)
    job = HistoryJob(
        job.market,
        job.symbol,
        job.timeframe,
        job.provider_symbols,
        target_days=2,
        request_limit=2,
    )

    run = await engine.sync(job, now=NOW)

    assert run.state is HistoryRunState.SUCCEEDED
    assert [(request.start, request.end) for request in primary.requests] == [
        (
            datetime(2026, 8, 12, tzinfo=UTC),
            datetime(2026, 8, 14, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 14, tzinfo=UTC),
            datetime(2026, 8, 15, tzinfo=UTC),
        ),
    ]
