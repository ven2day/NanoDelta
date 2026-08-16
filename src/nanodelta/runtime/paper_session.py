"""Durable, replay-safe orchestration for the continuous NSE paper session."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from uuid import uuid4

from nanodelta.contracts import FeatureRecord, Market, stable_id, utc
from nanodelta.persistence.migrations import Connection
from nanodelta.runtime.paper_decision import PaperDecisionResult

LOG = logging.getLogger(__name__)


class PaperSessionClaimState(StrEnum):
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    BUSY = "BUSY"


@dataclass(frozen=True)
class PaperSessionClaim:
    state: PaperSessionClaimState
    session_cycle_id: str
    evaluated_at: datetime
    claim_token: str | None = None
    prior_result: PaperDecisionResult | None = None


@dataclass(frozen=True)
class PaperSessionRun:
    session_cycle_id: str
    state: PaperSessionClaimState
    decision: PaperDecisionResult | None


@dataclass(frozen=True)
class PaperSessionHealth:
    processed: int = 0
    replayed: int = 0
    busy: int = 0
    failed: int = 0
    recovered_exit_plans: int = 0
    last_session_cycle_id: str | None = None
    last_completed_at: datetime | None = None
    last_error_type: str | None = None


class PaperDecisionProcessor(Protocol):
    def process(
        self, features: tuple[FeatureRecord, ...], *, evaluated_at: datetime | None = None
    ) -> PaperDecisionResult | None: ...


class PaperSessionStore(Protocol):
    def claim(
        self,
        *,
        session_cycle_id: str,
        feature_record_ids: tuple[str, ...],
        event_time: datetime,
        claimed_at: datetime,
    ) -> PaperSessionClaim: ...

    def reconcile_exit_plans(self, *, account_id: str) -> int: ...

    def retryable_features(
        self, *, as_of: datetime, limit: int = 10
    ) -> tuple[tuple[FeatureRecord, ...], ...]: ...

    def complete(
        self, claim: PaperSessionClaim, result: PaperDecisionResult, *, completed_at: datetime
    ) -> None: ...

    def fail(self, claim: PaperSessionClaim, *, failed_at: datetime, error_type: str) -> None: ...


@dataclass
class _MemoryCycle:
    evaluated_at: datetime
    state: str = "PENDING"
    claim_token: str | None = None
    locked_until: datetime | None = None
    result: PaperDecisionResult | None = None


class MemoryPaperSessionStore:
    """Deterministic test adapter mirroring the PostgreSQL lease contract."""

    def __init__(self, lease_seconds: float = 300) -> None:
        self._lease = timedelta(seconds=lease_seconds)
        self.cycles: dict[str, _MemoryCycle] = {}

    def claim(
        self,
        *,
        session_cycle_id: str,
        feature_record_ids: tuple[str, ...],
        event_time: datetime,
        claimed_at: datetime,
    ) -> PaperSessionClaim:
        del feature_record_ids, event_time
        cycle = self.cycles.setdefault(session_cycle_id, _MemoryCycle(claimed_at))
        if cycle.state == "COMPLETED":
            return PaperSessionClaim(
                PaperSessionClaimState.COMPLETED,
                session_cycle_id,
                cycle.evaluated_at,
                prior_result=cycle.result,
            )
        if cycle.state == "RUNNING" and cycle.locked_until is not None:
            if cycle.locked_until > claimed_at:
                return PaperSessionClaim(
                    PaperSessionClaimState.BUSY, session_cycle_id, cycle.evaluated_at
                )
        token = uuid4().hex
        cycle.state = "RUNNING"
        cycle.claim_token = token
        cycle.locked_until = claimed_at + self._lease
        return PaperSessionClaim(
            PaperSessionClaimState.CLAIMED, session_cycle_id, cycle.evaluated_at, token
        )

    def reconcile_exit_plans(self, *, account_id: str) -> int:
        del account_id
        return 0

    def retryable_features(
        self, *, as_of: datetime, limit: int = 10
    ) -> tuple[tuple[FeatureRecord, ...], ...]:
        del as_of, limit
        return ()

    def complete(
        self, claim: PaperSessionClaim, result: PaperDecisionResult, *, completed_at: datetime
    ) -> None:
        del completed_at
        cycle = self.cycles[claim.session_cycle_id]
        if cycle.claim_token != claim.claim_token:
            raise RuntimeError("paper session claim was lost before completion")
        cycle.state = "COMPLETED"
        cycle.locked_until = None
        cycle.result = result

    def fail(self, claim: PaperSessionClaim, *, failed_at: datetime, error_type: str) -> None:
        del failed_at, error_type
        cycle = self.cycles[claim.session_cycle_id]
        if cycle.claim_token == claim.claim_token:
            cycle.state = "FAILED"
            cycle.locked_until = None


class PostgresPaperSessionStore:
    def __init__(self, connect: Callable[[], Connection], *, lease_seconds: float = 300) -> None:
        if lease_seconds <= 0:
            raise ValueError("paper session lease must be positive")
        self._connect = connect
        self._lease = timedelta(seconds=lease_seconds)

    def claim(
        self,
        *,
        session_cycle_id: str,
        feature_record_ids: tuple[str, ...],
        event_time: datetime,
        claimed_at: datetime,
    ) -> PaperSessionClaim:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"nse-paper-session:{session_cycle_id}",),
            )
            cursor.execute(
                "INSERT INTO control.paper_session_cycles "
                "(session_cycle_id,market,feature_record_ids,event_time,evaluated_at,state) "
                "VALUES (%s,'nse',%s::jsonb,%s,%s,'PENDING') "
                "ON CONFLICT (session_cycle_id) DO NOTHING",
                (session_cycle_id, json.dumps(feature_record_ids), event_time, claimed_at),
            )
            cursor.execute(
                "SELECT evaluated_at,state,locked_until,candidate_count,allocation_count,"
                "risk_decision_count,order_count,exit_count,decision_cycle_id,cycle_mode "
                "FROM control.paper_session_cycles WHERE session_cycle_id=%s FOR UPDATE",
                (session_cycle_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("paper session cycle disappeared while claiming it")
            evaluated_at = utc(row[0], "evaluated_at")  # type: ignore[arg-type]
            state = str(row[1])
            locked_until = cast(datetime | None, row[2])
            if state == "COMPLETED":
                prior = self._result_from_row(row)
                connection.commit()
                return PaperSessionClaim(
                    PaperSessionClaimState.COMPLETED,
                    session_cycle_id,
                    evaluated_at,
                    prior_result=prior,
                )
            if state == "RUNNING" and locked_until is not None and locked_until > claimed_at:
                connection.commit()
                return PaperSessionClaim(
                    PaperSessionClaimState.BUSY, session_cycle_id, evaluated_at
                )
            token = uuid4().hex
            cursor.execute(
                "UPDATE control.paper_session_cycles SET state='RUNNING',claim_token=%s,"
                "locked_until=%s,attempt_count=attempt_count+1,last_error_type=NULL,updated_at=%s "
                "WHERE session_cycle_id=%s",
                (token, claimed_at + self._lease, claimed_at, session_cycle_id),
            )
            connection.commit()
            return PaperSessionClaim(
                PaperSessionClaimState.CLAIMED, session_cycle_id, evaluated_at, token
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reconcile_exit_plans(self, *, account_id: str) -> int:
        """Repair the only cross-transaction crash window in entry processing.

        A paper fill is durable before its protective exit plan is registered. If the
        process dies in that narrow window, rebuild the plan from immutable candidate,
        risk-decision and position lineage before processing another mark.
        """
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO paper.exit_plans "
                "(position_id,market,account_id,symbol,entry_action,quantity,stop_price,"
                "target_price,allocated_capital,candidate_id,approval_id,strategy_key,"
                "gold_snapshot_ids,state,created_at) "
                "SELECT p.position_id,p.market,p.account_id,p.symbol,"
                "CASE WHEN p.signed_quantity>0 THEN 'BUY' ELSE 'SELL' END,"
                "abs(p.signed_quantity),source.stop_price,source.target_price,"
                "abs(p.signed_quantity)*p.average_entry_price+p.total_fees,"
                "source.candidate_id,source.approval_id,source.strategy_key,p.gold_snapshot_ids,"
                "'ACTIVE',p.opened_at FROM paper.positions p "
                "JOIN LATERAL (SELECT c.candidate_id,c.approval_id,c.strategy_key,c.stop_price,"
                "c.target_price FROM jsonb_array_elements_text(p.decision_ids) ids(decision_id) "
                "JOIN paper.decisions d ON d.decision_id=ids.decision_id "
                "JOIN control.signal_candidates c ON c.candidate_id=d.candidate_id "
                "ORDER BY d.evaluated_at, d.decision_id LIMIT 1) source ON true "
                "WHERE p.market='nse' AND p.account_id=%s AND p.state='OPEN' "
                "AND NOT EXISTS (SELECT 1 FROM paper.exit_plans x "
                "WHERE x.position_id=p.position_id) ON CONFLICT (position_id) DO NOTHING",
                (account_id,),
            )
            recovered = max(0, int(getattr(cursor, "rowcount", 0)))
            connection.commit()
            return recovered
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def retryable_features(
        self, *, as_of: datetime, limit: int = 10
    ) -> tuple[tuple[FeatureRecord, ...], ...]:
        if limit <= 0:
            raise ValueError("paper session retry limit must be positive")
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT s.session_cycle_id,g.record_id,g.candle_record_id,g.symbol,g.timeframe,"
                "g.event_time,g.feature_version,g.features FROM "
                "(SELECT * FROM control.paper_session_cycles "
                "WHERE state='FAILED' OR (state='RUNNING' AND locked_until<=%s) "
                "ORDER BY event_time,session_cycle_id LIMIT %s) s "
                "CROSS JOIN LATERAL jsonb_array_elements_text(s.feature_record_ids) ids(record_id) "
                "JOIN nse_gold.feature_snapshots g ON g.record_id=ids.record_id "
                "ORDER BY s.event_time,s.session_cycle_id,g.record_id",
                (as_of, limit),
            )
            grouped: dict[str, list[FeatureRecord]] = {}
            for row in cursor.fetchall():
                raw_features = json.loads(row[7]) if isinstance(row[7], str) else row[7]
                values = cast(dict[str, object], raw_features)
                grouped.setdefault(str(row[0]), []).append(
                    FeatureRecord(
                        str(row[1]),
                        str(row[2]),
                        Market.NSE,
                        str(row[3]),
                        str(row[4]),
                        cast(datetime, row[5]),
                        float(cast(float, values["close"])),
                        float(cast(float, values["return_1"])),
                        float(cast(float, values["range_pct"])),
                        float(cast(float, values["body_pct"])),
                        (
                            None
                            if values.get("volume_change") is None
                            else float(cast(float, values["volume_change"]))
                        ),
                        int(cast(int, row[6])),
                    )
                )
            return tuple(tuple(features) for features in grouped.values())
        finally:
            connection.close()

    def complete(
        self, claim: PaperSessionClaim, result: PaperDecisionResult, *, completed_at: datetime
    ) -> None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE control.paper_session_cycles SET state='COMPLETED',locked_until=NULL,"
                "decision_cycle_id=%s,cycle_mode=%s,candidate_count=%s,allocation_count=%s,"
                "risk_decision_count=%s,order_count=%s,exit_count=%s,finished_at=%s,updated_at=%s "
                "WHERE session_cycle_id=%s AND state='RUNNING' AND claim_token=%s",
                (
                    result.cycle_id,
                    result.mode.value,
                    result.candidate_count,
                    result.allocation_count,
                    result.risk_decision_count,
                    result.order_count,
                    result.exit_count,
                    completed_at,
                    completed_at,
                    claim.session_cycle_id,
                    claim.claim_token,
                ),
            )
            if getattr(cursor, "rowcount", 1) != 1:
                raise RuntimeError("paper session claim was lost before completion")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail(self, claim: PaperSessionClaim, *, failed_at: datetime, error_type: str) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "UPDATE control.paper_session_cycles SET state='FAILED',locked_until=NULL,"
                "last_error_type=%s,updated_at=%s WHERE session_cycle_id=%s "
                "AND state='RUNNING' AND claim_token=%s",
                (error_type[:200], failed_at, claim.session_cycle_id, claim.claim_token),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _result_from_row(row: tuple[object, ...]) -> PaperDecisionResult | None:
        from nanodelta.orchestration.decision_pipeline import CycleMode

        decision_cycle_id = row[8]
        if decision_cycle_id is None:
            return None
        return PaperDecisionResult(
            Market.NSE,
            str(decision_cycle_id),
            CycleMode(str(row[9])),
            int(cast(int, row[3])),
            int(cast(int, row[4])),
            int(cast(int, row[5])),
            int(cast(int, row[6])),
            int(cast(int, row[7])),
        )


class ContinuousNsePaperSession:
    """Processes each settled NSE Gold input once, while replaying failures safely."""

    def __init__(
        self,
        *,
        processor: PaperDecisionProcessor,
        store: PaperSessionStore,
        account_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not account_id.strip():
            raise ValueError("paper session account_id is required")
        self._processor = processor
        self._store = store
        self._account_id = account_id
        self._clock = clock
        self.health = PaperSessionHealth()

    def process(self, features: tuple[FeatureRecord, ...]) -> PaperSessionRun | None:
        if not features:
            return None
        if {feature.market for feature in features} != {Market.NSE}:
            raise ValueError("continuous NSE paper session only accepts NSE Gold features")
        run = self._process_one(features)
        current_ids = frozenset(feature.record_id for feature in features)
        for retry in self._store.retryable_features(as_of=utc(self._clock(), "clock")):
            if frozenset(feature.record_id for feature in retry) == current_ids:
                continue
            try:
                self._process_one(retry)
            except Exception:
                # The failed row and error class are durable. Do not turn a decision
                # retry into a provider failure or prevent a newer Gold cycle.
                continue
        return run

    def _process_one(self, features: tuple[FeatureRecord, ...]) -> PaperSessionRun:
        event_time = max(utc(feature.event_time, "feature.event_time") for feature in features)
        record_ids = tuple(sorted(feature.record_id for feature in features))
        session_cycle_id = stable_id("continuous-nse-paper-session", self._account_id, record_ids)
        now = utc(self._clock(), "clock")
        claim = self._store.claim(
            session_cycle_id=session_cycle_id,
            feature_record_ids=record_ids,
            event_time=event_time,
            claimed_at=now,
        )
        if claim.state is PaperSessionClaimState.COMPLETED:
            self.health = replace(
                self.health,
                replayed=self.health.replayed + 1,
                last_session_cycle_id=session_cycle_id,
            )
            return PaperSessionRun(session_cycle_id, claim.state, claim.prior_result)
        if claim.state is PaperSessionClaimState.BUSY:
            self.health = replace(
                self.health,
                busy=self.health.busy + 1,
                last_session_cycle_id=session_cycle_id,
            )
            return PaperSessionRun(session_cycle_id, claim.state, None)
        try:
            recovered = self._store.reconcile_exit_plans(account_id=self._account_id)
            decision = self._processor.process(features, evaluated_at=claim.evaluated_at)
            if decision is None:
                raise RuntimeError("non-empty NSE features produced no paper decision result")
            completed_at = utc(self._clock(), "clock")
            self._store.complete(claim, decision, completed_at=completed_at)
            self.health = PaperSessionHealth(
                processed=self.health.processed + 1,
                replayed=self.health.replayed,
                busy=self.health.busy,
                failed=self.health.failed,
                recovered_exit_plans=self.health.recovered_exit_plans + recovered,
                last_session_cycle_id=session_cycle_id,
                last_completed_at=completed_at,
                last_error_type=None,
            )
            LOG.info(
                "continuous NSE paper cycle completed",
                extra={"event": "paper_cycle_completed", "market": Market.NSE.value},
            )
            return PaperSessionRun(session_cycle_id, claim.state, decision)
        except Exception as exc:
            error_type = type(exc).__name__
            failed_at = utc(self._clock(), "clock")
            self._store.fail(claim, failed_at=failed_at, error_type=error_type)
            self.health = PaperSessionHealth(
                processed=self.health.processed,
                replayed=self.health.replayed,
                busy=self.health.busy,
                failed=self.health.failed + 1,
                recovered_exit_plans=self.health.recovered_exit_plans,
                last_session_cycle_id=session_cycle_id,
                last_completed_at=self.health.last_completed_at,
                last_error_type=error_type,
            )
            LOG.error(
                "continuous NSE paper cycle failed",
                extra={"event": "paper_cycle_failed", "market": Market.NSE.value},
            )
            raise
