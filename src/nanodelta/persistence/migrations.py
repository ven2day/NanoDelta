"""Checksum-verified, advisory-locked SQL migration runner."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class Cursor(Protocol):
    def execute(self, query: str, params: tuple[object, ...] = ()) -> Any: ...

    def fetchone(self) -> tuple[object, ...] | None: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Migration:
    version: str
    sql: str
    checksum: str


def load_migrations(directory: Path) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=path.stem,
                sql=sql,
                checksum=hashlib.sha256(sql.encode()).hexdigest(),
            )
        )
    return tuple(migrations)


class MigrationRunner:
    _LOCK_ID = 6_641_002_024

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def apply(self, migrations: Iterable[Migration]) -> tuple[str, ...]:
        connection = self._connect()
        applied: list[str] = []
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT pg_advisory_lock(%s)", (self._LOCK_ID,))
            cursor.execute(
                "CREATE SCHEMA IF NOT EXISTS control;"
                "CREATE TABLE IF NOT EXISTS control.schema_migrations ("
                "version text PRIMARY KEY, checksum text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for migration in migrations:
                cursor.execute(
                    "SELECT checksum FROM control.schema_migrations WHERE version = %s",
                    (migration.version,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[0] != migration.checksum:
                        raise RuntimeError(f"migration checksum mismatch for {migration.version}")
                    continue
                cursor.execute(migration.sql)
                cursor.execute(
                    "INSERT INTO control.schema_migrations(version, checksum) VALUES (%s, %s)",
                    (migration.version, migration.checksum),
                )
                applied.append(migration.version)
            connection.commit()
            return tuple(applied)
        except Exception:
            connection.rollback()
            raise
        finally:
            try:
                connection.cursor().execute("SELECT pg_advisory_unlock(%s)", (self._LOCK_ID,))
            finally:
                connection.close()
