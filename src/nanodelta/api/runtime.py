"""Deployable API bootstrap with explicit liveness and database readiness."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.conninfo import make_conninfo

from nanodelta.api.app import ApiServices, create_app
from nanodelta.api.read_models import PostgresAuthoritativeReadStore
from nanodelta.operations import Actor, OperationalStore, RuntimeController


def _required_secret(path_variable: str) -> str:
    configured = os.environ.get(path_variable)
    if not configured:
        raise RuntimeError(f"{path_variable} is required")
    value = Path(configured).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{path_variable} points to an empty secret")
    return value


def _database_url() -> str | None:
    configured = os.environ.get("DATABASE_URL")
    if configured:
        return configured
    password_path = os.environ.get("POSTGRES_PASSWORD_FILE")
    host = os.environ.get("POSTGRES_HOST")
    if not password_path or not host:
        return None
    password = Path(password_path).read_text(encoding="utf-8").strip()
    return make_conninfo(
        host=host,
        dbname=os.environ.get("POSTGRES_DB", "nanodelta"),
        user=os.environ.get("POSTGRES_USER", "nanodelta"),
        password=password,
    )


def _api_keys() -> dict[str, Actor]:
    keys = {_required_secret("NANODELTA_ADMIN_API_KEY_FILE"): Actor("deployment-admin", "admin")}
    role_file = os.environ.get("NANODELTA_BACKEND_KEYS_PATH")
    if role_file:
        parsed = json.loads(Path(role_file).read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError("NANODELTA_BACKEND_KEYS_PATH must contain a JSON object")
        for role in ("viewer", "operator", "admin"):
            value = parsed.get(role)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(f"backend API key for {role} is required")
            keys[value] = Actor(f"ui-{role}", role)
    return keys


def build_app() -> FastAPI:
    operations = OperationalStore()
    database_url = _database_url()
    services = ApiServices(
        operations=operations,
        controller=RuntimeController(operations),
        history_engines={},
        history_jobs={},
        api_keys=_api_keys(),
        read_store=(
            PostgresAuthoritativeReadStore(database_url) if database_url is not None else None
        ),
    )
    application = create_app(services)

    @application.get("/health/live", include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @application.get("/health/ready", include_in_schema=False)
    def ready() -> dict[str, str]:
        database_url = _database_url()
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
