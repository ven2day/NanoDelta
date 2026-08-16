"""Durable runtime state and operational audit repository."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import cast

from nanodelta.contracts import Market
from nanodelta.operations.controller import (
    AuditRecord,
    Command,
    OperationalStore,
    WorkerState,
)
from nanodelta.persistence.migrations import Connection


class PostgresOperationalStore(OperationalStore):
    def __init__(self, connect: Callable[[], Connection]) -> None:
        super().__init__()
        self._connect = connect

    def worker_state(self, market: Market) -> WorkerState:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT state FROM control.runtime_workers WHERE market=%s",
                (market.value,),
            )
            row = cursor.fetchone()
            return WorkerState(str(row[0])) if row else super().worker_state(market)
        finally:
            connection.close()

    def set_worker_state(self, market: Market, state: WorkerState) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "INSERT INTO control.runtime_workers(market,state,updated_at) "
                "VALUES (%s,%s,now()) ON CONFLICT (market) DO UPDATE SET "
                "state=EXCLUDED.state,updated_at=EXCLUDED.updated_at",
                (market.value, state.value),
            )
            connection.commit()
            super().set_worker_state(market, state)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def audit_record(self, idempotency_key: str) -> AuditRecord | None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT audit_id,market,command,actor_id,previous_state,resulting_state,"
                "requested_at,detail FROM control.operational_audit "
                "WHERE idempotency_key=%s",
                (idempotency_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return AuditRecord(
                str(row[0]),
                idempotency_key,
                Market(str(row[1])),
                Command(str(row[2])),
                str(row[3]),
                WorkerState(str(row[4])),
                WorkerState(str(row[5])),
                cast(datetime, row[6]),
                str(row[7]),
            )
        finally:
            connection.close()

    def save_audit(self, record: AuditRecord) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "INSERT INTO control.operational_audit "
                "(audit_id,idempotency_key,market,command,actor_id,previous_state,"
                "resulting_state,requested_at,detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (
                    record.audit_id,
                    record.idempotency_key,
                    record.market.value,
                    record.command.value,
                    record.actor_id,
                    record.previous_state.value,
                    record.resulting_state.value,
                    record.requested_at,
                    record.detail,
                ),
            )
            connection.commit()
            super().save_audit(record)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_transition(self, record: AuditRecord) -> None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO control.operational_audit "
                "(audit_id,idempotency_key,market,command,actor_id,previous_state,"
                "resulting_state,requested_at,detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (
                    record.audit_id,
                    record.idempotency_key,
                    record.market.value,
                    record.command.value,
                    record.actor_id,
                    record.previous_state.value,
                    record.resulting_state.value,
                    record.requested_at,
                    record.detail,
                ),
            )
            cursor.execute(
                "INSERT INTO control.runtime_workers(market,state,updated_at) "
                "VALUES (%s,%s,now()) ON CONFLICT (market) DO UPDATE SET "
                "state=EXCLUDED.state,updated_at=EXCLUDED.updated_at",
                (record.market.value, record.resulting_state.value),
            )
            connection.commit()
            OperationalStore.set_worker_state(self, record.market, record.resulting_state)
            OperationalStore.save_audit(self, record)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_queued_command(self, record: AuditRecord) -> None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO control.operational_audit "
                "(audit_id,idempotency_key,market,command,actor_id,previous_state,"
                "resulting_state,requested_at,detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    record.audit_id,
                    record.idempotency_key,
                    record.market.value,
                    record.command.value,
                    record.actor_id,
                    record.previous_state.value,
                    record.resulting_state.value,
                    record.requested_at,
                    record.detail,
                ),
            )
            cursor.execute(
                "INSERT INTO control.runtime_command_queue "
                "(command_id,idempotency_key,market,command,requested_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    record.audit_id,
                    record.idempotency_key,
                    record.market.value,
                    record.command.value,
                    record.requested_at,
                ),
            )
            connection.commit()
            OperationalStore.save_audit(self, record)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def runtime_command_status(self, idempotency_key: str) -> dict[str, object] | None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT command_id,market,command,state,attempts,requested_at,claimed_at,"
                "completed_at,instance_id,last_error FROM control.runtime_command_queue "
                "WHERE idempotency_key=%s",
                (idempotency_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            names = (
                "command_id",
                "market",
                "command",
                "state",
                "attempts",
                "requested_at",
                "claimed_at",
                "completed_at",
                "instance_id",
                "last_error",
            )
            return dict(zip(names, row, strict=True))
        finally:
            connection.close()
