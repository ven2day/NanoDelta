"""Durable command consumption for independently deployed market workers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from nanodelta.contracts import Market
from nanodelta.operations import Command
from nanodelta.persistence.migrations import Connection
from nanodelta.runtime.supervisor import MarketWorker


@dataclass(frozen=True)
class RuntimeCommand:
    command_id: str
    market: Market
    command: Command
    requested_at: datetime


class PostgresRuntimeCommandMailbox:
    def __init__(self, connect: Callable[[], Connection], instance_id: str) -> None:
        self._connect = connect
        self._instance_id = instance_id

    def claim(self) -> RuntimeCommand | None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT command_id,market,command,requested_at "
                "FROM control.runtime_command_queue WHERE state='PENDING' "
                "ORDER BY requested_at FOR UPDATE SKIP LOCKED LIMIT 1"
            )
            row = cursor.fetchone()
            if row is None:
                connection.commit()
                return None
            cursor.execute(
                "UPDATE control.runtime_command_queue SET state='RUNNING',"
                "attempts=attempts+1,claimed_at=now(),instance_id=%s "
                "WHERE command_id=%s",
                (self._instance_id, str(row[0])),
            )
            connection.commit()
            return RuntimeCommand(
                str(row[0]),
                Market(str(row[1])),
                Command(str(row[2])),
                cast(datetime, row[3]),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(self, command_id: str, *, error: str | None = None) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "UPDATE control.runtime_command_queue SET state=%s,completed_at=now(),"
                "last_error=%s WHERE command_id=%s AND instance_id=%s",
                (
                    "FAILED" if error is not None else "SUCCEEDED",
                    error,
                    command_id,
                    self._instance_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class RuntimeCommandConsumer:
    def __init__(
        self,
        workers: Mapping[Market, MarketWorker],
        mailbox: PostgresRuntimeCommandMailbox,
        *,
        transition_timeout_seconds: float = 30,
    ) -> None:
        if transition_timeout_seconds <= 0:
            raise ValueError("runtime command transition timeout must be positive")
        self._workers = dict(workers)
        self._mailbox = mailbox
        self._transition_timeout = transition_timeout_seconds

    async def process_one(self) -> bool:
        queued = self._mailbox.claim()
        if queued is None:
            return False
        worker = self._workers[queued.market]
        try:
            async with asyncio.timeout(self._transition_timeout):
                if queued.command is Command.START:
                    await worker.start()
                elif queued.command is Command.STOP:
                    await worker.stop()
                elif queued.command is Command.DRAIN:
                    await worker.drain()
                else:
                    raise RuntimeError(f"unsupported runtime command: {queued.command.value}")
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                await worker.cancel()
            self._mailbox.complete(queued.command_id, error=f"{type(exc).__name__}: {exc}")
        else:
            self._mailbox.complete(queued.command_id)
        return True
