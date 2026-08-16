"""build_realtime_cycles' Dhan-token and TrueData resolution: which credential path
gets used, and that TrueData is genuinely optional (not every NSE deployment has an
account for it -- Dhan is its documented realtime fallback). DhanTokenProvider's own
PIN/TOTP generation logic is already covered by test_nse_universe_and_dhan_auth.py;
these tests are about the selection logic build_realtime_cycles added, not that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanodelta.providers.truedata import TrueDataClient
from nanodelta.runtime.realtime_config import _dhan_access_token, _truedata_client_or_none


async def test_dhan_access_token_prefers_a_manually_supplied_static_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "access_token"
    token_file.write_text("static-token-value", encoding="utf-8")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN_PATH", str(token_file))
    # Even if PIN/TOTP paths are also set, the static token takes precedence.
    monkeypatch.setenv("DHAN_PIN_PATH", str(tmp_path / "unused-pin"))
    monkeypatch.setenv("DHAN_TOTP_SECRET_PATH", str(tmp_path / "unused-totp"))

    assert await _dhan_access_token("client-1") == "static-token-value"


async def test_dhan_access_token_requires_some_credential_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DHAN_ACCESS_TOKEN_PATH", raising=False)
    monkeypatch.delenv("DHAN_PIN_PATH", raising=False)
    monkeypatch.delenv("DHAN_TOTP_SECRET_PATH", raising=False)

    with pytest.raises(RuntimeError, match="Dhan credentials are required"):
        await _dhan_access_token("client-1")


async def test_dhan_access_token_requires_both_pin_and_totp_paths_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DHAN_ACCESS_TOKEN_PATH", raising=False)
    monkeypatch.setenv("DHAN_PIN_PATH", str(tmp_path / "pin"))
    monkeypatch.delenv("DHAN_TOTP_SECRET_PATH", raising=False)

    with pytest.raises(RuntimeError, match="Dhan credentials are required"):
        await _dhan_access_token("client-1")


def test_truedata_client_or_none_returns_none_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRUEDATA_USERNAME", raising=False)
    assert _truedata_client_or_none() is None


def test_truedata_client_or_none_builds_a_client_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    password_file = tmp_path / "truedata_password"
    password_file.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("TRUEDATA_USERNAME", "operator")
    monkeypatch.setenv("TRUEDATA_PASSWORD_PATH", str(password_file))

    client = _truedata_client_or_none()

    assert isinstance(client, TrueDataClient)
