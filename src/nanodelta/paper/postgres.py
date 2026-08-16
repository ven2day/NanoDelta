"""Durable PostgreSQL adapter for paper execution.

PaperExecutionEngine's own docstring says what it is: "In-memory paper ledger
with idempotent immediate market fills." Every paper order, fill, and position
vanished on restart. This adapter persists them to paper.decisions/orders/
fills/positions (already migrated, previously unwritten) using the same
hydrate-then-delegate-then-persist pattern as PostgresOperationalStore and
PostgresStrategyRegistry: prerequisite state is loaded from PostgreSQL into the
in-memory cache the base class already trusts, the base class's own tested
fill/idempotency/position-netting logic runs unchanged, and the result is
written back inside one transaction per call (or one per batch).

paper.orders.decision_id is a foreign key into paper.decisions, so every
execute() persists the approved RiskDecision as a paper.decisions row before
the order that depends on it -- decisions rejected upstream never reach this
class at all (PaperBatchExecutor withholds a batch entirely if any member is
rejected), so only APPROVED decisions are ever written here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import cast

from nanodelta.contracts import AdvisoryAction, Market, stable_id
from nanodelta.paper.execution import (
    ExecutionPolicy,
    ExecutionReceipt,
    OrderState,
    PaperExecutionEngine,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PositionState,
)
from nanodelta.persistence.migrations import Connection
from nanodelta.risk import RiskDecision


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_json(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


class PostgresPaperExecutionEngine(PaperExecutionEngine):
    def __init__(self, policy: ExecutionPolicy, connect: Callable[[], Connection]) -> None:
        super().__init__(policy)
        self._connect = connect

    def execute(
        self,
        decision: RiskDecision,
        *,
        idempotency_key: str,
        executed_at: datetime,
    ) -> ExecutionReceipt:
        connection = self._connect()
        try:
            receipt = self._execute_one(connection, decision, idempotency_key, executed_at)
            connection.commit()
            return receipt
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute_batch(
        self,
        decisions: tuple[RiskDecision, ...],
        *,
        batch_id: str,
        executed_at: datetime,
    ) -> tuple[ExecutionReceipt, ...]:
        if not batch_id.strip():
            raise ValueError("batch_id is required")
        if any(not decision.approved for decision in decisions):
            raise PermissionError("every batch risk decision must be approved")
        keys = tuple(f"{batch_id}:{decision.decision_id}" for decision in decisions)
        if len(keys) != len(set(keys)):
            raise ValueError("batch contains duplicate risk decisions")
        connection = self._connect()
        try:
            receipts = tuple(
                self._execute_one(connection, decision, key, executed_at)
                for key, decision in zip(keys, decisions, strict=True)
            )
            connection.commit()
            return receipts
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _execute_one(
        self,
        connection: Connection,
        decision: RiskDecision,
        idempotency_key: str,
        executed_at: datetime,
    ) -> ExecutionReceipt:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        existing = self._load_receipt(connection, idempotency_key)
        if existing is not None:
            if existing.order.decision_id != decision.decision_id:
                raise ValueError("idempotency key is already bound to another decision")
            self._receipts[idempotency_key] = existing
            return existing
        if not decision.approved:
            raise PermissionError("paper execution requires an approved risk decision")

        intent = decision.intent
        key = (intent.market, intent.account_id, intent.symbol)
        current = self._load_open_position(connection, *key)
        if current is not None:
            self._positions[key] = current

        receipt = super().execute(
            decision, idempotency_key=idempotency_key, executed_at=executed_at
        )
        self._save_decision(connection, decision)
        self._save_order(connection, idempotency_key, receipt.order)
        self._save_fill(connection, receipt.fill)
        self._save_position(connection, receipt.position)
        prior_realized = current.realized_pnl if current is not None else 0.0
        self._save_realization(connection, receipt.fill, receipt.position, prior_realized)
        return receipt

    # -- writes -----------------------------------------------------------

    def _save_decision(self, connection: Connection, decision: RiskDecision) -> None:
        intent = decision.intent
        connection.cursor().execute(
            "INSERT INTO paper.decisions "
            "(decision_id,intent_id,market,account_id,symbol,action,quantity,reference_price,"
            "candidate_id,approval_id,portfolio_snapshot_id,state,rejection_reasons,limits,"
            "gold_snapshot_ids,agent_evidence_id,evaluated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s) "
            "ON CONFLICT (decision_id) DO NOTHING",
            (
                decision.decision_id,
                intent.intent_id,
                intent.market.value,
                intent.account_id,
                intent.symbol,
                intent.action.value,
                intent.quantity,
                intent.reference_price,
                intent.candidate_id,
                intent.approval_id,
                decision.portfolio_snapshot_id,
                decision.state.value,
                _dump(list(decision.rejection_reasons)),
                _dump(asdict(decision.limits)),
                _dump(list(intent.gold_snapshot_ids)),
                intent.agent_evidence_id,
                decision.evaluated_at,
            ),
        )

    def _save_order(self, connection: Connection, idempotency_key: str, order: PaperOrder) -> None:
        connection.cursor().execute(
            "INSERT INTO paper.orders "
            "(order_id,idempotency_key,decision_id,market,account_id,symbol,action,quantity,"
            "state,submitted_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (
                order.order_id,
                idempotency_key,
                order.decision_id,
                order.market.value,
                order.account_id,
                order.symbol,
                order.action.value,
                order.quantity,
                order.state.value,
                order.submitted_at,
            ),
        )

    def _save_fill(self, connection: Connection, fill: PaperFill) -> None:
        connection.cursor().execute(
            "INSERT INTO paper.fills (fill_id,order_id,quantity,price,fee,filled_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (order_id) DO NOTHING",
            (fill.fill_id, fill.order_id, fill.quantity, fill.price, fill.fee, fill.filled_at),
        )

    def _save_position(self, connection: Connection, position: PaperPosition) -> None:
        connection.cursor().execute(
            "INSERT INTO paper.positions "
            "(position_id,market,account_id,symbol,signed_quantity,average_entry_price,"
            "realized_pnl,total_fees,opened_at,updated_at,closed_at,state,decision_ids,"
            "strategy_keys,approval_ids,gold_snapshot_ids,agent_evidence_ids) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,"
            "%s::jsonb,%s::jsonb) "
            "ON CONFLICT (position_id) DO UPDATE SET "
            "signed_quantity=EXCLUDED.signed_quantity,"
            "average_entry_price=EXCLUDED.average_entry_price,"
            "realized_pnl=EXCLUDED.realized_pnl,"
            "total_fees=EXCLUDED.total_fees,"
            "updated_at=EXCLUDED.updated_at,"
            "closed_at=EXCLUDED.closed_at,"
            "state=EXCLUDED.state,"
            "decision_ids=EXCLUDED.decision_ids,"
            "strategy_keys=EXCLUDED.strategy_keys,"
            "approval_ids=EXCLUDED.approval_ids,"
            "gold_snapshot_ids=EXCLUDED.gold_snapshot_ids,"
            "agent_evidence_ids=EXCLUDED.agent_evidence_ids",
            (
                position.position_id,
                position.market.value,
                position.account_id,
                position.symbol,
                position.signed_quantity,
                position.average_entry_price,
                position.realized_pnl,
                position.total_fees,
                position.opened_at,
                position.updated_at,
                position.closed_at,
                position.state.value,
                _dump(list(position.decision_ids)),
                _dump(list(position.strategy_keys)),
                _dump(list(position.approval_ids)),
                _dump(list(position.gold_snapshot_ids)),
                _dump(list(position.agent_evidence_ids)),
            ),
        )

    def _save_realization(
        self,
        connection: Connection,
        fill: PaperFill,
        position: PaperPosition,
        prior_realized_pnl: float,
    ) -> None:
        gross_delta = position.realized_pnl - prior_realized_pnl
        connection.cursor().execute(
            "INSERT INTO paper.realization_events "
            "(event_id,fill_id,position_id,market,account_id,symbol,gross_pnl_delta,fee,"
            "net_pnl,realized_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (fill_id) DO NOTHING",
            (
                stable_id("paper-realization", fill.fill_id),
                fill.fill_id,
                position.position_id,
                position.market.value,
                position.account_id,
                position.symbol,
                gross_delta,
                fill.fee,
                gross_delta - fill.fee,
                fill.filled_at,
            ),
        )

    # -- reads --------------------------------------------------------------

    def _load_open_position(
        self, connection: Connection, market: Market, account_id: str, symbol: str
    ) -> PaperPosition | None:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT position_id,market,account_id,symbol,signed_quantity,average_entry_price,"
            "realized_pnl,total_fees,opened_at,updated_at,closed_at,state,decision_ids,"
            "strategy_keys,approval_ids,gold_snapshot_ids,agent_evidence_ids "
            "FROM paper.positions WHERE market=%s AND account_id=%s AND symbol=%s AND state='OPEN'",
            (market.value, account_id, symbol),
        )
        row = cursor.fetchone()
        return self._position_from_row(row) if row is not None else None

    def _load_receipt(
        self, connection: Connection, idempotency_key: str
    ) -> ExecutionReceipt | None:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT o.order_id,o.idempotency_key,o.decision_id,o.market,o.account_id,o.symbol,"
            "o.action,o.quantity,o.state,o.submitted_at,"
            "f.fill_id,f.quantity,f.price,f.fee,f.filled_at,"
            "p.position_id,p.market,p.account_id,p.symbol,p.signed_quantity,"
            "p.average_entry_price,p.realized_pnl,p.total_fees,p.opened_at,p.updated_at,"
            "p.closed_at,p.state,p.decision_ids,p.strategy_keys,p.approval_ids,"
            "p.gold_snapshot_ids,p.agent_evidence_ids "
            "FROM paper.orders o "
            "JOIN paper.fills f ON f.order_id=o.order_id "
            "JOIN paper.positions p ON p.decision_ids ? o.decision_id "
            "WHERE o.idempotency_key=%s",
            (idempotency_key,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        order = PaperOrder(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            Market(str(row[3])),
            str(row[4]),
            str(row[5]),
            AdvisoryAction(str(row[6])),
            float(cast(float, row[7])),
            OrderState(str(row[8])),
            cast(datetime, row[9]),
        )
        fill = PaperFill(
            str(row[10]),
            order.order_id,
            float(cast(float, row[11])),
            float(cast(float, row[12])),
            float(cast(float, row[13])),
            cast(datetime, row[14]),
        )
        position = self._position_from_row(row[15:])
        assert position is not None
        return ExecutionReceipt(order, fill, position)

    @staticmethod
    def _position_from_row(row: tuple[object, ...] | None) -> PaperPosition | None:
        if row is None:
            return None
        return PaperPosition(
            str(row[0]),
            Market(str(row[1])),
            str(row[2]),
            str(row[3]),
            float(cast(float, row[4])),
            float(cast(float, row[5])),
            float(cast(float, row[6])),
            float(cast(float, row[7])),
            cast(datetime, row[8]),
            cast(datetime, row[9]),
            cast(datetime | None, row[10]),
            PositionState(str(row[11])),
            tuple(cast(list[str], _load_json(row[12]))),
            tuple(cast(list[str], _load_json(row[13]))),
            tuple(cast(list[str], _load_json(row[14]))),
            tuple(cast(list[str], _load_json(row[15]))),
            tuple(cast(list[str], _load_json(row[16]))),
        )
