"""Local-paper Forex decision, order, and position lifecycle.

No method in this module submits an OANDA order.  OANDA quotes are used only to
simulate executable-side fills and deterministic stop/target exits.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from src.core.execution import (
    ExecutionStatus,
    SchemaBoundExecutionRepository,
    execution_record_from_result,
    persist_execution_records,
    stable_execution_id,
)
from src.core.models import CanonicalQuote
from src.core.outcomes import (
    OutcomeRecord,
    SchemaBoundOutcomeRepository,
    persist_outcome_records,
)
from src.markets.forex.persistence import forex_record_id
from src.markets.forex.runtime.lifecycle import (
    ForexDecisionLifecycle,
    ForexDecisionStage,
    append_persisted_stage,
)


class ForexPricingProvider(Protocol):
    async def get_prices(self, symbols: list[str]) -> dict[str, CanonicalQuote]: ...


def persist_lifecycle_record(
    repository: Any,
    record: ForexDecisionLifecycle,
    *,
    signal_id: str | None = None,
) -> None:
    repository.persist_decision(
        decision_id=record.decision_id,
        signal_id=signal_id or record.candidate_id,
        symbol=record.instrument,
        final_action=record.final_action,
        rejection_reason=record.rejection_reason,
        payload=record.to_dict(),
        created_at=record.settled_candle_timestamp,
    )


def create_local_paper_position(
    repository: Any,
    record: ForexDecisionLifecycle,
    *,
    quantity: float,
    position_payload: dict[str, Any],
) -> bool:
    """Persist final decision first, then atomically publish local order + position."""

    intent_id = forex_record_id("paper-intent", record.candidate_id)
    order_id = forex_record_id("paper-order", intent_id)
    position_id = forex_record_id("paper-position", intent_id)
    record.order_id = order_id
    record.position_id = position_id
    record.final_action = f"PAPER_{record.side}"
    record.advance(ForexDecisionStage.FINAL_PAPER_DECISION, "DETERMINISTIC_RISK_APPROVED")
    persist_lifecycle_record(repository, record)

    canonical_position = {
        **position_payload,
        "decision_id": record.decision_id,
        "candidate_id": record.candidate_id,
        "candidate_version": record.candidate_version,
        "feature_snapshot_id": record.feature_snapshot_id,
        "intent_id": intent_id,
        "order_id": order_id,
        "position_id": position_id,
        "quantity": float(quantity),
        "timeframe": record.timeframe,
        "strategy": record.strategy,
        "supporting_strategies": list(record.supporting_strategies),
        "status": "OPEN",
    }
    created = repository.persist_paper_fill(
        intent_id=intent_id,
        order_id=order_id,
        position_id=position_id,
        symbol=record.instrument,
        side=record.side,
        quantity=quantity,
        order_payload={**canonical_position, "status": "SIMULATED_FILLED"},
        position_payload=canonical_position,
        created_at=record.settled_candle_timestamp,
    )
    if created:
        record.advance(ForexDecisionStage.PAPER_ORDER_CREATED, "LOCAL_PAPER_ORDER_PERSISTED")
        record.advance(ForexDecisionStage.POSITION_OPEN, "SIMULATED_FILL_CONFIRMED")
    else:
        # The deterministic final decision remains auditable even when the local
        # paper order cannot be created.  It is not a position and never reaches
        # OANDA, but it must not disappear from the final-decision ledger.
        record.rejection_reason = "PAPER_ORDER_REJECTED_DUPLICATE_INTENT"
    persist_lifecycle_record(repository, record)
    return bool(created)


@dataclass(frozen=True)
class ForexPositionManagementResult:
    positions_monitored: int = 0
    positions_closed: int = 0
    stop_exits: int = 0
    target_exits: int = 0
    stale_or_missing_quotes: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "positions_monitored": self.positions_monitored,
            "positions_closed": self.positions_closed,
            "stop_exits": self.stop_exits,
            "target_exits": self.target_exits,
            "stale_or_missing_quotes": self.stale_or_missing_quotes,
        }


def _exit_for_position(
    position: dict[str, Any], quote: CanonicalQuote
) -> tuple[str, float] | None:
    side = str(position.get("side", "")).upper()
    stop = float(position.get("stop_price", position.get("stop_loss", 0.0)) or 0.0)
    target = float(position.get("target_price", 0.0) or 0.0)
    if side == "BUY":
        executable = float(quote.bid)
        if stop > 0 and executable <= stop:
            return "STOP", executable
        if target > 0 and executable >= target:
            return "TARGET", executable
    elif side == "SELL":
        executable = float(quote.ask)
        if stop > 0 and executable >= stop:
            return "STOP", executable
        if target > 0 and executable <= target:
            return "TARGET", executable
    return None


class ForexPaperPositionManager:
    """Manage persisted local-paper positions using executable quote sides."""

    def __init__(
        self,
        provider: ForexPricingProvider,
        repository: Any,
        execution_repository: SchemaBoundExecutionRepository | None = None,
        outcome_repository: SchemaBoundOutcomeRepository | None = None,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.execution_repository = execution_repository
        self.outcome_repository = outcome_repository

    async def manage(self) -> ForexPositionManagementResult:
        positions = await asyncio.to_thread(self.repository.load_open_positions)
        if not positions:
            return ForexPositionManagementResult()
        symbols = sorted(
            {
                str(item.get("symbol", item.get("instrument", ""))).upper()
                for item in positions
                if item.get("symbol") or item.get("instrument")
            }
        )
        quotes = await self.provider.get_prices(symbols)
        closed = stop_exits = target_exits = missing = 0
        for position in positions:
            symbol = str(position.get("symbol", position.get("instrument", ""))).upper()
            quote = quotes.get(symbol)
            if quote is None:
                missing += 1
                continue
            exit_result = _exit_for_position(position, quote)
            if exit_result is None:
                continue
            reason, exit_price = exit_result
            decision_id = str(position.get("decision_id", ""))
            position_id = str(position.get("position_id", ""))
            if not decision_id or not position_id:
                missing += 1
                continue
            decision = await asyncio.to_thread(self.repository.load_decision, decision_id)
            if decision is None:
                missing += 1
                continue
            closed_when = datetime.now(UTC)
            closed_at = closed_when.isoformat()
            closed_position = {
                **position,
                "status": "CLOSED",
                "closed_at": closed_at,
                "exit_price": exit_price,
                "exit_reason": reason,
            }
            closed_decision = append_persisted_stage(
                decision,
                ForexDecisionStage.POSITION_CLOSED,
                f"{reason}_EXIT_FILLED",
            )
            exit_side = "SELL" if str(position.get("side", "")).upper() == "BUY" else "BUY"
            intent_id = forex_record_id(
                "paper-exit-intent", position_id, reason, quote.timestamp.isoformat()
            )
            order_id = forex_record_id("paper-exit-order", intent_id)
            persisted = await asyncio.to_thread(
                self.repository.persist_paper_close,
                intent_id=intent_id,
                order_id=order_id,
                position_id=position_id,
                decision_id=decision_id,
                symbol=symbol,
                side=exit_side,
                order_payload={
                    **closed_position,
                    "side": exit_side,
                    "status": "SIMULATED_FILLED",
                },
                position_payload=closed_position,
                decision_payload=closed_decision,
                closed_at=closed_at,
            )
            if not persisted:
                continue
            execution_record = execution_record_from_result(
                market="FOREX",
                provider="OANDA",
                intent_id=intent_id,
                requested_price=exit_price,
                order_type="MARKET",
                decision_id=decision_id,
                result={
                    "status": ExecutionStatus.FILLED.value,
                    "symbol": symbol,
                    "side": exit_side,
                    "quantity": float(position.get("quantity", 0.0) or 0.0),
                    "fill_price": exit_price,
                    "mode": "PAPER",
                    "order_id": order_id,
                    "position_id": position_id,
                    "message": f"{reason} paper exit fill",
                },
            )
            await asyncio.to_thread(
                persist_execution_records,
                self.execution_repository,
                [execution_record],
            )
            quantity = float(position.get("quantity", 0.0) or 0.0)
            entry_price = float(position.get("entry_price", 0.0) or 0.0)
            entry_side = str(position.get("side", "BUY")).upper()
            signed = 1.0 if entry_side == "BUY" else -1.0
            net_pnl = (exit_price - entry_price) * quantity * signed
            notional = entry_price * quantity
            opened_at = None
            opened_token = str(position.get("settled_candle_timestamp", ""))
            if opened_token:
                try:
                    opened_at = datetime.fromisoformat(opened_token.replace("Z", "+00:00"))
                except ValueError:
                    opened_at = None
            candidate_id = str(position.get("candidate_id", ""))
            outcome = OutcomeRecord.create(
                market="FOREX",
                provider="OANDA",
                trade_id=position_id,
                decision_id=decision_id,
                entry_execution_id=(
                    stable_execution_id(
                        "FOREX",
                        forex_record_id("paper-intent", candidate_id),
                    )
                    if candidate_id
                    else None
                ),
                exit_execution_id=execution_record.execution_id,
                feature_snapshot_id=(
                    str(position.get("feature_snapshot_id", "")).strip() or None
                ),
                symbol=symbol,
                timeframe=str(position.get("timeframe", "")),
                side=entry_side,
                strategy=str(position.get("strategy", "")),
                opened_at=opened_at,
                closed_at=closed_when,
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                net_pnl=net_pnl,
                return_pct=(net_pnl / notional * 100 if notional else 0.0),
                exit_reason=reason,
                attribution={
                    "candidate_id": candidate_id,
                    "candidate_version": position.get("candidate_version"),
                    "supporting_strategies": position.get("supporting_strategies", []),
                    "account_home_currency": position.get("account_home_currency"),
                },
                payload=closed_position,
            )
            await asyncio.to_thread(
                persist_outcome_records,
                self.outcome_repository,
                [outcome],
            )
            closed += 1
            if reason == "STOP":
                stop_exits += 1
            else:
                target_exits += 1
        return ForexPositionManagementResult(
            positions_monitored=len(positions),
            positions_closed=closed,
            stop_exits=stop_exits,
            target_exits=target_exits,
            stale_or_missing_quotes=missing,
        )


__all__ = [
    "ForexPaperPositionManager",
    "ForexPositionManagementResult",
    "create_local_paper_position",
    "persist_lifecycle_record",
]
