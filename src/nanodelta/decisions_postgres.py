"""Durable PostgreSQL adapter for the staged decision ledger."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from nanodelta.contracts import Market
from nanodelta.decisions import Decision, DecisionStage, DecisionStatus, SignalCandidate
from nanodelta.persistence.migrations import Connection


class PostgresDecisionLedger:
    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def append_candidate(self, candidate: SignalCandidate, decision: Decision) -> None:
        if (
            decision.candidate_id != candidate.candidate_id
            or decision.cycle_id != candidate.cycle_id
        ):
            raise ValueError("signal decision does not match candidate identity")
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO control.signal_candidates "
                "(candidate_id,cycle_id,market,symbol,timeframe,strategy_key,approval_id,"
                "event_time,action,reference_price,stop_price,target_price,confidence,"
                "gold_snapshot_ids,evidence) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) "
                "ON CONFLICT (candidate_id) DO NOTHING",
                (
                    candidate.candidate_id,
                    candidate.cycle_id,
                    candidate.market.value,
                    candidate.symbol,
                    candidate.timeframe,
                    candidate.strategy_key,
                    candidate.approval_id,
                    candidate.event_time,
                    candidate.action.value,
                    candidate.reference_price,
                    candidate.stop_price,
                    candidate.target_price,
                    candidate.confidence,
                    json.dumps(candidate.gold_snapshot_ids, separators=(",", ":")),
                    json.dumps(dict(candidate.evidence), sort_keys=True, separators=(",", ":")),
                ),
            )
            cursor.execute(
                "INSERT INTO control.decision_events "
                "(decision_id,cycle_id,market,symbol,timeframe,stage,status,reason_code,"
                "occurred_at,candidate_id,strategy_key,detail,metrics) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT (decision_id) DO NOTHING",
                self._decision_values(decision),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append(self, decision: Decision) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "INSERT INTO control.decision_events "
                "(decision_id,cycle_id,market,symbol,timeframe,stage,status,reason_code,"
                "occurred_at,candidate_id,strategy_key,detail,metrics) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT (decision_id) DO NOTHING",
                self._decision_values(decision),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _decision_values(decision: Decision) -> tuple[object, ...]:
        return (
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
        )

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
