from datetime import UTC, datetime, timedelta

import pytest

from nanodelta.agents import AdvisoryAction
from nanodelta.contracts import Market
from nanodelta.paper import ExecutionPolicy, PaperExecutionEngine, PositionState
from nanodelta.risk import (
    PortfolioPosition,
    PortfolioSnapshot,
    RiskDecisionState,
    RiskEngine,
    RiskLimits,
    TradeIntent,
)
from nanodelta.strategies import StrategyApproval, StrategyIdentity

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
IDENTITY = StrategyIdentity(Market.NSE, "vwap_pullback", "1.0.0", "5m", "30m", 1)


def approval() -> StrategyApproval:
    return StrategyApproval.create(
        identity=IDENTITY,
        validation_run_id="validation-1",
        approved_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=30),
        approved_by="committee",
        reason="passed",
    )


def intent(
    action: AdvisoryAction = AdvisoryAction.BUY,
    *,
    quantity: float = 10,
    reference_price: float = 100,
    suffix: str = "1",
) -> TradeIntent:
    artifact = approval()
    return TradeIntent(
        f"intent-{suffix}",
        Market.NSE,
        "paper-1",
        "RELIANCE",
        action,
        quantity,
        reference_price,
        NOW,
        f"candidate-{suffix}",
        artifact.approval_id,
        IDENTITY,
        (f"gold-{suffix}",),
        "agent-1",
    )


def portfolio(*positions: PortfolioPosition, pnl: float = 0) -> PortfolioSnapshot:
    return PortfolioSnapshot("snapshot-1", "paper-1", 100_000, pnl, positions, NOW)


def limits() -> RiskLimits:
    return RiskLimits(10_000, 20_000, 50_000, 80_000, 2_000, 5)


def approved_decision(
    action: AdvisoryAction = AdvisoryAction.BUY,
    *,
    quantity: float = 10,
    reference_price: float = 100,
    suffix: str = "1",
    snapshot: PortfolioSnapshot | None = None,
):
    return RiskEngine(limits()).evaluate(
        intent(action, quantity=quantity, reference_price=reference_price, suffix=suffix),
        approval(),
        snapshot or portfolio(),
        evaluated_at=NOW,
    )


def test_risk_approves_exact_current_strategy_and_bounded_exposure() -> None:
    decision = approved_decision()

    assert decision.state is RiskDecisionState.APPROVED
    assert decision.rejection_reasons == ()


def test_risk_reports_all_deterministic_limit_failures() -> None:
    existing = PortfolioPosition(Market.NSE, "paper-1", "TCS", 10, 100)
    constrained = RiskLimits(500, 500, 1_100, 1_100, 100, 1, max_snapshot_age_seconds=1)
    decision = RiskEngine(constrained).evaluate(
        intent(quantity=10, reference_price=100),
        approval(),
        PortfolioSnapshot(
            "stale", "paper-1", 10_000, -100, (existing,), NOW - timedelta(seconds=5)
        ),
        evaluated_at=NOW,
    )

    assert decision.state is RiskDecisionState.REJECTED
    assert set(decision.rejection_reasons) == {
        "STALE_PORTFOLIO_SNAPSHOT",
        "DAILY_LOSS_LIMIT_REACHED",
        "ORDER_NOTIONAL_LIMIT_EXCEEDED",
        "POSITION_NOTIONAL_LIMIT_EXCEEDED",
        "TOTAL_GROSS_EXPOSURE_LIMIT_EXCEEDED",
        "MARKET_GROSS_EXPOSURE_LIMIT_EXCEEDED",
        "OPEN_POSITION_LIMIT_REACHED",
    }


def test_paper_execution_is_idempotent_and_applies_costs() -> None:
    engine = PaperExecutionEngine(ExecutionPolicy(slippage_bps=10, fee_bps=5))
    decision = approved_decision()

    first = engine.execute(decision, idempotency_key="order-key-1", executed_at=NOW)
    second = engine.execute(decision, idempotency_key="order-key-1", executed_at=NOW)

    assert second is first
    assert first.fill.price == pytest.approx(100.1)
    assert first.fill.fee == pytest.approx(0.5005)
    assert first.position.signed_quantity == 10
    assert first.position.state is PositionState.OPEN


def test_rejected_risk_decision_cannot_enter_paper_execution() -> None:
    rejected = RiskEngine(RiskLimits(1, 1, 1, 1, 1, 1)).evaluate(
        intent(), approval(), portfolio(), evaluated_at=NOW
    )
    with pytest.raises(PermissionError, match="approved risk decision"):
        PaperExecutionEngine(ExecutionPolicy(0, 0)).execute(
            rejected, idempotency_key="blocked", executed_at=NOW
        )


def test_buy_then_sell_closes_position_and_realizes_pnl() -> None:
    engine = PaperExecutionEngine(ExecutionPolicy(0, 0))
    opened = engine.execute(approved_decision(), idempotency_key="open", executed_at=NOW).position
    snapshot = portfolio(
        PortfolioPosition(
            opened.market,
            opened.account_id,
            opened.symbol,
            opened.signed_quantity,
            110,
        )
    )
    close_decision = approved_decision(
        AdvisoryAction.SELL,
        quantity=10,
        reference_price=110,
        suffix="2",
        snapshot=snapshot,
    )
    closed = engine.execute(
        close_decision,
        idempotency_key="close",
        executed_at=NOW + timedelta(minutes=5),
    ).position

    assert closed.state is PositionState.CLOSED
    assert closed.signed_quantity == 0
    assert closed.realized_pnl == pytest.approx(100)
    assert closed.closed_at == NOW + timedelta(minutes=5)
