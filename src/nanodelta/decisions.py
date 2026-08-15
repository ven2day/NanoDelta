"""Append-only, queryable stage decisions for every evaluated symbol grain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nanodelta.contracts import Market, stable_id, utc


class DecisionStage(StrEnum):
    GLOBAL = "global"
    POSITION_MANAGEMENT = "position_management"
    DATA_READINESS = "data_readiness"
    TRADEABILITY = "tradeability"
    STRATEGY_ELIGIBILITY = "strategy_eligibility"
    SIGNAL = "signal"
    SCORING = "scoring"
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


class DecisionLedger(Protocol):
    def append(self, decision: Decision) -> None: ...

    def for_cycle(self, cycle_id: str) -> tuple[Decision, ...]: ...


class InMemoryDecisionLedger:
    """Idempotent test/runtime ledger; PostgreSQL is the durable deployment adapter."""

    def __init__(self) -> None:
        self._decisions: dict[str, Decision] = {}

    def append(self, decision: Decision) -> None:
        existing = self._decisions.get(decision.decision_id)
        if existing is not None and existing != decision:
            raise ValueError("decision identity is immutable")
        self._decisions[decision.decision_id] = decision

    def for_cycle(self, cycle_id: str) -> tuple[Decision, ...]:
        return tuple(
            decision for decision in self._decisions.values() if decision.cycle_id == cycle_id
        )
