"""PostgreSQL persistence for runtime leases and heartbeat snapshots."""

from __future__ import annotations

from collections.abc import Callable

from nanodelta.persistence.migrations import Connection
from nanodelta.runtime.supervisor import RuntimeStateStore, WorkerSnapshot


class PostgresRuntimeStateStore(RuntimeStateStore):
    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    async def save(self, snapshot: WorkerSnapshot) -> None:
        connection = self._connect()
        try:
            connection.cursor().execute(
                "INSERT INTO control.runtime_instances "
                "(market,instance_id,state,last_heartbeat,last_cycle_started,"
                "last_cycle_finished,last_error,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,now()) "
                "ON CONFLICT (market) DO UPDATE SET instance_id=EXCLUDED.instance_id,"
                "state=EXCLUDED.state,last_heartbeat=EXCLUDED.last_heartbeat,"
                "last_cycle_started=EXCLUDED.last_cycle_started,"
                "last_cycle_finished=EXCLUDED.last_cycle_finished,"
                "last_error=EXCLUDED.last_error,updated_at=now()",
                (
                    snapshot.market.value,
                    snapshot.instance_id,
                    snapshot.state.value,
                    snapshot.last_heartbeat,
                    snapshot.last_cycle_started,
                    snapshot.last_cycle_finished,
                    snapshot.last_error,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
