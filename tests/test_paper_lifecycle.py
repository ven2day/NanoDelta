from datetime import UTC, datetime

import pytest

from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.decisions import DecisionStage, InMemoryDecisionLedger
from nanodelta.orchestration import PortfolioAllocation
from nanodelta.orchestration.decision_pipeline import ScoreBreakdown, ScoredCandidate
from nanodelta.paper import (
    ExecutionPolicy,
    PaperExecutionEngine,
    PositionState,
)
from nanodelta.paper.lifecycle import ExitReason, MemoryLifecycleStore, PaperPositionLifecycle
from nanodelta.risk import (
    PortfolioPosition,
    PortfolioSnapshot,
    RiskEngine,
    RiskLimits,
    TradeIntent,
)
from nanodelta.strategies import (
    DeterministicCandidate,
    RegimeEvidence,
    StrategyIdentity,
    StrategySignal,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
IDENTITY = StrategyIdentity(Market.NSE, "test_momentum", "1", "1m", "intraday", 1)
LIMITS = RiskLimits(100_000, 100_000, 200_000, 200_000, 10_000, 10)


def test_target_closes_position_records_outcome_and_never_reopens() -> None:
    execution = PaperExecutionEngine(ExecutionPolicy(0, 0))
    opening_intent = TradeIntent(
        "entry-intent",
        Market.NSE,
        "paper-test",
        "RELIANCE",
        AdvisoryAction.BUY,
        10,
        100,
        NOW,
        "candidate-1",
        "approval-test-only",
        IDENTITY,
        ("gold-entry",),
    )
    entry_portfolio = PortfolioSnapshot("empty", "paper-test", 100_000, 0, (), NOW)
    opening = RiskEngine(LIMITS).evaluate_exit(  # cannot use exit authority to open
        opening_intent, entry_portfolio, evaluated_at=NOW
    )
    assert not opening.approved

    # Explicitly create the already-approved entry receipt; admission/risk is tested separately.
    from nanodelta.risk import RiskDecision, RiskDecisionState

    approved_entry = RiskDecision(
        "entry-risk",
        opening_intent,
        "empty",
        RiskDecisionState.APPROVED,
        (),
        NOW,
        LIMITS,
    )
    receipt = execution.execute(approved_entry, idempotency_key="entry", executed_at=NOW)
    signal = StrategySignal(AdvisoryAction.BUY, 0.9, 100, 95, 110)
    candidate = DeterministicCandidate(
        "candidate-1",
        IDENTITY,
        "approval-test-only",
        "RELIANCE",
        None,
        NOW,
        ("gold-entry",),
        signal,
        RegimeEvidence(),
    )
    scored = ScoredCandidate(candidate, ScoreBreakdown(0.9, 1, 1, 1, 1, 0, 0, 0, 0.9))
    allocation = PortfolioAllocation(scored, 10, 100, 95, 110)
    store = MemoryLifecycleStore()
    ledger = InMemoryDecisionLedger()
    lifecycle = PaperPositionLifecycle(
        store=store, execution=execution, risk=RiskEngine(LIMITS), ledger=ledger
    )
    lifecycle.register((allocation,), (receipt,))
    open_portfolio = PortfolioSnapshot(
        "open",
        "paper-test",
        100_000,
        0,
        (PortfolioPosition(Market.NSE, "paper-test", "RELIANCE", 10, 110),),
        NOW,
    )

    outcomes = lifecycle.manage(
        market=Market.NSE,
        account_id="paper-test",
        marks={"RELIANCE": 110},
        portfolio=open_portfolio,
        gold_snapshot_ids={"RELIANCE": "gold-exit"},
        evaluated_at=NOW,
    )

    assert len(outcomes) == 1
    assert outcomes[0].net_pnl == pytest.approx(100)
    assert outcomes[0].gold_snapshot_ids == ("gold-entry", "gold-exit")
    assert store.reasons[receipt.position.position_id] is ExitReason.TARGET
    position = execution.position(Market.NSE, "paper-test", "RELIANCE")
    assert position is not None and position.state is PositionState.CLOSED
    recorded = next(iter(ledger._decisions.values()))
    assert {item.stage for item in ledger.for_cycle(recorded.cycle_id)} == {
        DecisionStage.POSITION_MANAGEMENT
    }
    assert (
        lifecycle.manage(
            market=Market.NSE,
            account_id="paper-test",
            marks={"RELIANCE": 120},
            portfolio=open_portfolio,
            gold_snapshot_ids={"RELIANCE": "gold-later"},
            evaluated_at=NOW,
        )
        == ()
    )


def test_stop_and_target_direction_is_symmetric() -> None:
    from nanodelta.paper.lifecycle import ExitPlan

    buy = ExitPlan(
        "p1",
        Market.NSE,
        "a",
        "X",
        AdvisoryAction.BUY,
        1,
        95,
        110,
        100,
        "c",
        "a",
        IDENTITY,
        ("g",),
        NOW,
    )
    sell = ExitPlan(
        "p2",
        Market.NSE,
        "a",
        "X",
        AdvisoryAction.SELL,
        1,
        105,
        90,
        100,
        "c",
        "a",
        IDENTITY,
        ("g",),
        NOW,
    )
    assert PaperPositionLifecycle._trigger(buy, 94) is ExitReason.STOP
    assert PaperPositionLifecycle._trigger(buy, 111) is ExitReason.TARGET
    assert PaperPositionLifecycle._trigger(sell, 106) is ExitReason.STOP
    assert PaperPositionLifecycle._trigger(sell, 89) is ExitReason.TARGET
