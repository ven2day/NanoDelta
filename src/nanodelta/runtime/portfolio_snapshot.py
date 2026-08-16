"""Builds a live nanodelta.risk.PortfolioSnapshot from durable paper-execution
state, so RiskEngine evaluates trade intents against the account's real open
positions and today's real realized P&L instead of an empty placeholder.

Mark prices for open positions come from the caller, not this module: a
snapshot's job is to report state, not to decide what "current price" means
for staleness/freshness purposes (that's the realtime feed's concern, wired in
Phase 4). A position with no supplied mark price is a real gap in the caller's
data, not something to silently paper over with a stale or missing exposure
figure -- silently omitting an open position from a risk snapshot could let
its exposure bypass every gross-exposure check in RiskEngine.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

from nanodelta.contracts import Market, stable_id
from nanodelta.persistence.migrations import Connection
from nanodelta.risk import PortfolioPosition, PortfolioSnapshot


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def build_portfolio_snapshot(
    connection: Connection,
    *,
    market: Market,
    account_id: str,
    equity: float,
    mark_prices: Mapping[str, float],
    now: datetime,
) -> PortfolioSnapshot:
    positions = _load_open_positions(connection, market, account_id, mark_prices)
    realized_pnl_today = _load_realized_pnl_today(connection, market, account_id, now)
    return PortfolioSnapshot(
        stable_id("portfolio-snapshot", market.value, account_id, now.isoformat()),
        account_id,
        equity,
        realized_pnl_today,
        positions,
        now,
    )


def _load_open_positions(
    connection: Connection,
    market: Market,
    account_id: str,
    mark_prices: Mapping[str, float],
) -> tuple[PortfolioPosition, ...]:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT symbol,signed_quantity FROM paper.positions "
        "WHERE market=%s AND account_id=%s AND state='OPEN'",
        (market.value, account_id),
    )
    positions = []
    for row in cursor.fetchall():
        symbol = str(row[0])
        mark_price = mark_prices.get(symbol)
        if mark_price is None:
            raise RuntimeError(
                f"no mark price supplied for open position {market.value}:{symbol}; "
                "refusing to build a risk snapshot that silently omits its exposure"
            )
        positions.append(
            PortfolioPosition(market, account_id, symbol, float(cast(float, row[1])), mark_price)
        )
    return tuple(positions)


def _load_realized_pnl_today(
    connection: Connection, market: Market, account_id: str, now: datetime
) -> float:
    start, end = _day_bounds(now)
    cursor = connection.cursor()
    cursor.execute(
        "SELECT COALESCE(SUM(net_pnl), 0) FROM paper.outcomes "
        "WHERE market=%s AND account_id=%s AND closed_at>=%s AND closed_at<%s",
        (market.value, account_id, start, end),
    )
    row = cursor.fetchone()
    return float(cast(float, row[0])) if row is not None else 0.0
