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


def test_runtime_loads_optional_least_privilege_api_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_secret(monkeypatch, tmp_path)
    reader = tmp_path / "read_api_key"
    operator = tmp_path / "operator_api_key"
    reader.write_text("reader-key", encoding="utf-8")
    operator.write_text("operator-key", encoding="utf-8")
    monkeypatch.setenv("NANODELTA_READ_API_KEY_FILE", str(reader))
    monkeypatch.setenv("NANODELTA_OPERATOR_API_KEY_FILE", str(operator))

    application = runtime.build_app()
    routes = {route.path for route in application.routes}

    assert "/api/overview" in routes
    assert runtime._optional_secret("NANODELTA_READ_API_KEY_FILE") == "reader-key"
    assert runtime._optional_secret("NANODELTA_OPERATOR_API_KEY_FILE") == "operator-key"


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
