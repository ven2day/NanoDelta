from __future__ import annotations

from pathlib import Path

import pytest

from nanodelta.runtime.cli import _database_url


def test_database_url_prefers_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://explicit/database")
    assert _database_url() == "postgresql://explicit/database"


def test_database_url_fails_closed_when_configuration_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "DATABASE_URL",
        "POSTGRES_HOST",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="database configuration missing"):
        _database_url()


def test_database_url_reads_password_from_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "password"
    secret.write_text("complex password\n", encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_DB", "nanodelta")
    monkeypatch.setenv("POSTGRES_USER", "nanodelta")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(secret))

    value = _database_url()

    assert "host=db" in value
    assert "dbname=nanodelta" in value
    assert "password='complex password'" in value
