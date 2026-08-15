"""Broker-neutral Execution-layer records produced only by execution engines."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from src.core.models import Market, PriceSide
from src.core.pipeline import ProcessingRole


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"


class ExecutionStatus(StrEnum):
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    symbol: str
    side: PriceSide
    requested_quantity: float
    requested_price: float
    order_type: str


@dataclass(frozen=True)
class FillRecord:
    order_id: str
    filled_quantity: float
    fill_price: float
    filled_at: datetime


@dataclass(frozen=True)
class PositionReference:
    position_id: str
    trade_id: str | None = None


@dataclass(frozen=True)
class ExecutionRecord:
    execution_id: str
    execution_version: str
    market: Market
    provider: str
    mode: ExecutionMode
    status: ExecutionStatus
    decision_id: str | None
    order: OrderIntent
    fill: FillRecord | None
    position: PositionReference | None
    message: str
    payload: Mapping[str, Any]
    updated_at: datetime
    producer: ProcessingRole = ProcessingRole.EXECUTION_ENGINE

    @classmethod
    def create(
        cls,
        *,
        market: Market | str,
        provider: str,
        mode: ExecutionMode | str,
        status: ExecutionStatus | str,
        intent_id: str,
        symbol: str,
        side: PriceSide | str,
        requested_quantity: float,
        requested_price: float,
        order_type: str,
        payload: Mapping[str, Any],
        decision_id: str | None = None,
        order_id: str = "",
        filled_quantity: float = 0.0,
        fill_price: float = 0.0,
        position_id: str = "",
        trade_id: str = "",
        message: str = "",
        execution_version: str = "execution-v1",
        updated_at: datetime | None = None,
    ) -> ExecutionRecord:
        normalized_market = Market.parse(market)
        normalized_mode = ExecutionMode(str(mode).strip().upper())
        normalized_status = ExecutionStatus(str(status).strip().upper())
        normalized_side = PriceSide(str(side).strip().upper())
        intent_id = intent_id.strip()
        if not intent_id:
            raise ValueError("Execution intent_id cannot be empty")
        if requested_quantity < 0 or requested_price < 0:
            raise ValueError("Execution request quantity and price cannot be negative")
        if normalized_status in {
            ExecutionStatus.FILLED,
            ExecutionStatus.PARTIALLY_FILLED,
        } and (not order_id.strip() or filled_quantity <= 0 or fill_price <= 0):
            raise ValueError("Filled executions require order ID, quantity, and price")
        when = (updated_at or datetime.now(UTC)).astimezone(UTC)
        identity = f"{normalized_market.value}|{intent_id}|{execution_version}"
        execution_id = hashlib.sha256(identity.encode()).hexdigest()
        fill = (
            FillRecord(
                order_id=order_id.strip(),
                filled_quantity=float(filled_quantity),
                fill_price=float(fill_price),
                filled_at=when,
            )
            if normalized_status
            in {ExecutionStatus.FILLED, ExecutionStatus.PARTIALLY_FILLED}
            else None
        )
        position = (
            PositionReference(position_id=position_id.strip(), trade_id=trade_id.strip() or None)
            if position_id.strip()
            else None
        )
        return cls(
            execution_id=execution_id,
            execution_version=execution_version,
            market=normalized_market,
            provider=provider.strip().upper(),
            mode=normalized_mode,
            status=normalized_status,
            decision_id=decision_id.strip() if decision_id else None,
            order=OrderIntent(
                intent_id=intent_id,
                symbol=symbol.strip().upper(),
                side=normalized_side,
                requested_quantity=float(requested_quantity),
                requested_price=float(requested_price),
                order_type=order_type.strip().upper(),
            ),
            fill=fill,
            position=position,
            message=message,
            payload=MappingProxyType(dict(payload)),
            updated_at=when,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "execution_version": self.execution_version,
            "producer": self.producer.value,
            "market": self.market.value,
            "provider": self.provider,
            "mode": self.mode.value,
            "status": self.status.value,
            "decision_id": self.decision_id,
            "intent_id": self.order.intent_id,
            "symbol": self.order.symbol,
            "side": self.order.side.value,
            "requested_quantity": self.order.requested_quantity,
            "requested_price": self.order.requested_price,
            "order_type": self.order.order_type,
            "fill": (
                {
                    "order_id": self.fill.order_id,
                    "filled_quantity": self.fill.filled_quantity,
                    "fill_price": self.fill.fill_price,
                    "filled_at": self.fill.filled_at.isoformat(),
                }
                if self.fill is not None
                else None
            ),
            "position": (
                {"position_id": self.position.position_id, "trade_id": self.position.trade_id}
                if self.position is not None
                else None
            ),
            "message": self.message,
            "payload": dict(self.payload),
            "updated_at": self.updated_at.isoformat(),
        }


def execution_record_from_result(
    *,
    market: Market | str,
    provider: str,
    intent_id: str,
    requested_price: float,
    order_type: str,
    result: Mapping[str, Any],
    decision_id: str | None = None,
) -> ExecutionRecord:
    raw_mode = str(result.get("mode", "PAPER")).upper()
    if "SHADOW" in raw_mode:
        mode = ExecutionMode.SHADOW
    elif "PAPER" in raw_mode or raw_mode in {"MOCK", "SIMULATED"}:
        mode = ExecutionMode.PAPER
    else:
        raise ValueError("Shared Execution journal currently accepts paper/shadow results only")
    raw_status = str(result.get("status", "REJECTED")).upper()
    status = (
        ExecutionStatus.DUPLICATE
        if bool(result.get("is_duplicate")) or raw_status == "DUPLICATE"
        else ExecutionStatus(raw_status)
    )
    quantity = float(result.get("quantity", 0.0) or 0.0)
    return ExecutionRecord.create(
        market=market,
        provider=provider,
        mode=mode,
        status=status,
        intent_id=intent_id,
        symbol=str(result.get("symbol", "")),
        side=str(result.get("side", "")),
        requested_quantity=quantity,
        requested_price=requested_price,
        order_type=order_type,
        decision_id=decision_id,
        order_id=str(result.get("order_id", "")),
        filled_quantity=quantity,
        fill_price=float(result.get("fill_price", 0.0) or 0.0),
        position_id=str(result.get("position_id", "")),
        trade_id=str(result.get("trade_id", "")),
        message=str(result.get("message", "")),
        payload=result,
    )
