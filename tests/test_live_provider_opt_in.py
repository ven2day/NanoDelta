"""Credentialed provider checks; collected safely and skipped by default."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nanodelta.providers import DhanClient, OandaClient, OkxClient, PoloniexClient, TrueDataClient
from nanodelta.providers.base import HistoricalRequest


def _enabled() -> None:
    if os.environ.get("NANODELTA_LIVE_PROVIDER_TESTS") != "1":
        pytest.skip("set NANODELTA_LIVE_PROVIDER_TESTS=1 for controlled external calls")


def _secret_path(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} secret path is not configured")
    path = Path(value)
    if not path.is_file():
        pytest.skip(f"{name} does not reference a readable file")
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        pytest.skip(f"{name} secret file is empty")
    return secret


def _request(symbol: str) -> HistoricalRequest:
    end = datetime.now(UTC) - timedelta(days=1)
    return HistoricalRequest(symbol, "15m", end - timedelta(days=7), end, 5)


@pytest.mark.asyncio
async def test_live_dhan_history_requires_explicit_secret_paths() -> None:
    _enabled()
    client_id = _secret_path("DHAN_CLIENT_ID_PATH")
    token = _secret_path("DHAN_ACCESS_TOKEN_PATH")
    security_id = _secret_path("DHAN_TEST_SECURITY_ID_PATH")
    rows = await DhanClient(
        client_id=client_id, access_token=token, security_id=security_id
    ).fetch_candles(_request(os.environ.get("DHAN_TEST_SYMBOL", "RELIANCE")))
    assert rows


@pytest.mark.asyncio
async def test_live_truedata_history_requires_explicit_secret_paths() -> None:
    _enabled()
    username = _secret_path("TRUEDATA_USERNAME_PATH")
    password = _secret_path("TRUEDATA_PASSWORD_PATH")
    rows = await TrueDataClient(username=username, password=password).fetch_candles(
        _request(os.environ.get("TRUEDATA_TEST_SYMBOL", "RELIANCE"))
    )
    assert rows


@pytest.mark.asyncio
async def test_live_oanda_practice_history_requires_explicit_secret_paths() -> None:
    _enabled()
    account = _secret_path("OANDA_ACCOUNT_ID_PATH")
    token = _secret_path("OANDA_ACCESS_TOKEN_PATH")
    rows = await OandaClient(account_id=account, access_token=token, practice=True).fetch_candles(
        _request(os.environ.get("OANDA_TEST_SYMBOL", "EUR_USD"))
    )
    assert rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    (OkxClient(), PoloniexClient()),
    ids=("okx", "poloniex"),
)
async def test_live_public_crypto_history_still_requires_opt_in(client: object) -> None:
    _enabled()
    rows = await client.fetch_candles(_request("BTC_USDT"))  # type: ignore[attr-defined]
    assert rows


@pytest.mark.asyncio
async def test_live_oanda_pricing_stream_requires_explicit_secret_paths() -> None:
    _enabled()
    account = _secret_path("OANDA_ACCOUNT_ID_PATH")
    token = _secret_path("OANDA_ACCESS_TOKEN_PATH")
    client = OandaClient(account_id=account, access_token=token, practice=True)
    async with asyncio.timeout(20):
        payload = await anext(
            client.stream([os.environ.get("OANDA_TEST_SYMBOL", "EUR_USD")], "pricing")
        )
    assert payload["type"] == "PRICE"


@pytest.mark.asyncio
async def test_live_okx_ticker_stream_still_requires_explicit_opt_in() -> None:
    _enabled()
    async with asyncio.timeout(20):
        payload = await anext(OkxClient().stream(["BTC_USDT"], "tickers"))
    assert payload["arg"]["instId"] == "BTC-USDT"
