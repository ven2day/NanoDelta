"""Pure deterministic risk evaluation for paper-trade intents."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nanodelta.contracts import AdvisoryAction, Market, stable_id, utc
from nanodelta.strategies.registry import StrategyApproval, StrategyIdentity


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    market: Market
    account_id: str
    symbol: str
    action: AdvisoryAction
    quantity: float
    reference_price: float
    event_time: datetime
    candidate_id: str
    approval_id: str
    identity: StrategyIdentity
    gold_snapshot_ids: tuple[str, ...]
    agent_evidence_id: str | None = None

    def __post_init__(self) -> None:
        if self.action is AdvisoryAction.ABSTAIN:
            raise ValueError("ABSTAIN cannot become a trade intent")
        if self.market is not self.identity.market:
            raise ValueError("trade intent market must match strategy identity")
        if not self.account_id or not self.symbol or not self.gold_snapshot_ids:
            raise ValueError("account_id, symbol, and Gold lineage are required")
        if not math.isfinite(self.quantity) or self.quantity <= 0:
            raise ValueError("quantity must be finite and positive")
        if not math.isfinite(self.reference_price) or self.reference_price <= 0:
            raise ValueError("reference_price must be finite and positive")
        utc(self.event_time, "event_time")


@dataclass(frozen=True)
class PortfolioPosition:
    market: Market
    account_id: str
    symbol: str
    signed_quantity: float
    mark_price: float

    @property
    def gross_notional(self) -> float:
        return abs(self.signed_quantity * self.mark_price)


@dataclass(frozen=True)
class PortfolioSnapshot:
    snapshot_id: str
    account_id: str
    equity: float
    realized_pnl_today: float
    positions: tuple[PortfolioPosition, ...]
    captured_at: datetime

    def __post_init__(self) -> None:
        if not math.isfinite(self.equity) or self.equity <= 0:
            raise ValueError("equity must be finite and positive")
        if not math.isfinite(self.realized_pnl_today):
            raise ValueError("realized_pnl_today must be finite")
        if any(position.account_id != self.account_id for position in self.positions):
            raise ValueError("portfolio positions must belong to the snapshot account")
        utc(self.captured_at, "captured_at")


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float
    max_position_notional: float
    max_market_gross_exposure: float
    max_total_gross_exposure: float
    max_daily_loss: float
    max_open_positions: int
    max_snapshot_age_seconds: int = 30

    def __post_init__(self) -> None:
        numeric = (
            self.max_order_notional,
            self.max_position_notional,
            self.max_market_gross_exposure,
            self.max_total_gross_exposure,
            self.max_daily_loss,
        )
        if not all(math.isfinite(value) and value > 0 for value in numeric):
            raise ValueError("risk limits must be finite and positive")
        if self.max_open_positions < 1 or self.max_snapshot_age_seconds < 0:
            raise ValueError("position and freshness limits are invalid")


class RiskDecisionState(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class RiskDecision:
    decision_id: str
    intent: TradeIntent
    portfolio_snapshot_id: str
    state: RiskDecisionState
    rejection_reasons: tuple[str, ...]
    evaluated_at: datetime
    limits: RiskLimits

    @property
    def approved(self) -> bool:
        return self.state is RiskDecisionState.APPROVED


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def evaluate(
        self,
        intent: TradeIntent,
        approval: StrategyApproval,
        portfolio: PortfolioSnapshot,
        *,
        evaluated_at: datetime,
    ) -> RiskDecision:
        evaluated_at = utc(evaluated_at, "evaluated_at")
        reasons: list[str] = []
        if approval.approval_id != intent.approval_id or approval.identity != intent.identity:
            reasons.append("STRATEGY_APPROVAL_MISMATCH")
        elif not approval.is_current(evaluated_at):
            reasons.append("STRATEGY_APPROVAL_NOT_CURRENT")
        if portfolio.account_id != intent.account_id:
            reasons.append("ACCOUNT_MISMATCH")
        age = (evaluated_at - utc(portfolio.captured_at, "captured_at")).total_seconds()
        if age < 0 or age > self._limits.max_snapshot_age_seconds:
            reasons.append("STALE_PORTFOLIO_SNAPSHOT")
        if portfolio.realized_pnl_today <= -self._limits.max_daily_loss:
            reasons.append("DAILY_LOSS_LIMIT_REACHED")

        order_notional = intent.quantity * intent.reference_price
        if order_notional > self._limits.max_order_notional:
            reasons.append("ORDER_NOTIONAL_LIMIT_EXCEEDED")

        target = next(
            (
                position
                for position in portfolio.positions
                if position.market is intent.market and position.symbol == intent.symbol
            ),
            None,
        )
        current_signed = target.signed_quantity if target else 0.0
        delta = intent.quantity if intent.action is AdvisoryAction.BUY else -intent.quantity
        projected_signed = current_signed + delta
        projected_notional = abs(projected_signed * intent.reference_price)
        if projected_notional > self._limits.max_position_notional:
            reasons.append("POSITION_NOTIONAL_LIMIT_EXCEEDED")

        total_gross = sum(position.gross_notional for position in portfolio.positions)
        current_notional = target.gross_notional if target else 0.0
        projected_total = total_gross - current_notional + projected_notional
        if projected_total > self._limits.max_total_gross_exposure:
            reasons.append("TOTAL_GROSS_EXPOSURE_LIMIT_EXCEEDED")

        market_gross = sum(
            position.gross_notional
            for position in portfolio.positions
            if position.market is intent.market
        )
        projected_market = market_gross - current_notional + projected_notional
        if projected_market > self._limits.max_market_gross_exposure:
            reasons.append("MARKET_GROSS_EXPOSURE_LIMIT_EXCEEDED")

        existing_open = sum(position.signed_quantity != 0 for position in portfolio.positions)
        opens_new = current_signed == 0 and projected_signed != 0
        if opens_new and existing_open >= self._limits.max_open_positions:
            reasons.append("OPEN_POSITION_LIMIT_REACHED")

        decision_id = stable_id(
            intent.intent_id,
            portfolio.snapshot_id,
            evaluated_at.isoformat(),
            self._limits,
        )
        return RiskDecision(
            decision_id,
            intent,
            portfolio.snapshot_id,
            RiskDecisionState.REJECTED if reasons else RiskDecisionState.APPROVED,
            tuple(reasons),
            evaluated_at,
            self._limits,
        )

    def evaluate_exit(
        self,
        intent: TradeIntent,
        portfolio: PortfolioSnapshot,
        *,
        evaluated_at: datetime,
    ) -> RiskDecision:
        """Approve only an order that reduces one existing position.

        Protective exits remain available after an entry approval expires or a daily-loss/entry
        kill gate activates. They cannot open, add to, or reverse a position.
        """
        evaluated_at = utc(evaluated_at, "evaluated_at")
        reasons: list[str] = []
        if portfolio.account_id != intent.account_id:
            reasons.append("ACCOUNT_MISMATCH")
        age = (evaluated_at - utc(portfolio.captured_at, "captured_at")).total_seconds()
        if age < 0 or age > self._limits.max_snapshot_age_seconds:
            reasons.append("STALE_PORTFOLIO_SNAPSHOT")
        current = next(
            (
                position
                for position in portfolio.positions
                if position.market is intent.market and position.symbol == intent.symbol
            ),
            None,
        )
        delta = intent.quantity if intent.action is AdvisoryAction.BUY else -intent.quantity
        if current is None or current.signed_quantity == 0:
            reasons.append("OPEN_POSITION_NOT_FOUND")
        elif current.signed_quantity * delta >= 0:
            reasons.append("EXIT_MUST_REDUCE_POSITION")
        elif abs(delta) > abs(current.signed_quantity):
            reasons.append("EXIT_CANNOT_REVERSE_POSITION")
        decision_id = stable_id(
            "protective-exit", intent.intent_id, portfolio.snapshot_id, evaluated_at.isoformat()
        )
        return RiskDecision(
            decision_id,
            intent,
            portfolio.snapshot_id,
            RiskDecisionState.REJECTED if reasons else RiskDecisionState.APPROVED,
            tuple(reasons),
            evaluated_at,
            self._limits,
        )
