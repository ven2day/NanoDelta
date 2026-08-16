"""Deployable API bootstrap with explicit liveness and database readiness."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException

from nanodelta.api.app import ApiServices, create_app
from nanodelta.observability import configure_json_logging
from nanodelta.operations import Actor, PostgresOperationalStore, RuntimeController
from nanodelta.persistence.migrations import Connection


def _required_secret(path_variable: str) -> str:
    configured = os.environ.get(path_variable)
    if not configured:
        raise RuntimeError(f"{path_variable} is required")
    value = Path(configured).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{path_variable} points to an empty secret")
    return value


def _optional_secret(path_variable: str) -> str | None:
    configured = os.environ.get(path_variable)
    if not configured:
        return None
    value = Path(configured).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{path_variable} points to an empty secret")
    return value


def _connect() -> Connection:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(database_url)


def build_app() -> FastAPI:
    configure_json_logging()
    operations = PostgresOperationalStore(_connect)
    api_keys = {
        _required_secret("NANODELTA_ADMIN_API_KEY_FILE"): Actor(
            "deployment-admin", "admin"
        )
    }
    optional_roles = (
        ("NANODELTA_OPERATOR_API_KEY_FILE", "deployment-operator", "operator"),
        ("NANODELTA_READ_API_KEY_FILE", "deployment-reader", "read"),
    )
    for variable, actor_id, role in optional_roles:
        if key := _optional_secret(variable):
            api_keys[key] = Actor(actor_id, role)
    services = ApiServices(
        operations=operations,
        controller=RuntimeController(operations),
        history_engines={},
        history_jobs={},
        api_keys=api_keys,
    )
    application = create_app(services)

    @application.get("/health/live", include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @application.get("/health/ready", include_in_schema=False)
    def ready() -> dict[str, str]:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            with psycopg.connect(database_url, connect_timeout=3) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
        except psycopg.Error as exc:
            raise HTTPException(status_code=503, detail="database is unavailable") from exc
        return {"status": "ready"}

    return application
