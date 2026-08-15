from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from nanodelta.contracts import Market
from nanodelta.outcomes import (
    LearningDisposition,
    OfflineLearner,
    Outcome,
    OutcomeRecorder,
)
from nanodelta.paper import PaperPosition, PositionState
from nanodelta.strategies import StrategyIdentity

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
IDENTITY = StrategyIdentity(Market.CRYPTO, "momentum", "1.0.0", "15m", "1h", 1)


def closed_position() -> PaperPosition:
    return PaperPosition(
        "position-1",
        Market.CRYPTO,
        "paper-crypto",
        "BTC_USDT",
        0,
        0,
        125,
        5,
        NOW,
        NOW + timedelta(hours=2),
        NOW + timedelta(hours=2),
        PositionState.CLOSED,
        ("decision-open", "decision-close"),
        (IDENTITY.key,),
        ("approval-1",),
        ("gold-open", "gold-close"),
        ("agent-1",),
    )


def outcome(number: int, net_return: float) -> Outcome:
    net = net_return * 1_000
    return Outcome(
        f"outcome-{number}",
        f"position-{number}",
        Market.CRYPTO,
        "paper-crypto",
        "BTC_USDT",
        NOW,
        NOW + timedelta(hours=number),
        net,
        0,
        net,
        net_return,
        (f"decision-{number}",),
        ("approval-1",),
        (f"gold-{number}",),
        (),
        IDENTITY,
        NOW + timedelta(hours=number),
    )


def test_outcome_is_idempotent_and_preserves_full_lineage() -> None:
    recorder = OutcomeRecorder()
    position = closed_position()

    first = recorder.record(
        position, strategy_identity=IDENTITY, allocated_capital=1_000, recorded_at=NOW
    )
    second = recorder.record(
        position, strategy_identity=IDENTITY, allocated_capital=1_000, recorded_at=NOW
    )

    assert second is first
    assert first.gross_pnl == 125
    assert first.net_pnl == 120
    assert first.return_on_allocated_capital == pytest.approx(0.12)
    assert first.gold_snapshot_ids == ("gold-open", "gold-close")

    with pytest.raises(ValueError, match="different inputs"):
        recorder.record(
            position,
            strategy_identity=IDENTITY,
            allocated_capital=9_999,
            recorded_at=NOW,
        )


def test_open_position_cannot_produce_outcome() -> None:
    open_position = replace(
        closed_position(),
        signed_quantity=1,
        average_entry_price=100,
        closed_at=None,
        state=PositionState.OPEN,
    )
    with pytest.raises(ValueError, match="closed positions"):
        OutcomeRecorder().record(
            open_position,
            strategy_identity=IDENTITY,
            allocated_capital=1_000,
            recorded_at=NOW,
        )


def test_learning_is_review_evidence_with_no_execution_action() -> None:
    learner = OfflineLearner(minimum_sample_size=3)
    positive = learner.assess(
        IDENTITY,
        (outcome(1, 0.02), outcome(2, 0.01), outcome(3, -0.005)),
        generated_at=NOW,
    )
    negative = learner.assess(
        IDENTITY,
        (outcome(1, -0.02), outcome(2, -0.01), outcome(3, -0.03)),
        generated_at=NOW,
    )

    assert positive.disposition is LearningDisposition.RETAIN
    assert positive.win_rate == pytest.approx(2 / 3)
    assert negative.disposition is LearningDisposition.SUSPENSION_REVIEW
    assert not hasattr(negative, "action")
    assert not hasattr(learner, "execute")


def test_learning_rejects_mixed_strategy_identity() -> None:
    other = StrategyIdentity(Market.CRYPTO, "other", "1", "15m", "1h", 1)
    mixed = replace(outcome(1, 0.01), strategy_identity=other)
    with pytest.raises(ValueError, match="exact strategy identity"):
        OfflineLearner().assess(IDENTITY, (mixed,), generated_at=NOW)
