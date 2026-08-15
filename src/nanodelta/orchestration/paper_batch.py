"""Handoff from a constructed portfolio batch to deterministic risk and paper execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nanodelta.contracts import Market, stable_id, utc
from nanodelta.decisions import Decision, DecisionLedger, DecisionStage, DecisionStatus
from nanodelta.orchestration.decision_pipeline import PipelineResult
from nanodelta.paper import ExecutionReceipt, PaperExecutionEngine
from nanodelta.risk import PortfolioSnapshot, RiskDecision, RiskEngine, TradeIntent
from nanodelta.strategies import StrategyRegistry


@dataclass(frozen=True)
class PaperBatchResult:
    risk_decisions: tuple[RiskDecision, ...]
    receipts: tuple[ExecutionReceipt, ...]


class PaperBatchExecutor:
    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        risk: RiskEngine,
        execution: PaperExecutionEngine,
        ledger: DecisionLedger,
    ) -> None:
        self._registry = registry
        self._risk = risk
        self._execution = execution
        self._ledger = ledger

    def execute(
        self,
        result: PipelineResult,
        *,
        account_id: str,
        portfolio: PortfolioSnapshot,
        evaluated_at: datetime,
    ) -> PaperBatchResult:
        evaluated_at = utc(evaluated_at, "evaluated_at")
        risk_decisions = []
        for allocation in result.allocations:
            candidate = allocation.candidate.candidate
            approval = self._registry.require_approval(candidate.identity, at=evaluated_at)
            intent = TradeIntent(
                stable_id("trade-intent", result.cycle_id, candidate.candidate_id),
                candidate.identity.market,
                account_id,
                candidate.symbol,
                candidate.signal.action,
                allocation.quantity,
                allocation.expected_entry_price,
                evaluated_at,
                candidate.candidate_id,
                approval.approval_id,
                candidate.identity,
                candidate.gold_snapshot_ids,
            )
            risk_decision = self._risk.evaluate(
                intent, approval, portfolio, evaluated_at=evaluated_at
            )
            risk_decisions.append(risk_decision)
            self._append(
                result,
                candidate.identity.market,
                candidate.symbol,
                candidate.identity.timeframe,
                candidate.candidate_id,
                candidate.identity.key,
                DecisionStage.RISK,
                DecisionStatus.PASSED if risk_decision.approved else DecisionStatus.REJECTED,
                "RISK_APPROVED" if risk_decision.approved else risk_decision.rejection_reasons[0],
                evaluated_at,
            )
        decisions = tuple(risk_decisions)
        if any(not decision.approved for decision in decisions):
            return PaperBatchResult(decisions, ())
        receipts = self._execution.execute_batch(
            decisions, batch_id=result.cycle_id, executed_at=evaluated_at
        )
        for allocation, receipt in zip(result.allocations, receipts, strict=True):
            candidate = allocation.candidate.candidate
            self._append(
                result,
                candidate.identity.market,
                candidate.symbol,
                candidate.identity.timeframe,
                candidate.candidate_id,
                candidate.identity.key,
                DecisionStage.EXECUTION,
                DecisionStatus.ORDERED,
                "PAPER_ORDER_CREATED",
                evaluated_at,
                detail=receipt.order.order_id,
            )
        return PaperBatchResult(decisions, receipts)

    def _append(
        self,
        result: PipelineResult,
        market: Market,
        symbol: str,
        timeframe: str,
        candidate_id: str,
        strategy_key: str,
        stage: DecisionStage,
        status: DecisionStatus,
        reason: str,
        occurred_at: datetime,
        *,
        detail: str = "",
    ) -> None:
        decision = Decision.create(
            cycle_id=result.cycle_id,
            market=market,
            symbol=symbol,
            timeframe=timeframe,
            stage=stage,
            status=status,
            reason_code=reason,
            occurred_at=occurred_at,
            candidate_id=candidate_id,
            strategy_key=strategy_key,
            detail=detail,
        )
        self._ledger.append(decision)
