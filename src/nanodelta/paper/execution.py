"""Deterministic immediate-fill paper execution; no live execution interface exists."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from nanodelta.agents.tradingagents import AdvisoryAction
from nanodelta.contracts import Market, stable_id, utc
from nanodelta.risk.engine import RiskDecision


class OrderState(StrEnum):
    FILLED = "FILLED"


class PositionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class ExecutionPolicy:
    slippage_bps: float
    fee_bps: float

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value) and value >= 0 for value in (self.slippage_bps, self.fee_bps)
        ):
            raise ValueError("slippage and fees must be finite and non-negative")


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    idempotency_key: str
    decision_id: str
    market: Market
    account_id: str
    symbol: str
    action: AdvisoryAction
    quantity: float
    state: OrderState
    submitted_at: datetime


@dataclass(frozen=True)
class PaperFill:
    fill_id: str
    order_id: str
    quantity: float
    price: float
    fee: float
    filled_at: datetime


@dataclass(frozen=True)
class PaperPosition:
    position_id: str
    market: Market
    account_id: str
    symbol: str
    signed_quantity: float
    average_entry_price: float
    realized_pnl: float
    total_fees: float
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    state: PositionState
    decision_ids: tuple[str, ...]
    strategy_keys: tuple[str, ...]
    approval_ids: tuple[str, ...]
    gold_snapshot_ids: tuple[str, ...]
    agent_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionReceipt:
    order: PaperOrder
    fill: PaperFill
    position: PaperPosition


class PaperExecutionEngine:
    """In-memory paper ledger with idempotent immediate market fills."""

    def __init__(self, policy: ExecutionPolicy) -> None:
        self._policy = policy
        self._receipts: dict[str, ExecutionReceipt] = {}
        self._positions: dict[tuple[Market, str, str], PaperPosition] = {}

    def execute(
        self,
        decision: RiskDecision,
        *,
        idempotency_key: str,
        executed_at: datetime,
    ) -> ExecutionReceipt:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        cached = self._receipts.get(idempotency_key)
        if cached is not None:
            if cached.order.decision_id != decision.decision_id:
                raise ValueError("idempotency key is already bound to another decision")
            return cached
        if not decision.approved:
            raise PermissionError("paper execution requires an approved risk decision")

        executed_at = utc(executed_at, "executed_at")
        intent = decision.intent
        direction = 1.0 if intent.action is AdvisoryAction.BUY else -1.0
        fill_price = intent.reference_price * (1 + direction * self._policy.slippage_bps / 10_000)
        fee = intent.quantity * fill_price * self._policy.fee_bps / 10_000
        order_id = stable_id("paper-order", idempotency_key, decision.decision_id)
        fill_id = stable_id("paper-fill", order_id, executed_at.isoformat())
        order = PaperOrder(
            order_id,
            idempotency_key,
            decision.decision_id,
            intent.market,
            intent.account_id,
            intent.symbol,
            intent.action,
            intent.quantity,
            OrderState.FILLED,
            executed_at,
        )
        fill = PaperFill(fill_id, order_id, intent.quantity, fill_price, fee, executed_at)
        key = (intent.market, intent.account_id, intent.symbol)
        position = self._apply_fill(self._positions.get(key), decision, fill)
        self._positions[key] = position
        receipt = ExecutionReceipt(order, fill, position)
        self._receipts[idempotency_key] = receipt
        return receipt

    def position(self, market: Market, account_id: str, symbol: str) -> PaperPosition | None:
        return self._positions.get((market, account_id, symbol))

    @staticmethod
    def _apply_fill(
        current: PaperPosition | None, decision: RiskDecision, fill: PaperFill
    ) -> PaperPosition:
        intent = decision.intent
        delta = fill.quantity if intent.action is AdvisoryAction.BUY else -fill.quantity
        if current is None or current.state is PositionState.CLOSED:
            signed = delta
            return PaperPosition(
                stable_id(
                    "paper-position",
                    intent.market.value,
                    intent.account_id,
                    intent.symbol,
                    fill.fill_id,
                ),
                intent.market,
                intent.account_id,
                intent.symbol,
                signed,
                fill.price,
                0.0,
                fill.fee,
                fill.filled_at,
                fill.filled_at,
                None,
                PositionState.OPEN,
                (decision.decision_id,),
                (intent.identity.key,),
                (intent.approval_id,),
                intent.gold_snapshot_ids,
                (intent.agent_evidence_id,) if intent.agent_evidence_id else (),
            )

        old = current.signed_quantity
        new = old + delta
        same_direction = old * delta > 0
        realized = current.realized_pnl
        average = current.average_entry_price
        if same_direction:
            average = (abs(old) * current.average_entry_price + abs(delta) * fill.price) / abs(new)
        else:
            closing = min(abs(old), abs(delta))
            realized += (
                closing * (fill.price - current.average_entry_price) * (1.0 if old > 0 else -1.0)
            )
            if new == 0:
                average = 0.0
            elif old * new < 0:
                average = fill.price

        return replace(
            current,
            signed_quantity=new,
            average_entry_price=average,
            realized_pnl=realized,
            total_fees=current.total_fees + fill.fee,
            updated_at=fill.filled_at,
            closed_at=fill.filled_at if new == 0 else None,
            state=PositionState.CLOSED if new == 0 else PositionState.OPEN,
            decision_ids=(*current.decision_ids, decision.decision_id),
            strategy_keys=tuple(dict.fromkeys((*current.strategy_keys, intent.identity.key))),
            approval_ids=tuple(dict.fromkeys((*current.approval_ids, intent.approval_id))),
            gold_snapshot_ids=tuple(
                dict.fromkeys((*current.gold_snapshot_ids, *intent.gold_snapshot_ids))
            ),
            agent_evidence_ids=tuple(
                dict.fromkeys(
                    (
                        *current.agent_evidence_ids,
                        *((intent.agent_evidence_id,) if intent.agent_evidence_id else ()),
                    )
                )
            ),
        )
