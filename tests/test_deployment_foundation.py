from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nanodelta.api import runtime
from nanodelta.operations import PostgresOperationalStore


def configure_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "admin_api_key"
    secret.write_text("test-admin-key", encoding="utf-8")
    monkeypatch.setenv("NANODELTA_ADMIN_API_KEY_FILE", str(secret))


def test_runtime_requires_secret_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NANODELTA_ADMIN_API_KEY_FILE", raising=False)
    with pytest.raises(RuntimeError, match="NANODELTA_ADMIN_API_KEY_FILE is required"):
        runtime.build_app()


def test_liveness_is_independent_of_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_secret(monkeypatch, tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = TestClient(runtime.build_app()).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_fails_closed_without_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_secret(monkeypatch, tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = TestClient(runtime.build_app()).get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "DATABASE_URL is not configured"}


def test_build_app_wires_operations_through_postgres_operational_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_secret(monkeypatch, tmp_path)
    connects_used: list[object] = []

    class RecordingStore(PostgresOperationalStore):
        def __init__(self, connect: object) -> None:
            connects_used.append(connect)
            super().__init__(connect)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime, "PostgresOperationalStore", RecordingStore)
    runtime.build_app()
    assert connects_used == [runtime._connect]


def test_runtime_loads_role_scoped_backend_api_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_secret(monkeypatch, tmp_path)
    keys = tmp_path / "backend_keys.json"
    keys.write_text(
        '{"viewer":"viewer-key","operator":"operator-key","admin":"ui-admin-key"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("NANODELTA_BACKEND_KEYS_PATH", str(keys))

    actors = runtime._api_keys()

    assert actors["viewer-key"].role == "viewer"
    assert actors["operator-key"].role == "operator"
    assert actors["ui-admin-key"].role == "admin"


def test_build_finops_is_unconfigured_without_qwen_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    assert runtime._build_finops() == (None, None)


def configure_qwen_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "test-qwen-key")
    monkeypatch.setenv("QWEN_CHAT_COMPLETIONS_ENDPOINT", "https://example.invalid/v1/chat")
    monkeypatch.setenv("QWEN_BILLING_MODE", "subscription")
    monkeypatch.setenv("QWEN_DAILY_REQUEST_LIMIT", "500")
    monkeypatch.setenv("QWEN_DAILY_TOKEN_LIMIT", "1000000")
    monkeypatch.setenv("QWEN_DAILY_COST_LIMIT_USD", "0")
    monkeypatch.setenv("QWEN_SUBSCRIPTION_PLAN_ID", "coding-plan")
    monkeypatch.setenv("QWEN_SUBSCRIPTION_MONTHLY_FEE_USD", "19.90")
    monkeypatch.delenv("QWEN_SUBSCRIPTION_5H_REQUEST_LIMIT", raising=False)
    monkeypatch.delenv("QWEN_SUBSCRIPTION_WEEKLY_REQUEST_LIMIT", raising=False)
    monkeypatch.delenv("QWEN_SUBSCRIPTION_MONTHLY_REQUEST_LIMIT", raising=False)


def test_build_finops_builds_a_working_guard_and_gateway_for_subscription_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_qwen_subscription(monkeypatch)

    guard, gateway = runtime._build_finops()

    assert guard is not None and gateway is not None
    assert guard.provider == "qwen"
    assert guard.billing_mode.value == "SUBSCRIPTION"
    assert guard.subscription is not None
    assert guard.subscription.plan_id == "coding-plan"
    assert gateway.guard is guard
    assert gateway.api_key == "test-qwen-key"


def test_build_finops_requires_full_config_once_qwen_api_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_qwen_subscription(monkeypatch)
    monkeypatch.delenv("QWEN_DAILY_REQUEST_LIMIT", raising=False)

    with pytest.raises(RuntimeError, match="QWEN_DAILY_REQUEST_LIMIT is required"):
        runtime._build_finops()


def test_build_finops_rejects_payg_billing_mode_not_yet_wired_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_qwen_subscription(monkeypatch)
    monkeypatch.setenv("QWEN_BILLING_MODE", "payg")

    with pytest.raises(RuntimeError, match="PriceCatalog"):
        runtime._build_finops()


def test_build_app_exposes_finops_status_once_qwen_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_secret(monkeypatch, tmp_path)
    configure_qwen_subscription(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    client = TestClient(runtime.build_app())
    response = client.get("/api/finops", headers={"X-API-Key": "test-admin-key"})

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "qwen"
    assert body["billing_mode"] == "SUBSCRIPTION"
    assert body["kill_switch"] is False


def test_runtime_commands_fail_closed_without_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Before this store was wired to Postgres, this exact call would have silently
    # "succeeded" against throwaway in-memory state with no database at all.
    configure_secret(monkeypatch, tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(runtime.build_app())
    response = client.post(
        "/api/nse/runtime/start",
        json={"confirmed": True},
        headers={"X-API-Key": "test-admin-key", "Idempotency-Key": "test-key-1"},
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "DATABASE_URL is required"}
