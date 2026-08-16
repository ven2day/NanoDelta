from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from nanodelta.contracts import Market
from nanodelta.operations import Actor, Command, PostgresOperationalStore, RuntimeController
from nanodelta.runtime.control import RuntimeCommand, RuntimeCommandConsumer


class Cursor:
    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((query, params))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class Connection:
    def __init__(self, cursor: Cursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> Cursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass


def test_api_controller_atomically_queues_command_without_fake_worker() -> None:
    connection = Connection(Cursor(row=None))
    store = PostgresOperationalStore(lambda: connection)  # type: ignore[arg-type]
    controller = RuntimeController(store, durable_commands=True)

    record = controller.command(
        Market.NSE,
        Command.START,
        Actor("operator-1", "operator"),
        idempotency_key="start-nse-1",
        confirmed=True,
        requested_at=datetime(2026, 8, 16, tzinfo=UTC),
    )

    queries = [query for query, _ in connection._cursor.calls]
    assert any("operational_audit" in query for query in queries)
    assert any("runtime_command_queue" in query for query in queries)
    assert connection.commits == 1
    assert record.resulting_state == record.previous_state


class Mailbox:
    def __init__(self, command: RuntimeCommand | None) -> None:
        self.command = command
        self.completions: list[tuple[str, str | None]] = []

    def claim(self) -> RuntimeCommand | None:
        command, self.command = self.command, None
        return command

    def complete(self, command_id: str, *, error: str | None = None) -> None:
        self.completions.append((command_id, error))


class Worker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def drain(self) -> None:
        self.calls.append("drain")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [(Command.START, "start"), (Command.STOP, "stop"), (Command.DRAIN, "drain")],
)
async def test_independent_runtime_consumes_each_durable_command(
    command: Command, expected: str
) -> None:
    queued = RuntimeCommand("command-1", Market.CRYPTO, command, datetime.now(UTC))
    mailbox = Mailbox(queued)
    worker = Worker()
    workers: dict[Market, Any] = {Market.CRYPTO: worker}

    consumed = await RuntimeCommandConsumer(workers, mailbox).process_one()  # type: ignore[arg-type]

    assert consumed is True
    assert worker.calls == [expected]
    assert mailbox.completions == [("command-1", None)]
