from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nanodelta.contracts import AdvisoryAction, Market, Provider
from nanodelta.decisions import Decision, DecisionStage, DecisionStatus, SignalCandidate
from nanodelta.decisions_postgres import PostgresDecisionLedger
from nanodelta.markets.nse_session import NseSessionState, nse_equity_session
from nanodelta.runtime.realtime_config import _bar_timeframes
from nanodelta.runtime.universe import ConfiguredInstrument, publish_configured_universe


class Cursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((query, params))


class Connection:
    def __init__(self) -> None:
        self.cursor_value = Cursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> Cursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def test_nse_session_uses_ist_normal_market_and_configured_holidays() -> None:
    environment = {
        "NANODELTA_NSE_HOLIDAY_CALENDAR_YEAR": "2026",
        "NANODELTA_NSE_HOLIDAYS": "2026-08-19",
    }
    open_snapshot = nse_equity_session(datetime(2026, 8, 17, 4, 0, tzinfo=UTC), environment)
    holiday = nse_equity_session(datetime(2026, 8, 19, 4, 0, tzinfo=UTC), environment)

    assert open_snapshot.state is NseSessionState.OPEN
    assert open_snapshot.reason == "NORMAL_MARKET_SESSION"
    assert open_snapshot.normal_open == "09:15"
    assert open_snapshot.normal_close == "15:30"
    assert open_snapshot.holiday_calendar_complete is True
    assert holiday.state is NseSessionState.CLOSED
    assert holiday.reason == "CONFIGURED_TRADING_HOLIDAY"


def test_nse_session_reports_calendar_provenance_without_fabricating_completeness() -> None:
    snapshot = nse_equity_session(datetime(2026, 8, 17, 4, 0, tzinfo=UTC), {})
    assert snapshot.state is NseSessionState.OPEN
    assert snapshot.holiday_calendar_complete is False
    assert snapshot.holiday_calendar_year is None


def test_nse_bar_timeframes_require_1m_and_reject_unknown_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NANODELTA_NSE_REALTIME_TIMEFRAMES", "1m,5m,15m")
    assert _bar_timeframes("NANODELTA_NSE_REALTIME_TIMEFRAMES", "1m") == {
        "1m": 60,
        "5m": 300,
        "15m": 900,
    }
    monkeypatch.setenv("NANODELTA_NSE_REALTIME_TIMEFRAMES", "5m,15m")
    with pytest.raises(RuntimeError, match="must include 1m"):
        _bar_timeframes("NANODELTA_NSE_REALTIME_TIMEFRAMES", "1m")


def test_configured_universe_is_reconciled_durably() -> None:
    connection = Connection()
    configured_at = datetime(2026, 8, 17, 3, 30, tzinfo=UTC)
    publish_configured_universe(
        lambda: connection,
        Market.NSE,
        (
            ConfiguredInstrument(
                Market.NSE,
                "RELIANCE",
                Provider.DHAN,
                "1333",
                ("1m", "5m", "15m"),
                "intraday",
            ),
        ),
        configured_at=configured_at,
    )

    assert "UPDATE control.market_universe SET enabled=false" in connection.cursor_value.calls[0][0]
    insert, params = connection.cursor_value.calls[1]
    assert "INSERT INTO control.market_universe" in insert
    assert params[0:6] == ("nse", "RELIANCE", "dhan", "1333", '["1m","5m","15m"]', "intraday")
    assert connection.commits == 1
    assert connection.closed is True


def test_postgres_candidate_and_signal_stage_commit_atomically() -> None:
    connection = Connection()
    at = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
    candidate = SignalCandidate(
        "candidate-1",
        "cycle-1",
        Market.NSE,
        "RELIANCE",
        "15m",
        "strategy-1",
        "approval-1",
        at,
        AdvisoryAction.BUY,
        100,
        98,
        104,
        0.8,
        ("gold-1",),
    )
    decision = Decision.create(
        cycle_id="cycle-1",
        market=Market.NSE,
        symbol="RELIANCE",
        timeframe="15m",
        stage=DecisionStage.SIGNAL,
        status=DecisionStatus.PASSED,
        reason_code="SIGNAL_GENERATED",
        occurred_at=at,
        candidate_id="candidate-1",
        strategy_key="strategy-1",
        detail="BUY",
    )

    PostgresDecisionLedger(lambda: connection).append_candidate(candidate, decision)

    queries = [query for query, _ in connection.cursor_value.calls]
    assert "INSERT INTO control.signal_candidates" in queries[0]
    assert "INSERT INTO control.decision_events" in queries[1]
    assert connection.commits == 1
    assert connection.rollbacks == 0
