"""NSE normal equity-session status with explicit holiday-calendar provenance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
NORMAL_OPEN = time(9, 15)
NORMAL_CLOSE = time(15, 30)


class NseSessionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class NseSessionSnapshot:
    state: NseSessionState
    as_of: datetime
    local_date: date
    normal_open: str
    normal_close: str
    reason: str
    holiday_calendar_year: int | None
    holiday_calendar_complete: bool


def _holidays(environ: Mapping[str, str]) -> frozenset[date]:
    raw = environ.get("NANODELTA_NSE_HOLIDAYS", "")
    try:
        return frozenset(
            date.fromisoformat(value.strip()) for value in raw.split(",") if value.strip()
        )
    except ValueError as exc:
        raise RuntimeError("NANODELTA_NSE_HOLIDAYS must contain comma-separated ISO dates") from exc


def nse_equity_session(
    at: datetime,
    environ: Mapping[str, str],
) -> NseSessionSnapshot:
    if at.tzinfo is None:
        raise ValueError("session timestamp must be timezone-aware")
    local = at.astimezone(IST)
    raw_year = environ.get("NANODELTA_NSE_HOLIDAY_CALENDAR_YEAR", "").strip()
    try:
        calendar_year = int(raw_year) if raw_year else None
    except ValueError as exc:
        raise RuntimeError("NANODELTA_NSE_HOLIDAY_CALENDAR_YEAR must be a year") from exc
    holidays = _holidays(environ)
    complete = calendar_year == local.year and bool(holidays)
    if local.weekday() >= 5:
        state, reason = NseSessionState.CLOSED, "WEEKEND"
    elif local.date() in holidays:
        state, reason = NseSessionState.CLOSED, "CONFIGURED_TRADING_HOLIDAY"
    elif local.time() < NORMAL_OPEN:
        state, reason = NseSessionState.CLOSED, "BEFORE_NORMAL_MARKET"
    elif local.time() >= NORMAL_CLOSE:
        state, reason = NseSessionState.CLOSED, "AFTER_NORMAL_MARKET"
    else:
        state, reason = NseSessionState.OPEN, "NORMAL_MARKET_SESSION"
    return NseSessionSnapshot(
        state,
        at.astimezone(UTC),
        local.date(),
        NORMAL_OPEN.isoformat(timespec="minutes"),
        NORMAL_CLOSE.isoformat(timespec="minutes"),
        reason,
        calendar_year,
        complete,
    )
