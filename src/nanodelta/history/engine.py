"""Market-isolated historical orchestration over existing provider clients."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from nanodelta.contracts import EventType, Market, Provider, stable_id, utc
from nanodelta.history.timeframes import MarketCalendar, last_settled_open, timeframe_delta
from nanodelta.pipeline import EtlPipeline
from nanodelta.providers.base import HistoricalClient, HistoricalRequest, ProviderCapability
from nanodelta.providers.registry import ProviderRegistry


class HistoryRunState(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ReadinessState(StrEnum):
    READY = "READY"
    BACKFILLING = "BACKFILLING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE = "STALE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class HistoryJob:
    market: Market
    symbol: str
    timeframe: str
    provider_symbols: Mapping[Provider, str]
    target_days: int = 730
    overlap_bars: int = 3
    request_limit: int = 1000
    window_start: datetime | None = None
    window_end: datetime | None = None

    def __post_init__(self) -> None:
        timeframe_delta(self.timeframe)
        if not self.symbol or self.target_days < 1 or self.overlap_bars < 0:
            raise ValueError("history job settings are invalid")
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("both history window overrides are required")
        if self.window_start is not None and self.window_end is not None:
            if utc(self.window_start, "window_start") >= utc(self.window_end, "window_end"):
                raise ValueError("history window start must be before end")


@dataclass(frozen=True)
class Watermark:
    market: Market
    provider: Provider
    symbol: str
    timeframe: str
    event_time: datetime
    updated_at: datetime


@dataclass(frozen=True)
class HistoryRun:
    run_id: str
    market: Market
    symbol: str
    timeframe: str
    state: HistoryRunState
    started_at: datetime
    finished_at: datetime | None
    provider: Provider | None
    rows_received: int
    bronze_created: int
    silver_created: int
    error: str | None


@dataclass(frozen=True)
class HistoryStatus:
    market: Market
    symbol: str
    timeframe: str
    state: ReadinessState
    target_start: datetime
    settled_end: datetime
    first_open: datetime | None
    last_open: datetime | None
    expected_count: int
    actual_count: int
    gap_count: int
    gaps: tuple[datetime, ...]


class InMemoryHistoryState:
    def __init__(self) -> None:
        self.watermarks: dict[tuple[Market, Provider, str, str], Watermark] = {}
        self.coverage: dict[tuple[Market, str, str], set[datetime]] = {}
        self.runs: dict[str, HistoryRun] = {}

    def covered(self, market: Market, symbol: str, timeframe: str) -> set[datetime]:
        return set(self.coverage.get((market, symbol, timeframe), set()))

    def record_open(self, market: Market, symbol: str, timeframe: str, value: datetime) -> None:
        self.coverage.setdefault((market, symbol, timeframe), set()).add(utc(value, "open_time"))

    def watermark(
        self, market: Market, provider: Provider, symbol: str, timeframe: str
    ) -> Watermark | None:
        return self.watermarks.get((market, provider, symbol, timeframe))

    def commit_watermark(self, value: Watermark) -> None:
        key = (value.market, value.provider, value.symbol, value.timeframe)
        current = self.watermarks.get(key)
        if current is None or value.event_time >= current.event_time:
            self.watermarks[key] = value

    def save_run(self, run: HistoryRun) -> None:
        self.runs[run.run_id] = run

    def has_run_state(
        self, market: Market, symbol: str, timeframe: str, state: HistoryRunState
    ) -> bool:
        return any(
            run.market is market
            and run.symbol == symbol
            and run.timeframe == timeframe
            and run.state is state
            for run in self.runs.values()
        )


class HistoryState(Protocol):
    def covered(self, market: Market, symbol: str, timeframe: str) -> set[datetime]: ...
    def record_open(self, market: Market, symbol: str, timeframe: str, value: datetime) -> None: ...
    def watermark(
        self, market: Market, provider: Provider, symbol: str, timeframe: str
    ) -> Watermark | None: ...
    def commit_watermark(self, value: Watermark) -> None: ...
    def save_run(self, run: HistoryRun) -> None: ...
    def has_run_state(
        self, market: Market, symbol: str, timeframe: str, state: HistoryRunState
    ) -> bool: ...


class BackfillEngine:
    def __init__(
        self,
        *,
        pipeline: EtlPipeline,
        registry: ProviderRegistry,
        clients: Mapping[Provider, HistoricalClient],
        state: HistoryState,
        calendars: Mapping[Market, MarketCalendar],
    ) -> None:
        self._pipeline = pipeline
        self._registry = registry
        self._clients = clients
        self._state = state
        self._calendars = calendars

    async def sync(self, job: HistoryJob, *, now: datetime) -> HistoryRun:
        now = utc(now, "now")
        # `now` is the shared, single snapshot passed to every job in one sync
        # pass -- correct for defining a consistent backfill window across the
        # whole pass, but wrong for the run's own started_at/finished_at, which
        # must reflect when THIS job actually ran in real wall-clock time
        # (started_at) rather than when the pass began (now), or every job in a
        # pass appears frozen at the same instant regardless of how long it
        # actually took.
        started_at = datetime.now(UTC)
        end = (
            min(utc(job.window_end, "window_end"), last_settled_open(now, job.timeframe))
            if job.window_end is not None
            else last_settled_open(now, job.timeframe)
        )
        route = self._registry.route(job.market, ProviderCapability.HISTORICAL_CANDLES)
        run_id = stable_id(job.market.value, job.symbol, job.timeframe, now.isoformat())
        run = HistoryRun(
            run_id,
            job.market,
            job.symbol,
            job.timeframe,
            HistoryRunState.RUNNING,
            started_at,
            None,
            None,
            0,
            0,
            0,
            None,
        )
        self._state.save_run(run)
        errors: list[str] = []
        for provider in route:
            client = self._clients.get(provider)
            provider_symbol = job.provider_symbols.get(provider)
            if client is None or provider_symbol is None:
                errors.append(f"{provider.value}: unavailable")
                continue
            if client.market is not job.market or client.provider is not provider:
                errors.append(f"{provider.value}: ownership mismatch")
                continue
            watermark = self._state.watermark(job.market, provider, job.symbol, job.timeframe)
            step = timeframe_delta(job.timeframe)
            target_start = (
                utc(job.window_start, "window_start")
                if job.window_start is not None
                else end - timedelta(days=job.target_days)
            )
            start = (
                target_start
                if job.window_start is not None
                else (
                    max(target_start, watermark.event_time - step * job.overlap_bars)
                    if watermark
                    else target_start
                )
            )
            try:
                bronze = silver = 0
                rows_received = 0
                settled_opens: list[datetime] = []
                cursor = start
                while cursor <= end:
                    window_end = min(
                        end + step,
                        cursor + step * job.request_limit,
                    )
                    rows = await client.fetch_candles(
                        HistoricalRequest(
                            provider_symbol,
                            job.timeframe,
                            cursor,
                            window_end,
                            job.request_limit,
                        )
                    )
                    rows_received += len(rows)
                    for payload in rows:
                        result = self._pipeline.ingest(
                            market=job.market,
                            provider=provider,
                            event_type=EventType.CANDLE,
                            provider_symbol=provider_symbol,
                            payload=dict(payload),
                            received_at=now,
                        )
                        bronze += int(result.bronze_created)
                        silver += int(result.silver_created)
                        if result.canonical is not None and result.canonical.is_settled:
                            opened = result.canonical.open_time
                            if (
                                result.canonical.symbol == job.symbol
                                and result.canonical.timeframe == job.timeframe
                                and start <= opened <= end
                            ):
                                self._state.record_open(
                                    job.market, job.symbol, job.timeframe, opened
                                )
                                settled_opens.append(opened)
                    cursor = window_end
                if not settled_opens:
                    errors.append(f"{provider.value}: no matching settled candles")
                    continue
                self._state.commit_watermark(
                    Watermark(
                        job.market,
                        provider,
                        job.symbol,
                        job.timeframe,
                        max(settled_opens),
                        now,
                    )
                )
                run = replace(
                    run,
                    state=HistoryRunState.SUCCEEDED,
                    finished_at=datetime.now(UTC),
                    provider=provider,
                    rows_received=rows_received,
                    bronze_created=bronze,
                    silver_created=silver,
                )
                self._state.save_run(run)
                return run
            except Exception as exc:
                errors.append(f"{provider.value}: {type(exc).__name__}: {exc}")
        run = replace(
            run,
            state=HistoryRunState.FAILED,
            finished_at=datetime.now(UTC),
            error="; ".join(errors),
        )
        self._state.save_run(run)
        return run

    def status(self, job: HistoryJob, *, now: datetime) -> HistoryStatus:
        end = last_settled_open(now, job.timeframe)
        start = end - timedelta(days=job.target_days)
        expected = self._calendars[job.market].expected_opens(start, end, job.timeframe)
        actual_all = self._state.covered(job.market, job.symbol, job.timeframe)
        actual = tuple(opened for opened in expected if opened in actual_all)
        gaps = tuple(opened for opened in expected if opened not in actual_all)
        last = max(actual_all) if actual_all else None
        running = self._state.has_run_state(
            job.market, job.symbol, job.timeframe, HistoryRunState.RUNNING
        )
        failed = self._state.has_run_state(
            job.market, job.symbol, job.timeframe, HistoryRunState.FAILED
        )
        if running:
            state = ReadinessState.BACKFILLING
        elif not gaps:
            state = ReadinessState.READY
        elif last is not None and end - last > timeframe_delta(job.timeframe):
            state = ReadinessState.STALE
        elif failed:
            state = ReadinessState.FAILED
        else:
            state = ReadinessState.INSUFFICIENT_DATA
        return HistoryStatus(
            job.market,
            job.symbol,
            job.timeframe,
            state,
            start,
            end,
            min(actual_all) if actual_all else None,
            last,
            len(expected),
            len(actual),
            len(gaps),
            gaps,
        )

    async def repair(
        self, job: HistoryJob, gaps: tuple[datetime, ...], *, now: datetime
    ) -> tuple[HistoryRun, ...]:
        if not gaps:
            return ()
        ordered = sorted({utc(gap, "gap") for gap in gaps})
        step = timeframe_delta(job.timeframe)
        groups: list[list[datetime]] = [[ordered[0]]]
        for gap in ordered[1:]:
            if gap == groups[-1][-1] + step:
                groups[-1].append(gap)
            else:
                groups.append([gap])
        runs = []
        for index, group in enumerate(groups):
            repair_job = replace(
                job,
                window_start=group[0],
                window_end=group[-1] + step,
            )
            run = await self.sync(repair_job, now=now + timedelta(microseconds=index))
            covered = self._state.covered(job.market, job.symbol, job.timeframe)
            remaining = [gap for gap in group if gap not in covered]
            if remaining and run.state is HistoryRunState.SUCCEEDED:
                run = replace(
                    run,
                    state=HistoryRunState.FAILED,
                    error=f"targeted gaps remain: {len(remaining)}",
                )
                self._state.save_run(run)
            runs.append(run)
        return tuple(runs)
