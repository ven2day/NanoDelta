"""Deployable API bootstrap with explicit liveness and database readiness."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

import psycopg
from fastapi import FastAPI, HTTPException

from nanodelta.api.app import ApiServices, create_app
from nanodelta.api.read_models import PostgresAuthoritativeReadStore
from nanodelta.decisions_postgres import PostgresDecisionLedger
from nanodelta.finops import (
    BillingMode,
    BudgetPolicy,
    FinOpsGuard,
    HttpxQwenTransport,
    InMemoryFinOpsLedger,
    QwenFinOpsGateway,
    SubscriptionPlan,
)
from nanodelta.history.config import build_history_services
from nanodelta.observability import configure_json_logging
from nanodelta.operations import Actor, PostgresOperationalStore, RuntimeController
from nanodelta.persistence.migrations import Connection
from nanodelta.security import PostgresSecurityStore
from nanodelta.validation.postgres import PostgresNseValidationStore


def _required_secret(path_variable: str) -> str:
    configured = os.environ.get(path_variable)
    if not configured:
        raise RuntimeError(f"{path_variable} is required")
    value = Path(configured).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{path_variable} points to an empty secret")
    return value


def _connect() -> Connection:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(database_url)


def _api_keys() -> dict[str, Actor]:
    keys = {_required_secret("NANODELTA_ADMIN_API_KEY_FILE"): Actor("deployment-admin", "admin")}
    role_file = os.environ.get("NANODELTA_BACKEND_KEYS_PATH")
    if role_file is None:
        return keys
    try:
        parsed = json.loads(Path(role_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("NANODELTA_BACKEND_KEYS_PATH is unreadable or invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("NANODELTA_BACKEND_KEYS_PATH must contain a JSON object")
    for role in ("viewer", "operator", "admin"):
        value = parsed.get(role)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"backend API key for {role} is required")
        keys[value] = Actor(f"ui-{role}", role)
    return keys


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required when QWEN_API_KEY is set")
    return value


def _optional_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    return int(value) if value else None


def _build_finops() -> tuple[FinOpsGuard | None, QwenFinOpsGateway | None]:
    """Qwen/FinOps is entirely optional -- absent QWEN_API_KEY means "not configured",
    matching every other optional provider in this deployment (TrueData, OANDA static
    token). See docs/QWEN_FINOPS.md and env/.env.qwen.example for the full contract."""
    api_key = os.environ.get("QWEN_API_KEY", "").strip()
    if not api_key:
        return None, None
    endpoint = _required_env("QWEN_CHAT_COMPLETIONS_ENDPOINT")
    deployment_scope = os.environ.get("QWEN_DEPLOYMENT_SCOPE", "international").strip()
    billing_mode = BillingMode(_required_env("QWEN_BILLING_MODE").upper())
    if billing_mode is BillingMode.PAYG:
        raise RuntimeError(
            "QWEN_BILLING_MODE=PAYG requires a versioned PriceCatalog sourced from Qwen's "
            "official pricing page; only SUBSCRIPTION billing is wired up so far"
        )
    policy = BudgetPolicy(
        daily_request_limit=int(_required_env("QWEN_DAILY_REQUEST_LIMIT")),
        daily_token_limit=int(_required_env("QWEN_DAILY_TOKEN_LIMIT")),
        daily_cost_limit_usd=Decimal(_required_env("QWEN_DAILY_COST_LIMIT_USD")),
    )
    subscription = SubscriptionPlan(
        plan_id=_required_env("QWEN_SUBSCRIPTION_PLAN_ID"),
        monthly_fee_usd=Decimal(_required_env("QWEN_SUBSCRIPTION_MONTHLY_FEE_USD")),
        five_hour_request_limit=_optional_int("QWEN_SUBSCRIPTION_5H_REQUEST_LIMIT"),
        weekly_request_limit=_optional_int("QWEN_SUBSCRIPTION_WEEKLY_REQUEST_LIMIT"),
        monthly_request_limit=_optional_int("QWEN_SUBSCRIPTION_MONTHLY_REQUEST_LIMIT"),
    )
    guard = FinOpsGuard(
        provider="qwen",
        billing_mode=billing_mode,
        policy=policy,
        ledger=InMemoryFinOpsLedger(),
        subscription=subscription,
    )
    gateway = QwenFinOpsGateway(
        guard=guard,
        transport=HttpxQwenTransport(endpoint=endpoint),
        api_key=api_key,
        deployment_scope=deployment_scope,
    )
    return guard, gateway


T = TypeVar("T")


def _run_sync(coroutine: Coroutine[object, object, T]) -> T:
    """Run a one-shot startup coroutine to completion from synchronous code.

    build_app is a uvicorn --factory callable, invoked from *inside* uvicorn's own
    running event loop -- asyncio.run() would raise there. A dedicated thread gets
    its own fresh loop so this doesn't collide with it.
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def build_app() -> FastAPI:
    database_url = os.environ.get("DATABASE_URL")
    configure_json_logging()
    operations = PostgresOperationalStore(_connect)
    history_enabled = os.environ.get("NANODELTA_HISTORY_ENABLED", "false").lower() == "true"
    history_engines, history_jobs = (
        _run_sync(build_history_services(database_url))
        if history_enabled and database_url is not None
        else ({}, {})
    )
    finops_guard, qwen_gateway = _build_finops()
    services = ApiServices(
        operations=operations,
        controller=RuntimeController(operations, durable_commands=True),
        history_engines=history_engines,
        history_jobs=history_jobs,
        api_keys=_api_keys(),
        finops=finops_guard,
        qwen_gateway=qwen_gateway,
        decision_ledger=PostgresDecisionLedger(_connect),
        read_store=(
            PostgresAuthoritativeReadStore(database_url) if database_url is not None else None
        ),
        history_unavailable_reason=(
            None
            if history_enabled
            else "history operations are disabled; set NANODELTA_HISTORY_ENABLED=true"
        ),
        security=PostgresSecurityStore(_connect),
        nse_validation_reader=(
            PostgresNseValidationStore(_connect) if database_url is not None else None
        ),
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
