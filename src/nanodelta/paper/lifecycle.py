"""Deterministic stop/target management and closed-position outcome recording."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nanodelta.contracts import AdvisoryAction, Market, stable_id, utc
from nanodelta.decisions import Decision, DecisionLedger, DecisionStage, DecisionStatus
from nanodelta.orchestration.decision_pipeline import PortfolioAllocation
from nanodelta.outcomes import Outcome, OutcomeRecorder
from nanodelta.paper.execution import ExecutionReceipt, PaperExecutionEngine
from nanodelta.risk import PortfolioSnapshot, RiskEngine, TradeIntent
from nanodelta.strategies import StrategyIdentity


class ExitReason(StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"


@dataclass(frozen=True)
class ExitPlan:
    position_id: str
    market: Market
    account_id: str
    symbol: str
    entry_action: AdvisoryAction
    quantity: float
    stop_price: float
    target_price: float
    allocated_capital: float
    candidate_id: str
    approval_id: str
    identity: StrategyIdentity
    gold_snapshot_ids: tuple[str, ...]
    created_at: datetime


class LifecycleStore(Protocol):
    def save_plan(self, plan: ExitPlan) -> None: ...

    def active(self, market: Market, account_id: str) -> tuple[ExitPlan, ...]: ...

    def save_outcome(self, outcome: Outcome, reason: ExitReason) -> None: ...


class MemoryLifecycleStore:
    def __init__(self) -> None:
        self.plans: dict[str, ExitPlan] = {}
        self.outcomes: dict[str, Outcome] = {}
        self.reasons: dict[str, ExitReason] = {}

    def save_plan(self, plan: ExitPlan) -> None:
        existing = self.plans.get(plan.position_id)
        if existing is not None and existing != plan:
            raise ValueError("position exit plan is immutable")
        self.plans[plan.position_id] = plan

    def active(self, market: Market, account_id: str) -> tuple[ExitPlan, ...]:
        return tuple(
            plan
            for position_id, plan in self.plans.items()
            if plan.market is market
            and plan.account_id == account_id
            and position_id not in self.outcomes
        )

    def save_outcome(self, outcome: Outcome, reason: ExitReason) -> None:
        existing = self.outcomes.get(outcome.position_id)
        if existing is not None and existing != outcome:
            raise ValueError("position outcome is immutable")
        self.outcomes[outcome.position_id] = outcome
        self.reasons[outcome.position_id] = reason


class PaperPositionLifecycle:
    def __init__(
        self,
        *,
        store: LifecycleStore,
        execution: PaperExecutionEngine,
        risk: RiskEngine,
        ledger: DecisionLedger,
    ) -> None:
        self._store = store
        self._execution = execution
        self._risk = risk
        self._ledger = ledger
        self._outcomes = OutcomeRecorder()

    def register(
        self,
        allocations: tuple[PortfolioAllocation, ...],
        receipts: tuple[ExecutionReceipt, ...],
    ) -> None:
        for allocation, receipt in zip(allocations, receipts, strict=True):
            candidate = allocation.candidate.candidate
            self._store.save_plan(
                ExitPlan(
                    receipt.position.position_id,
                    receipt.position.market,
                    receipt.position.account_id,
                    receipt.position.symbol,
                    candidate.signal.action,
                    receipt.fill.quantity,
                    allocation.stop_price,
                    allocation.target_price,
                    receipt.fill.quantity * receipt.fill.price + receipt.fill.fee,
                    candidate.candidate_id,
                    candidate.approval_id,
                    candidate.identity,
                    candidate.gold_snapshot_ids,
                    receipt.fill.filled_at,
                )
            )

    def manage(
        self,
        *,
        market: Market,
        account_id: str,
        marks: dict[str, float],
        portfolio: PortfolioSnapshot,
        gold_snapshot_ids: dict[str, str],
        evaluated_at: datetime,
    ) -> tuple[Outcome, ...]:
        evaluated_at = utc(evaluated_at, "evaluated_at")
        outcomes: list[Outcome] = []
        for plan in self._store.active(market, account_id):
            mark = marks.get(plan.symbol)
            if mark is None:
                continue
            reason = self._trigger(plan, mark)
            if reason is None:
                continue
            action = (
                AdvisoryAction.SELL
                if plan.entry_action is AdvisoryAction.BUY
                else AdvisoryAction.BUY
            )
            lineage = tuple(
                dict.fromkeys((*plan.gold_snapshot_ids, gold_snapshot_ids.get(plan.symbol, "")))
            )
            lineage = tuple(item for item in lineage if item)
            intent = TradeIntent(
                stable_id("protective-exit", plan.position_id, reason.value),
                plan.market,
                plan.account_id,
                plan.symbol,
                action,
                plan.quantity,
                mark,
                evaluated_at,
                plan.candidate_id,
                plan.approval_id,
                plan.identity,
                lineage,
            )
            decision = self._risk.evaluate_exit(intent, portfolio, evaluated_at=evaluated_at)
            if not decision.approved:
                self._record(
                    plan,
                    reason,
                    evaluated_at,
                    DecisionStatus.REJECTED,
                    "EXIT_RISK_REJECTED",
                )
                continue
            receipt = self._execution.execute(
                decision,
                idempotency_key=f"exit:{plan.position_id}:{reason.value}",
                executed_at=evaluated_at,
            )
            outcome = self._outcomes.record(
                receipt.position,
                strategy_identity=plan.identity,
                allocated_capital=plan.allocated_capital,
                recorded_at=evaluated_at,
            )
            self._store.save_outcome(outcome, reason)
            self._record(
                plan,
                reason,
                evaluated_at,
                DecisionStatus.ORDERED,
                f"PAPER_{reason.value}_EXIT",
            )
            outcomes.append(outcome)
        return tuple(outcomes)

    @staticmethod
    def _trigger(plan: ExitPlan, mark: float) -> ExitReason | None:
        if plan.entry_action is AdvisoryAction.BUY:
            if mark <= plan.stop_price:
                return ExitReason.STOP
            if mark >= plan.target_price:
                return ExitReason.TARGET
        else:
            if mark >= plan.stop_price:
                return ExitReason.STOP
            if mark <= plan.target_price:
                return ExitReason.TARGET
        return None

    def _record(
        self,
        plan: ExitPlan,
        reason: ExitReason,
        at: datetime,
        status: DecisionStatus,
        code: str,
    ) -> None:
        self._ledger.append(
            Decision.create(
                cycle_id=stable_id("exit-cycle", plan.position_id, reason.value),
                market=plan.market,
                symbol=plan.symbol,
                timeframe=plan.identity.timeframe,
                stage=DecisionStage.POSITION_MANAGEMENT,
                status=status,
                reason_code=code,
                occurred_at=at,
                candidate_id=plan.candidate_id,
                strategy_key=plan.identity.key,
            )
        )
