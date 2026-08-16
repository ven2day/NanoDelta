from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanodelta.contracts import Market
from nanodelta.history.config import build_history_services


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("DHAN_ACCESS_TOKEN_PATH", "TRUEDATA_PASSWORD_PATH", "OANDA_ACCESS_TOKEN_PATH"):
        secret = tmp_path / name.lower()
        secret.write_text("secret", encoding="utf-8")
        monkeypatch.setenv(name, str(secret))
    monkeypatch.setenv("NSE_DHAN_SECURITY_IDS_JSON", json.dumps({"RELIANCE": "2885"}))
    monkeypatch.setenv("DHAN_CLIENT_ID", "client")
    monkeypatch.setenv("TRUEDATA_USERNAME", "user")
    monkeypatch.setenv("FOREX_SYMBOLS", "EUR_USD")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "account")
    monkeypatch.setenv("CRYPTO_SYMBOLS", "BTC_USDT")
    monkeypatch.setenv("NANODELTA_HISTORY_TIMEFRAMES", "5m,1h,1d")


def test_history_services_cover_configured_universe_without_provider_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    engines, jobs = build_history_services("postgresql://unused")

    assert set(engines) == set(Market)
    assert len(jobs) == 9
    assert (Market.NSE, "RELIANCE", "5m") in jobs
    assert (Market.FOREX, "EUR_USD", "1h") in jobs
    assert (Market.CRYPTO, "BTC_USDT", "1d") in jobs


def test_history_services_fail_closed_when_enabled_configuration_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "NSE_DHAN_SECURITY_IDS_JSON",
        "DHAN_CLIENT_ID",
        "TRUEDATA_USERNAME",
        "FOREX_SYMBOLS",
        "OANDA_ACCOUNT_ID",
        "CRYPTO_SYMBOLS",
        "NANODELTA_HISTORY_TIMEFRAMES",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="required when history operations are enabled"):
        build_history_services("postgresql://unused")
