"""Durable PostgreSQL adapter for the staged decision ledger."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from nanodelta.contracts import Market
from nanodelta.decisions import Decision, DecisionStage, DecisionStatus
from nanodelta.persistence.migrations import Connection


class PostgresDecisionLedger:
    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def append(self, decision: Decision) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "INSERT INTO control.decision_events "
                "(decision_id,cycle_id,market,symbol,timeframe,stage,status,reason_code,"
                "occurred_at,candidate_id,strategy_key,detail,metrics) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT (decision_id) DO NOTHING",
                (
                    decision.decision_id,
                    decision.cycle_id,
                    decision.market.value,
                    decision.symbol,
                    decision.timeframe,
                    decision.stage.value,
                    decision.status.value,
                    decision.reason_code,
                    decision.occurred_at,
                    decision.candidate_id,
                    decision.strategy_key,
                    decision.detail,
                    json.dumps(dict(decision.metrics), sort_keys=True, separators=(",", ":")),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def for_cycle(self, cycle_id: str) -> tuple[Decision, ...]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT decision_id,cycle_id,market,symbol,timeframe,stage,status,reason_code,"
                "occurred_at,candidate_id,strategy_key,detail,metrics "
                "FROM control.decision_events WHERE cycle_id=%s "
                "ORDER BY occurred_at,decision_id",
                (cycle_id,),
            )
            return tuple(self._from_row(row) for row in cursor.fetchall())
        finally:
            connection.close()

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> Decision:
        raw_metrics = row[12]
        if isinstance(raw_metrics, str):
            raw_metrics = json.loads(raw_metrics)
        metrics = tuple(
            sorted(
                (str(name), float(cast(Any, value)))
                for name, value in cast(dict[object, object], raw_metrics).items()
            )
        )
        return Decision(
            str(row[0]),
            str(row[1]),
            Market(str(row[2])),
            str(row[3]),
            str(row[4]) if row[4] is not None else None,
            DecisionStage(str(row[5])),
            DecisionStatus(str(row[6])),
            str(row[7]),
            cast(datetime, row[8]),
            str(row[9]) if row[9] is not None else None,
            str(row[10]) if row[10] is not None else None,
            str(row[11]),
            metrics,
        )
