from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nanodelta.api import runtime


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
