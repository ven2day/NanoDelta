"""Command-line entry point for applying NanoDelta database migrations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import cast

import psycopg

from nanodelta.persistence.migrations import Connection, MigrationRunner, load_migrations


def default_migration_directory() -> Path:
    return Path(__file__).parents[3] / "migrations"


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply NanoDelta TimescaleDB migrations")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="PostgreSQL DSN; defaults to DATABASE_URL",
    )
    parser.add_argument(
        "--migrations",
        type=Path,
        default=default_migration_directory(),
        help="directory containing ordered .sql migrations",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    migrations = load_migrations(args.migrations)
    if not migrations:
        parser.error(f"no migrations found in {args.migrations}")

    def connect() -> Connection:
        return cast(Connection, psycopg.connect(args.database_url))

    applied = MigrationRunner(connect).apply(migrations)
    if applied:
        print("Applied migrations: " + ", ".join(applied))
    else:
        print("Database is already up to date")


if __name__ == "__main__":
    main()
