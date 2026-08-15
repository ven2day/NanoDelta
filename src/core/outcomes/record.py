"""Versioned closed-trade Outcome records for attribution and offline learning."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from src.core.models import Market, PriceSide
from src.core.pipeline import ProcessingRole


@dataclass(frozen=True)
class OutcomeRecord:
    outcome_id: str
    outcome_version: str
    market: Market
    provider: str
    trade_id: str
    decision_id: str | None
    entry_execution_id: str | None
    exit_execution_id: str | None
    feature_snapshot_id: str | None
    symbol: str
    timeframe: str
    side: PriceSide
    strategy: str
    opened_at: datetime | None
    closed_at: datetime
    entry_price: float
    exit_price: float
    quantity: float
    net_pnl: float
    return_pct: float
    mae: float
    mfe: float
    hold_minutes: int
    exit_reason: str
    attribution: Mapping[str, Any]
    learning_eligible: bool
    payload: Mapping[str, Any]
    producer: ProcessingRole = ProcessingRole.OUTCOME_ENGINE

    @classmethod
    def create(
        cls,
        *,
        market: Market | str,
        provider: str,
        trade_id: str,
        symbol: str,
        timeframe: str,
        side: PriceSide | str,
        strategy: str,
        closed_at: datetime,
        entry_price: float,
        exit_price: float,
        quantity: float,
        net_pnl: float,
        return_pct: float,
        exit_reason: str,
        attribution: Mapping[str, Any],
        payload: Mapping[str, Any],
        decision_id: str | None = None,
        entry_execution_id: str | None = None,
        exit_execution_id: str | None = None,
        feature_snapshot_id: str | None = None,
        opened_at: datetime | None = None,
        mae: float = 0.0,
        mfe: float = 0.0,
        hold_minutes: int = 0,
        outcome_version: str = "outcome-v1",
    ) -> OutcomeRecord:
        normalized_market = Market.parse(market)
        trade_id = trade_id.strip()
        if not trade_id:
            raise ValueError("Outcome trade_id cannot be empty")
        if closed_at.tzinfo is None or (opened_at is not None and opened_at.tzinfo is None):
            raise ValueError("Outcome timestamps must be timezone-aware")
        if entry_price <= 0 or exit_price <= 0 or quantity <= 0:
            raise ValueError("Outcome prices and quantity must be positive")
        if hold_minutes < 0:
            raise ValueError("Outcome hold_minutes cannot be negative")
        identity = f"{normalized_market.value}|{trade_id}|{outcome_version}"
        feature_id = feature_snapshot_id.strip() if feature_snapshot_id else None
        return cls(
            outcome_id=hashlib.sha256(identity.encode()).hexdigest(),
            outcome_version=outcome_version,
            market=normalized_market,
            provider=provider.strip().upper(),
            trade_id=trade_id,
            decision_id=decision_id.strip() if decision_id else None,
            entry_execution_id=(entry_execution_id.strip() if entry_execution_id else None),
            exit_execution_id=exit_execution_id.strip() if exit_execution_id else None,
            feature_snapshot_id=feature_id,
            symbol=symbol.strip().upper(),
            timeframe=timeframe.strip().lower(),
            side=PriceSide(str(side).strip().upper()),
            strategy=strategy.strip().lower(),
            opened_at=opened_at.astimezone(UTC) if opened_at is not None else None,
            closed_at=closed_at.astimezone(UTC),
            entry_price=float(entry_price),
            exit_price=float(exit_price),
            quantity=float(quantity),
            net_pnl=float(net_pnl),
            return_pct=float(return_pct),
            mae=float(mae),
            mfe=float(mfe),
            hold_minutes=int(hold_minutes),
            exit_reason=exit_reason.strip().upper() or "UNKNOWN",
            attribution=MappingProxyType(dict(attribution)),
            # A label is safe for offline training only when its exact feature grain is linked.
            learning_eligible=feature_id is not None,
            payload=MappingProxyType(dict(payload)),
        )

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "outcome_version": self.outcome_version,
            "producer": self.producer.value,
            "market": self.market.value,
            "provider": self.provider,
            "trade_id": self.trade_id,
            "decision_id": self.decision_id,
            "entry_execution_id": self.entry_execution_id,
            "exit_execution_id": self.exit_execution_id,
            "feature_snapshot_id": self.feature_snapshot_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "strategy": self.strategy,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat(),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "net_pnl": self.net_pnl,
            "return_pct": self.return_pct,
            "is_winner": self.is_winner,
            "mae": self.mae,
            "mfe": self.mfe,
            "hold_minutes": self.hold_minutes,
            "exit_reason": self.exit_reason,
            "attribution": dict(self.attribution),
            "learning_eligible": self.learning_eligible,
            "payload": dict(self.payload),
        }
