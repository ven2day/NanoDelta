"""Durable PostgreSQL adapter for strategy definitions, validations, and approvals.

Writes hydrate any prerequisite state this process has not seen yet, then delegate
to StrategyRegistry's own (already tested) in-memory invariant checks before
persisting. Reads used at decision time (require_approval, eligible) always query
PostgreSQL directly rather than trusting the in-memory cache, so a second replica's
approvals or an approval from an earlier process are never missed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any, cast

from nanodelta.contracts import Market
from nanodelta.persistence.migrations import Connection
from nanodelta.strategies.registry import (
    ApprovalState,
    StrategyApproval,
    StrategyDefinition,
    StrategyIdentity,
    StrategyRegistry,
)
from nanodelta.strategies.validation import ValidationMetrics, ValidationPolicy, ValidationResult


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class PostgresStrategyRegistry(StrategyRegistry):
    def __init__(self, connect: Callable[[], Connection]) -> None:
        super().__init__()
        self._connect = connect

    # -- writes -----------------------------------------------------------

    def register(self, definition: StrategyDefinition) -> None:
        connection = self._connect()
        try:
            self._hydrate_definition(connection, definition.identity)
            super().register(definition)
            connection.cursor().execute(
                "INSERT INTO research.strategy_definitions "
                "(strategy_key,market,strategy_id,strategy_version,timeframe,trade_horizon,"
                "feature_set_version,family,parameters,implementation_ref) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
                "ON CONFLICT (strategy_key) DO NOTHING",
                (
                    definition.identity.key,
                    definition.identity.market.value,
                    definition.identity.strategy_id,
                    definition.identity.strategy_version,
                    definition.identity.timeframe,
                    definition.identity.trade_horizon,
                    definition.identity.feature_set_version,
                    definition.family,
                    _dump(list(definition.parameters)),
                    definition.implementation_ref,
                ),
            )
            persisted = self._load_definition(connection, definition.identity)
            if persisted != definition:
                raise ValueError("strategy identity is immutable; publish a new version")
            self._definitions[definition.identity] = persisted
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_validation(self, result: ValidationResult) -> None:
        connection = self._connect()
        try:
            self._hydrate_definition(connection, result.identity)
            self._hydrate_validation(connection, result.validation_run_id)
            super().record_validation(result)
            connection.cursor().execute(
                "INSERT INTO research.validation_runs "
                "(validation_run_id,strategy_key,evaluated_at,passed,metrics,policy,"
                "rejection_reasons) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb) "
                "ON CONFLICT (validation_run_id) DO NOTHING",
                (
                    result.validation_run_id,
                    result.identity.key,
                    result.evaluated_at,
                    result.passed,
                    _dump(asdict(result.metrics)),
                    _dump(asdict(result.policy)),
                    _dump(list(result.rejection_reasons)),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_approval(self, approval: StrategyApproval) -> None:
        connection = self._connect()
        try:
            self._hydrate_definition(connection, approval.identity)
            self._hydrate_validation(connection, approval.validation_run_id)
            self._hydrate_approval(connection, approval.approval_id, force=True)
            super().record_approval(approval)
            self._upsert_approval(connection, approval)
            persisted = self._load_approval(connection, approval.approval_id)
            if persisted != approval:
                raise ValueError("approval artifacts are immutable")
            self._approvals[approval.approval_id] = persisted
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def revoke(self, approval_id: str, reason: str) -> None:
        connection = self._connect()
        try:
            self._hydrate_approval(connection, approval_id, force=True)
            super().revoke(approval_id, reason)
            self._upsert_approval(connection, self._approvals[approval_id])
            self._approvals[approval_id] = self._load_approval(connection, approval_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # -- authoritative reads ------------------------------------------------

    def require_approval(self, identity: StrategyIdentity, *, at: datetime) -> StrategyApproval:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT 1 FROM research.strategy_definitions WHERE strategy_key=%s",
                (identity.key,),
            )
            if cursor.fetchone() is None:
                raise LookupError("strategy identity is not registered")
            approvals = self._load_current_approvals(connection, identity, at=at)
            if not approvals:
                raise PermissionError("no current approval for exact strategy identity")
            return max(approvals, key=lambda approval: approval.approved_at)
        finally:
            connection.close()

    def eligible(
        self,
        *,
        market: Market,
        timeframe: str,
        trade_horizon: str,
        feature_set_version: int,
        at: datetime,
    ) -> tuple[StrategyDefinition, ...]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT DISTINCT d.strategy_key,d.market,d.strategy_id,d.strategy_version,"
                "d.timeframe,d.trade_horizon,d.feature_set_version,d.family,d.parameters,"
                "d.implementation_ref "
                "FROM research.strategy_definitions d "
                "JOIN research.strategy_approvals a ON a.strategy_key=d.strategy_key "
                "WHERE d.market=%s AND d.timeframe=%s AND d.trade_horizon=%s "
                "AND d.feature_set_version=%s AND a.state=%s "
                "AND a.approved_at<=%s AND a.expires_at>%s",
                (
                    market.value,
                    timeframe,
                    trade_horizon,
                    feature_set_version,
                    ApprovalState.APPROVED.value,
                    at,
                    at,
                ),
            )
            definitions = [self._definition_from_row(row) for row in cursor.fetchall()]
            return tuple(sorted(definitions, key=lambda item: item.identity))
        finally:
            connection.close()

    # -- hydration helpers (fill in-memory state this process hasn't seen) --

    def _hydrate_definition(self, connection: Connection, identity: StrategyIdentity) -> None:
        if identity in self._definitions:
            return
        cursor = connection.cursor()
        cursor.execute(
            "SELECT strategy_key,market,strategy_id,strategy_version,timeframe,trade_horizon,"
            "feature_set_version,family,parameters,implementation_ref "
            "FROM research.strategy_definitions WHERE strategy_key=%s",
            (identity.key,),
        )
        row = cursor.fetchone()
        if row is not None:
            self._definitions[identity] = self._definition_from_row(row)

    def _load_definition(
        self, connection: Connection, identity: StrategyIdentity
    ) -> StrategyDefinition:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT strategy_key,market,strategy_id,strategy_version,timeframe,trade_horizon,"
            "feature_set_version,family,parameters,implementation_ref "
            "FROM research.strategy_definitions WHERE strategy_key=%s",
            (identity.key,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("strategy definition insert did not persist")
        return self._definition_from_row(row)

    def _hydrate_validation(self, connection: Connection, validation_run_id: str) -> None:
        if validation_run_id in self._validations:
            return
        cursor = connection.cursor()
        cursor.execute(
            "SELECT v.validation_run_id,d.market,d.strategy_id,d.strategy_version,d.timeframe,"
            "d.trade_horizon,d.feature_set_version,v.evaluated_at,v.passed,v.metrics,v.policy,"
            "v.rejection_reasons "
            "FROM research.validation_runs v "
            "JOIN research.strategy_definitions d ON d.strategy_key=v.strategy_key "
            "WHERE v.validation_run_id=%s",
            (validation_run_id,),
        )
        row = cursor.fetchone()
        if row is not None:
            self._validations[validation_run_id] = self._validation_from_row(row)

    def _hydrate_approval(
        self, connection: Connection, approval_id: str, *, force: bool = False
    ) -> None:
        if not force and approval_id in self._approvals:
            return
        if force:
            self._approvals.pop(approval_id, None)
        try:
            self._approvals[approval_id] = self._load_approval(connection, approval_id)
        except LookupError:
            return

    def _load_approval(
        self, connection: Connection, approval_id: str
    ) -> StrategyApproval:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT a.approval_id,d.market,d.strategy_id,d.strategy_version,d.timeframe,"
            "d.trade_horizon,d.feature_set_version,a.validation_run_id,a.state,a.approved_at,"
            "a.expires_at,a.approved_by,a.reason "
            "FROM research.strategy_approvals a "
            "JOIN research.strategy_definitions d ON d.strategy_key=a.strategy_key "
            "WHERE a.approval_id=%s",
            (approval_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise LookupError("approval is not registered")
        return self._approval_from_row(row)

    def _load_current_approvals(
        self, connection: Connection, identity: StrategyIdentity, *, at: datetime
    ) -> list[StrategyApproval]:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT a.approval_id,d.market,d.strategy_id,d.strategy_version,d.timeframe,"
            "d.trade_horizon,d.feature_set_version,a.validation_run_id,a.state,a.approved_at,"
            "a.expires_at,a.approved_by,a.reason "
            "FROM research.strategy_approvals a "
            "JOIN research.strategy_definitions d ON d.strategy_key=a.strategy_key "
            "WHERE a.strategy_key=%s AND a.state=%s AND a.approved_at<=%s AND a.expires_at>%s",
            (identity.key, ApprovalState.APPROVED.value, at, at),
        )
        return [self._approval_from_row(row) for row in cursor.fetchall()]

    def _upsert_approval(self, connection: Connection, approval: StrategyApproval) -> None:
        connection.cursor().execute(
            "INSERT INTO research.strategy_approvals "
            "(approval_id,strategy_key,validation_run_id,state,approved_at,expires_at,"
            "approved_by,reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (approval_id) DO UPDATE SET state=EXCLUDED.state,reason=EXCLUDED.reason "
            "WHERE research.strategy_approvals.state<>%s OR EXCLUDED.state=%s",
            (
                approval.approval_id,
                approval.identity.key,
                approval.validation_run_id,
                approval.state.value,
                approval.approved_at,
                approval.expires_at,
                approval.approved_by,
                approval.reason,
                ApprovalState.REVOKED.value,
                ApprovalState.REVOKED.value,
            ),
        )

    # -- row -> dataclass -----------------------------------------------------

    @staticmethod
    def _identity_from_row(row: tuple[object, ...], *, offset: int) -> StrategyIdentity:
        return StrategyIdentity(
            Market(str(row[offset])),
            str(row[offset + 1]),
            str(row[offset + 2]),
            str(row[offset + 3]),
            str(row[offset + 4]),
            int(cast(int, row[offset + 5])),
        )

    @classmethod
    def _definition_from_row(cls, row: tuple[object, ...]) -> StrategyDefinition:
        parameters = row[8]
        if isinstance(parameters, str):
            parameters = json.loads(parameters)
        if isinstance(parameters, dict):
            parameter_items = tuple(sorted(cast(dict[str, str], parameters).items()))
        else:
            parameter_items = tuple(
                (str(item[0]), str(item[1]))
                for item in cast(list[list[object]], parameters)
            )
        return StrategyDefinition(
            cls._identity_from_row(row, offset=1),
            str(row[7]),
            parameter_items,
            str(row[9]),
        )

    @classmethod
    def _validation_from_row(cls, row: tuple[object, ...]) -> ValidationResult:
        metrics = row[9]
        policy = row[10]
        reasons = row[11]
        if isinstance(metrics, str):
            metrics = json.loads(metrics)
        if isinstance(policy, str):
            policy = json.loads(policy)
        if isinstance(reasons, str):
            reasons = json.loads(reasons)
        return ValidationResult(
            str(row[0]),
            cls._identity_from_row(row, offset=1),
            cast(datetime, row[7]),
            bool(row[8]),
            tuple(cast(list[str], reasons)),
            ValidationMetrics(**cast(dict[str, Any], metrics)),
            ValidationPolicy(**cast(dict[str, Any], policy)),
        )

    @classmethod
    def _approval_from_row(cls, row: tuple[object, ...]) -> StrategyApproval:
        return StrategyApproval(
            str(row[0]),
            cls._identity_from_row(row, offset=1),
            str(row[7]),
            ApprovalState(str(row[8])),
            cast(datetime, row[9]),
            cast(datetime, row[10]),
            str(row[11]),
            str(row[12]),
        )
