"""Settled timeframe boundaries and injectable market-session calendars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from nanodelta.contracts import Market, utc


def timeframe_delta(timeframe: str) -> timedelta:
    unit = timeframe[-1:]
    try:
        value = int(timeframe[:-1])
    except ValueError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe}") from exc
    multipliers = {"m": 60, "h": 3600, "d": 86400}
    if value < 1 or unit not in multipliers:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return timedelta(seconds=value * multipliers[unit])


def last_settled_open(now: datetime, timeframe: str) -> datetime:
    now = utc(now, "now")
    step = timeframe_delta(timeframe)
    epoch_seconds = int(now.timestamp())
    boundary = epoch_seconds - epoch_seconds % int(step.total_seconds())
    return datetime.fromtimestamp(boundary, UTC) - step


@dataclass(frozen=True)
class MarketCalendar:
    """Minimal default calendar; verified holiday sets are injected by deployment."""

    market: Market
    holidays: frozenset[str] = frozenset()

    def is_expected(self, timestamp: datetime, timeframe: str) -> bool:
        timestamp = utc(timestamp, "timestamp")
        if self.market is Market.CRYPTO:
            return True
        if timestamp.weekday() >= 5:
            return False
        if timestamp.date().isoformat() in self.holidays:
            return False
        if self.market is Market.FOREX:
            return True
        if timeframe == "1d":
            return True
        return time(3, 45) <= timestamp.time() < time(10)

    def expected_opens(
        self, start: datetime, end: datetime, timeframe: str
    ) -> tuple[datetime, ...]:
        start = utc(start, "start")
        end = utc(end, "end")
        step = timeframe_delta(timeframe)
        result = []
        cursor = start
        while cursor <= end:
            if self.is_expected(cursor, timeframe):
                result.append(cursor)
            cursor += step
        return tuple(result)
