from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nanodelta.contracts import Market
from nanodelta.risk import PortfolioPosition
from nanodelta.runtime.portfolio_snapshot import build_portfolio_snapshot

NOW = datetime(2026, 8, 15, 12, 30, tzinfo=UTC)


class FakeCursor:
    def __init__(self, positions: list[tuple[str, float]], outcomes_sum: float | None) -> None:
        self.positions = positions
        self.outcomes_sum = outcomes_sum
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._all: list[tuple[object, ...]] = []
        self._one: tuple[object, ...] | None = None

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((query, params))
        if query.startswith("SELECT symbol,signed_quantity"):
            self._all = list(self.positions)
        elif query.startswith("SELECT COALESCE(SUM"):
            self._one = (self.outcomes_sum if self.outcomes_sum is not None else 0.0,)

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._all

    def fetchone(self) -> tuple[object, ...] | None:
        return self._one


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor


def test_snapshot_includes_only_open_positions_with_supplied_mark_prices() -> None:
    cursor = FakeCursor(positions=[("RELIANCE", 10.0), ("TCS", -5.0)], outcomes_sum=0.0)
    snapshot = build_portfolio_snapshot(
        FakeConnection(cursor),
        market=Market.NSE,
        account_id="paper-1",
        equity=3_000_000,
        mark_prices={"RELIANCE": 2900.0, "TCS": 4000.0},
        now=NOW,
    )

    assert snapshot.account_id == "paper-1"
    assert snapshot.equity == 3_000_000
    assert snapshot.captured_at == NOW
    assert snapshot.positions == (
        PortfolioPosition(Market.NSE, "paper-1", "RELIANCE", 10.0, 2900.0),
        PortfolioPosition(Market.NSE, "paper-1", "TCS", -5.0, 4000.0),
    )
    # Only OPEN positions are ever selected -- confirm the query filters for it.
    assert "state='OPEN'" in cursor.calls[0][0]


def test_snapshot_raises_when_an_open_position_has_no_mark_price() -> None:
    cursor = FakeCursor(positions=[("RELIANCE", 10.0)], outcomes_sum=0.0)
    with pytest.raises(RuntimeError, match="no mark price supplied for open position"):
        build_portfolio_snapshot(
            FakeConnection(cursor),
            market=Market.NSE,
            account_id="paper-1",
            equity=3_000_000,
            mark_prices={},
            now=NOW,
        )


def test_snapshot_sums_todays_realized_pnl_from_durable_realization_events() -> None:
    cursor = FakeCursor(positions=[], outcomes_sum=4250.75)
    snapshot = build_portfolio_snapshot(
        FakeConnection(cursor),
        market=Market.NSE,
        account_id="paper-1",
        equity=3_000_000,
        mark_prices={},
        now=NOW,
    )

    assert snapshot.realized_pnl_today == pytest.approx(4250.75)
    query, params = cursor.calls[1]
    assert "paper.realization_events" in query
    assert params[0] == "nse"
    assert params[1] == "paper-1"
    start, end = params[2], params[3]
    assert start == datetime(2026, 8, 15, tzinfo=UTC)
    assert end == datetime(2026, 8, 16, tzinfo=UTC)


def test_snapshot_reports_zero_realized_pnl_with_no_outcomes_today() -> None:
    cursor = FakeCursor(positions=[], outcomes_sum=None)
    snapshot = build_portfolio_snapshot(
        FakeConnection(cursor),
        market=Market.NSE,
        account_id="paper-1",
        equity=3_000_000,
        mark_prices={},
        now=NOW,
    )

    assert snapshot.realized_pnl_today == 0.0


def test_snapshot_id_is_deterministic_for_the_same_inputs() -> None:
    cursor = lambda: FakeCursor(positions=[], outcomes_sum=0.0)  # noqa: E731
    first = build_portfolio_snapshot(
        FakeConnection(cursor()),
        market=Market.NSE,
        account_id="paper-1",
        equity=3_000_000,
        mark_prices={},
        now=NOW,
    )
    second = build_portfolio_snapshot(
        FakeConnection(cursor()),
        market=Market.NSE,
        account_id="paper-1",
        equity=3_000_000,
        mark_prices={},
        now=NOW,
    )
    assert first.snapshot_id == second.snapshot_id


@pytest.mark.parametrize("mark", [float("nan"), float("inf"), 0.0, -1.0])
def test_snapshot_rejects_invalid_mark_prices(mark: float) -> None:
    cursor = FakeCursor(positions=[("RELIANCE", 10.0)], outcomes_sum=0.0)
    with pytest.raises(ValueError, match="must be finite and positive"):
        build_portfolio_snapshot(
            FakeConnection(cursor),
            market=Market.NSE,
            account_id="paper-1",
            equity=3_000_000,
            mark_prices={"RELIANCE": mark},
            now=NOW,
        )


def test_snapshot_id_changes_when_portfolio_contents_change() -> None:
    first = build_portfolio_snapshot(
        FakeConnection(FakeCursor(positions=[("RELIANCE", 10.0)], outcomes_sum=0.0)),
        market=Market.NSE,
        account_id="paper-1",
        equity=3_000_000,
        mark_prices={"RELIANCE": 100.0},
        now=NOW,
    )
    changed = build_portfolio_snapshot(
        FakeConnection(FakeCursor(positions=[("RELIANCE", 10.0)], outcomes_sum=-500.0)),
        market=Market.NSE,
        account_id="paper-1",
        equity=3_000_000,
        mark_prices={"RELIANCE": 101.0},
        now=NOW,
    )
    assert first.snapshot_id != changed.snapshot_id
