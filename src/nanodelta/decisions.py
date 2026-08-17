"""Append-only, queryable stage decisions for every evaluated symbol grain."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nanodelta.contracts import AdvisoryAction, Market, stable_id, utc


class DecisionStage(StrEnum):
    GLOBAL = "global"
    POSITION_MANAGEMENT = "position_management"
    DATA_READINESS = "data_readiness"
    TRADEABILITY = "tradeability"
    STRATEGY_ELIGIBILITY = "strategy_eligibility"
    SIGNAL = "signal"
    SCORING = "scoring"
    SIGNAL_QUALITY = "signal_quality"
    LLM_REVIEW = "llm_review"
    PORTFOLIO_CONSTRUCTION = "portfolio_construction"
    ENTRY_REVALIDATION = "entry_revalidation"
    RISK = "risk"
    EXECUTION = "execution"


class DecisionStatus(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    ORDERED = "ordered"
    ERROR = "error"


@dataclass(frozen=True)
class Decision:
    decision_id: str
    cycle_id: str
    market: Market
    symbol: str
    timeframe: str | None
    stage: DecisionStage
    status: DecisionStatus
    reason_code: str
    occurred_at: datetime
    candidate_id: str | None = None
    strategy_key: str | None = None
    detail: str = ""
    metrics: tuple[tuple[str, float], ...] = field(default_factory=tuple)

    @classmethod
    def create(
        cls,
        *,
        cycle_id: str,
        market: Market,
        symbol: str,
        timeframe: str | None,
        stage: DecisionStage,
        status: DecisionStatus,
        reason_code: str,
        occurred_at: datetime,
        candidate_id: str | None = None,
        strategy_key: str | None = None,
        detail: str = "",
        metrics: tuple[tuple[str, float], ...] = (),
    ) -> Decision:
        if not cycle_id or not symbol or not reason_code:
            raise ValueError("cycle_id, symbol, and reason_code are required")
        if len({name for name, _ in metrics}) != len(metrics):
            raise ValueError("decision metric names must be unique")
        occurred_at = utc(occurred_at, "occurred_at")
        decision_id = stable_id(
            cycle_id,
            market.value,
            symbol,
            timeframe,
            stage.value,
            status.value,
            reason_code,
            candidate_id,
            strategy_key,
        )
        return cls(
            decision_id,
            cycle_id,
            market,
            symbol,
            timeframe,
            stage,
            status,
            reason_code,
            occurred_at,
            candidate_id,
            strategy_key,
            detail,
            metrics,
        )


@dataclass(frozen=True)
class SignalCandidate:
    """Immutable BUY/SELL evidence captured before later pipeline rejection."""

    candidate_id: str
    cycle_id: str
    market: Market
    symbol: str
    timeframe: str
    strategy_key: str
    approval_id: str
    event_time: datetime
    action: AdvisoryAction
    reference_price: float
    stop_price: float
    target_price: float
    confidence: float
    gold_snapshot_ids: tuple[str, ...]
    evidence: tuple[tuple[str, float], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        required = (
            self.candidate_id,
            self.cycle_id,
            self.symbol,
            self.timeframe,
            self.strategy_key,
            self.approval_id,
        )
        if any(not value for value in required) or not self.gold_snapshot_ids:
            raise ValueError("signal candidate identity and Gold lineage are required")
        if self.action is AdvisoryAction.ABSTAIN:
            raise ValueError("signal candidate action must be BUY or SELL")
        numeric = (
            self.reference_price,
            self.stop_price,
            self.target_price,
            self.confidence,
            *(value for _, value in self.evidence),
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("signal candidate evidence must be finite")
        if self.reference_price <= 0 or not 0 <= self.confidence <= 1:
            raise ValueError("signal candidate price or confidence is invalid")
        if len({name for name, _ in self.evidence}) != len(self.evidence):
            raise ValueError("signal candidate evidence names must be unique")
        utc(self.event_time, "event_time")


class DecisionLedger(Protocol):
    def append_candidate(self, candidate: SignalCandidate, decision: Decision) -> None: ...

    def append(self, decision: Decision) -> None: ...

    def for_cycle(self, cycle_id: str) -> tuple[Decision, ...]: ...


class InMemoryDecisionLedger:
    """Idempotent test/runtime ledger; PostgreSQL is the durable deployment adapter."""

    def __init__(self) -> None:
        self._decisions: dict[str, Decision] = {}
        self.candidates: dict[str, SignalCandidate] = {}

    def append_candidate(self, candidate: SignalCandidate, decision: Decision) -> None:
        existing = self.candidates.get(candidate.candidate_id)
        if existing is not None and existing != candidate:
            raise ValueError("signal candidate identity is immutable")
        if (
            decision.candidate_id != candidate.candidate_id
            or decision.cycle_id != candidate.cycle_id
        ):
            raise ValueError("signal decision does not match candidate identity")
        self.candidates[candidate.candidate_id] = candidate
        self.append(decision)

    def append(self, decision: Decision) -> None:
        existing = self._decisions.get(decision.decision_id)
        if existing is not None and existing != decision:
            raise ValueError("decision identity is immutable")
        self._decisions[decision.decision_id] = decision

    def for_cycle(self, cycle_id: str) -> tuple[Decision, ...]:
        return tuple(
            decision for decision in self._decisions.values() if decision.cycle_id == cycle_id
        )
