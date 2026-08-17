from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nanodelta.contracts import Provider
from nanodelta.providers import DhanClient, DhanSecretFiles, DhanTokenProvider, generate_totp
from nanodelta.providers.base import HistoricalRequest, HttpRequest
from nanodelta.universe import DhanInstrumentMaster, DhanNseUniverseBuilder, load_nse_symbols

NOW = datetime(2026, 8, 15, 10, tzinfo=UTC)


class FakeJsonTransport:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[HttpRequest] = []

    async def request(self, request: HttpRequest) -> Any:
        self.requests.append(request)
        return self.response


class FakeTextTransport:
    def __init__(self, text: str) -> None:
        self.text = text
        self.urls: list[str] = []

    async def get_text(self, url: str) -> str:
        self.urls.append(url)
        return self.text


def write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def test_totp_matches_rfc_6238_sha1_vector() -> None:
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert generate_totp(secret, at=datetime.fromtimestamp(59, tz=UTC), digits=8) == "94287082"


@pytest.mark.asyncio
async def test_dhan_token_uses_secret_paths_caches_and_hides_credentials(tmp_path: Path) -> None:
    secrets = DhanSecretFiles(
        write(tmp_path / "pin", "123456\n"),
        write(tmp_path / "totp", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ\n"),
    )
    transport = FakeJsonTransport(
        {"accessToken": "jwt-secret", "expiryTime": "2026-08-16T15:30:00+05:30"}
    )
    provider = DhanTokenProvider(client_id="1000000001", secrets=secrets, transport=transport)

    first = await provider.token(now=NOW)
    second = await provider.token(now=NOW)

    assert second is first
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.params["pin"] == "123456"
    assert len(str(request.params["totp"])) == 6
    assert "123456" not in repr(request)
    assert "jwt-secret" not in repr(first)
    assert "GEZDG" not in repr(secrets)


def test_symbols_csv_validates_duplicates_and_unsupported_timeframes(tmp_path: Path) -> None:
    valid = write(
        tmp_path / "symbols.csv",
        "symbol,security_id,timeframes,enabled\nRELIANCE,1333,5m|15m,true\nTCS,,1h,false\n",
    )
    assert load_nse_symbols(valid)[0].symbol == "RELIANCE"

    invalid = write(
        tmp_path / "bad.csv",
        "symbol,security_id,timeframes\nRELIANCE,1333,2h\n",
    )
    with pytest.raises(ValueError, match="does not support"):
        load_nse_symbols(invalid)


@pytest.mark.asyncio
async def test_dhan_derives_session_aligned_30m_from_complete_15m_pairs() -> None:
    # 03:45 and 04:00 UTC are 09:15 and 09:30 IST.
    first = int(datetime(2026, 8, 14, 3, 45, tzinfo=UTC).timestamp())
    transport = FakeJsonTransport(
        {
            "timestamp": [first, first + 900],
            "open": [100, 102],
            "high": [103, 106],
            "low": [99, 101],
            "close": [102, 105],
            "volume": [10, 20],
        }
    )
    client = DhanClient(
        client_id="client",
        access_token="token",
        security_ids={"RELIANCE": "1333"},
        transport=transport,
    )
    rows = await client.fetch_candles(
        HistoricalRequest(
            "RELIANCE",
            "30m",
            datetime(2026, 8, 14, tzinfo=UTC),
            datetime(2026, 8, 15, tzinfo=UTC),
        )
    )
    assert rows == [
        {
            "timestamp": first,
            "open": 100,
            "high": 106.0,
            "low": 99.0,
            "close": 105,
            "volume": 30.0,
            "timeframe": "30m",
            "settled": True,
        }
    ]
    assert transport.requests[0].json_body["interval"] == "15"  # type: ignore[index]


@pytest.mark.asyncio
async def test_builder_resolves_missing_security_ids_and_creates_730_day_jobs(
    tmp_path: Path,
) -> None:
    symbols = write(
        tmp_path / "symbols.csv",
        "symbol,security_id,timeframes\nRELIANCE,1333,5m|15m\nTCS,,1h\n",
    )
    master = FakeTextTransport(
        "EXCH_ID,SEGMENT,INSTRUMENT,SECURITY_ID,SYMBOL_NAME,TRADING_SYMBOL,DISPLAY_NAME\n"
        "NSE,E,EQUITY,11536,Tata Consultancy Services,TCS,TCS\n"
    )
    secrets = DhanSecretFiles(
        write(tmp_path / "pin", "123456"),
        write(tmp_path / "totp", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"),
    )
    token_transport = FakeJsonTransport(
        {"accessToken": "token", "expiryTime": "2026-08-16T15:30:00+05:30"}
    )
    builder = DhanNseUniverseBuilder(
        token_provider=DhanTokenProvider(
            client_id="client", secrets=secrets, transport=token_transport
        ),
        instrument_master=DhanInstrumentMaster(master),
    )

    universe = await builder.build(symbols, now=NOW)

    assert {item.symbol: item.security_id for item in universe.instruments} == {
        "RELIANCE": "1333",
        "TCS": "11536",
    }
    assert len(universe.jobs) == 3
    assert all(job.target_days == 730 for job in universe.jobs)
    assert all(job.provider_symbols[Provider.DHAN] == job.symbol for job in universe.jobs)


@pytest.mark.asyncio
async def test_instrument_master_resolves_via_underlying_symbol_fallback() -> None:
    """Dhan's real detailed-master CSV has no TRADING_SYMBOL column, and SYMBOL_NAME /
    DISPLAY_NAME hold the company name ("Reliance Industries"), not the plain ticker
    ("RELIANCE") -- only UNDERLYING_SYMBOL does for equity rows. Without this alias,
    resolve() silently fails for the overwhelming majority of NSE equities."""
    transport = FakeTextTransport(
        "EXCH_ID,SEGMENT,INSTRUMENT,SECURITY_ID,UNDERLYING_SYMBOL,SYMBOL_NAME,DISPLAY_NAME\n"
        "NSE,E,EQUITY,2885,RELIANCE,RELIANCE INDUSTRIES LTD,Reliance Industries\n"
    )
    master = DhanInstrumentMaster(transport)

    resolved = await master.resolve(("RELIANCE",))

    assert resolved["RELIANCE"].security_id == "2885"


def test_one_dhan_client_routes_each_canonical_symbol_to_its_security_id() -> None:
    client = DhanClient(
        client_id="client",
        access_token="token",
        security_ids={"RELIANCE": "1333", "TCS": "11536"},
    )
    request = HistoricalRequest(
        "TCS",
        "1h",
        datetime(2026, 8, 14, tzinfo=UTC),
        datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert client.history_request(request).json_body["securityId"] == "11536"  # type: ignore[index]


def test_builder_environment_supports_manual_token_or_protected_files(tmp_path: Path) -> None:
    manual, manual_csv = DhanNseUniverseBuilder.from_environment(
        {
            "DHAN_CLIENT_ID": "client",
            "DHAN_ACCESS_TOKEN": "token",
            "NSE_SYMBOLS_CSV": "config/nse/symbols.csv",
        }
    )
    assert isinstance(manual, DhanNseUniverseBuilder)
    assert manual_csv == Path("config/nse/symbols.csv")

    protected, protected_csv = DhanNseUniverseBuilder.from_environment(
        {
            "DHAN_CLIENT_ID": "client",
            "DHAN_PIN_PATH": str(tmp_path / "pin"),
            "DHAN_TOTP_SECRET_PATH": str(tmp_path / "totp"),
            "NSE_SYMBOLS_CSV": str(tmp_path / "symbols.csv"),
        }
    )
    assert isinstance(protected, DhanNseUniverseBuilder)
    assert protected_csv == tmp_path / "symbols.csv"

    with pytest.raises(ValueError, match="DHAN_ACCESS_TOKEN"):
        DhanNseUniverseBuilder.from_environment(
            {"DHAN_CLIENT_ID": "client", "NSE_SYMBOLS_CSV": "symbols.csv"}
        )
