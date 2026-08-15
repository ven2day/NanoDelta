"""Closed-position attribution and bounded offline learning summaries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nanodelta.contracts import Market, stable_id, utc
from nanodelta.paper.execution import PaperPosition, PositionState
from nanodelta.strategies.registry import StrategyIdentity


@dataclass(frozen=True)
class Outcome:
    outcome_id: str
    position_id: str
    market: Market
    account_id: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    gross_pnl: float
    total_fees: float
    net_pnl: float
    return_on_allocated_capital: float
    decision_ids: tuple[str, ...]
    approval_ids: tuple[str, ...]
    gold_snapshot_ids: tuple[str, ...]
    agent_evidence_ids: tuple[str, ...]
    strategy_identity: StrategyIdentity
    recorded_at: datetime


class OutcomeRecorder:
    def __init__(self) -> None:
        self._by_position: dict[str, Outcome] = {}

    def record(
        self,
        position: PaperPosition,
        *,
        strategy_identity: StrategyIdentity,
        allocated_capital: float,
        recorded_at: datetime,
    ) -> Outcome:
        if not math.isfinite(allocated_capital) or allocated_capital <= 0:
            raise ValueError("allocated_capital must be finite and positive")
        existing = self._by_position.get(position.position_id)
        if existing is not None:
            expected_return = (position.realized_pnl - position.total_fees) / allocated_capital
            if existing.strategy_identity != strategy_identity or not math.isclose(
                existing.return_on_allocated_capital, expected_return, rel_tol=1e-12
            ):
                raise ValueError("position outcome is already bound to different inputs")
            return existing
        if position.state is not PositionState.CLOSED or position.closed_at is None:
            raise ValueError("outcomes can be recorded only for closed positions")
        if position.market is not strategy_identity.market or position.strategy_keys != (
            strategy_identity.key,
        ):
            raise ValueError("outcome requires one matching exact strategy identity")
        recorded_at = utc(recorded_at, "recorded_at")
        net = position.realized_pnl - position.total_fees
        outcome = Outcome(
            stable_id("outcome", position.position_id),
            position.position_id,
            position.market,
            position.account_id,
            position.symbol,
            position.opened_at,
            position.closed_at,
            position.realized_pnl,
            position.total_fees,
            net,
            net / allocated_capital,
            position.decision_ids,
            position.approval_ids,
            position.gold_snapshot_ids,
            position.agent_evidence_ids,
            strategy_identity,
            recorded_at,
        )
        self._by_position[position.position_id] = outcome
        return outcome


class LearningDisposition(StrEnum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    RETAIN = "RETAIN"
    REVIEW = "REVIEW"
    SUSPENSION_REVIEW = "SUSPENSION_REVIEW"


@dataclass(frozen=True)
class LearningAssessment:
    assessment_id: str
    identity: StrategyIdentity
    outcome_ids: tuple[str, ...]
    sample_size: int
    win_rate: float
    average_net_return: float
    cumulative_net_pnl: float
    disposition: LearningDisposition
    generated_at: datetime
    policy_version: str


class OfflineLearner:
    """Produces review evidence only; it has no registry, risk, or execution dependency."""

    def __init__(
        self,
        *,
        minimum_sample_size: int = 30,
        review_average_return: float = 0.0,
        suspension_average_return: float = -0.01,
        policy_version: str = "1",
    ) -> None:
        if minimum_sample_size < 1:
            raise ValueError("minimum_sample_size must be positive")
        if suspension_average_return > review_average_return:
            raise ValueError("suspension threshold must not exceed review threshold")
        self._minimum_sample_size = minimum_sample_size
        self._review_average_return = review_average_return
        self._suspension_average_return = suspension_average_return
        self._policy_version = policy_version

    def assess(
        self,
        identity: StrategyIdentity,
        outcomes: tuple[Outcome, ...],
        *,
        generated_at: datetime,
    ) -> LearningAssessment:
        if any(outcome.strategy_identity != identity for outcome in outcomes):
            raise ValueError("learning inputs must share the exact strategy identity")
        ordered = tuple(sorted(outcomes, key=lambda item: (item.closed_at, item.outcome_id)))
        sample = len(ordered)
        win_rate = sum(outcome.net_pnl > 0 for outcome in ordered) / sample if sample else 0.0
        average_return = (
            sum(outcome.return_on_allocated_capital for outcome in ordered) / sample
            if sample
            else 0.0
        )
        cumulative = sum(outcome.net_pnl for outcome in ordered)
        if sample < self._minimum_sample_size:
            disposition = LearningDisposition.INSUFFICIENT_DATA
        elif average_return <= self._suspension_average_return:
            disposition = LearningDisposition.SUSPENSION_REVIEW
        elif average_return <= self._review_average_return:
            disposition = LearningDisposition.REVIEW
        else:
            disposition = LearningDisposition.RETAIN
        generated_at = utc(generated_at, "generated_at")
        outcome_ids = tuple(outcome.outcome_id for outcome in ordered)
        return LearningAssessment(
            stable_id(identity.key, *outcome_ids, self._policy_version),
            identity,
            outcome_ids,
            sample,
            win_rate,
            average_return,
            cumulative,
            disposition,
            generated_at,
            self._policy_version,
        )
