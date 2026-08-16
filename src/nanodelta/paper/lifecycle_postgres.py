"""PostgreSQL persistence for exit plans and immutable outcomes."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import cast

from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.outcomes import Outcome
from nanodelta.paper.lifecycle import ExitPlan, ExitReason, LifecycleStore
from nanodelta.persistence.migrations import Connection
from nanodelta.strategies import StrategyIdentity


class PostgresLifecycleStore(LifecycleStore):
    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def save_plan(self, plan: ExitPlan) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "INSERT INTO paper.exit_plans "
                "(position_id,market,account_id,symbol,entry_action,quantity,stop_price,"
                "target_price,allocated_capital,candidate_id,approval_id,strategy_key,"
                "gold_snapshot_ids,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s::jsonb,%s) ON CONFLICT (position_id) DO NOTHING",
                (
                    plan.position_id,
                    plan.market.value,
                    plan.account_id,
                    plan.symbol,
                    plan.entry_action.value,
                    plan.quantity,
                    plan.stop_price,
                    plan.target_price,
                    plan.allocated_capital,
                    plan.candidate_id,
                    plan.approval_id,
                    plan.identity.key,
                    json.dumps(plan.gold_snapshot_ids),
                    plan.created_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def active(self, market: Market, account_id: str) -> tuple[ExitPlan, ...]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE paper.exit_plans p SET state='CLOSED',closed_at=x.closed_at "
                "FROM paper.positions x WHERE x.position_id=p.position_id "
                "AND p.state='ACTIVE' AND x.state='CLOSED'"
            )
            connection.commit()
            cursor.execute(
                "SELECT p.position_id,p.market,p.account_id,p.symbol,p.entry_action,p.quantity,"
                "p.stop_price,p.target_price,p.allocated_capital,p.candidate_id,p.approval_id,"
                "d.strategy_id,d.strategy_version,d.timeframe,d.trade_horizon,"
                "d.feature_set_version,p.gold_snapshot_ids,p.created_at "
                "FROM paper.exit_plans p JOIN research.strategy_definitions d "
                "ON d.strategy_key=p.strategy_key JOIN paper.positions x "
                "ON x.position_id=p.position_id "
                "WHERE p.market=%s AND p.account_id=%s AND p.state='ACTIVE' AND x.state='OPEN' "
                "ORDER BY p.created_at,p.position_id",
                (market.value, account_id),
            )
            return tuple(self._from_row(row) for row in cursor.fetchall())
        finally:
            connection.close()

    def save_outcome(self, outcome: Outcome, reason: ExitReason) -> None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO paper.outcomes "
                "(outcome_id,position_id,market,account_id,symbol,strategy_key,opened_at,"
                "closed_at,gross_pnl,total_fees,net_pnl,return_on_allocated_capital,decision_ids,"
                "approval_ids,gold_snapshot_ids,agent_evidence_ids,recorded_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,"
                "%s::jsonb,%s) ON CONFLICT (position_id) DO NOTHING",
                (
                    outcome.outcome_id,
                    outcome.position_id,
                    outcome.market.value,
                    outcome.account_id,
                    outcome.symbol,
                    outcome.strategy_identity.key,
                    outcome.opened_at,
                    outcome.closed_at,
                    outcome.gross_pnl,
                    outcome.total_fees,
                    outcome.net_pnl,
                    outcome.return_on_allocated_capital,
                    json.dumps(outcome.decision_ids),
                    json.dumps(outcome.approval_ids),
                    json.dumps(outcome.gold_snapshot_ids),
                    json.dumps(outcome.agent_evidence_ids),
                    outcome.recorded_at,
                ),
            )
            cursor.execute(
                "UPDATE paper.exit_plans SET state='CLOSED',exit_reason=%s,closed_at=%s "
                "WHERE position_id=%s",
                (reason.value, outcome.closed_at, outcome.position_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> ExitPlan:
        raw_lineage = json.loads(row[16]) if isinstance(row[16], str) else row[16]
        identity = StrategyIdentity(
            Market(str(row[1])),
            str(row[11]),
            str(row[12]),
            str(row[13]),
            str(row[14]),
            int(cast(int, row[15])),
        )
        return ExitPlan(
            str(row[0]),
            Market(str(row[1])),
            str(row[2]),
            str(row[3]),
            AdvisoryAction(str(row[4])),
            float(cast(float, row[5])),
            float(cast(float, row[6])),
            float(cast(float, row[7])),
            float(cast(float, row[8])),
            str(row[9]),
            str(row[10]),
            identity,
            tuple(cast(list[str], raw_lineage)),
            cast(datetime, row[17]),
        )
