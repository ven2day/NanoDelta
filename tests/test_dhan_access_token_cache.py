"""Shared Postgres cache for PIN+TOTP-generated Dhan access tokens: processes that
start within the same 30-second TOTP window must share one token instead of each
submitting their own one-time code (Dhan's replay protection rejects the second)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nanodelta.providers.base import HttpRequest
from nanodelta.providers.dhan_auth import (
    DhanSecretFiles,
    DhanTokenProvider,
    _cached_dhan_access_token,
)


class FakeJsonTransport:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls = 0

    async def request(self, request: HttpRequest) -> Any:
        del request
        self.calls += 1
        return self.response


class FakeCursor:
    def __init__(self, store: dict[str, tuple[str, datetime]]) -> None:
        self._store = store
        self._result: tuple[object, ...] | None = None

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        statement = query.strip().split()[0]
        if statement == "SELECT" and "access_token" in query:
            self._result = self._store.get(str(params[0]))
        elif statement == "INSERT":
            client_id, access_token, expires_at, _fetched_at = params
            self._store[str(client_id)] = (str(access_token), expires_at)  # type: ignore[assignment]
        else:
            self._result = None  # advisory lock/unlock: no-op in a single test process

    def fetchone(self) -> tuple[object, ...] | None:
        return self._result

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class FakeConnection:
    def __init__(self, store: dict[str, tuple[str, datetime]]) -> None:
        self._store = store
        self.committed = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._store)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        raise AssertionError("should not roll back on the success path")

    def close(self) -> None:
        self.closed = True


def _provider(tmp_path: Path, transport: FakeJsonTransport) -> DhanTokenProvider:
    pin_path = tmp_path / "pin"
    totp_path = tmp_path / "totp"
    pin_path.write_text("123456", encoding="utf-8")
    totp_path.write_text("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", encoding="utf-8")
    secrets = DhanSecretFiles(pin_path, totp_path)
    return DhanTokenProvider(client_id="1000000001", secrets=secrets, transport=transport)


@pytest.mark.asyncio
async def test_cache_miss_fetches_once_and_persists(tmp_path: Path) -> None:
    store: dict[str, tuple[str, datetime]] = {}
    transport = FakeJsonTransport(
        {"accessToken": "fresh-token", "expiryTime": "2026-08-18T00:00:00+00:00"}
    )
    provider = _provider(tmp_path, transport)

    token = await _cached_dhan_access_token("1000000001", provider, lambda: FakeConnection(store))

    assert token == "fresh-token"
    assert transport.calls == 1
    assert store["1000000001"][0] == "fresh-token"


@pytest.mark.asyncio
async def test_fresh_cache_hit_never_calls_dhan(tmp_path: Path) -> None:
    store: dict[str, tuple[str, datetime]] = {
        "1000000001": ("cached-token", datetime.now(UTC) + timedelta(hours=12))
    }
    transport = FakeJsonTransport({"accessToken": "should-not-be-used", "expiryTime": "x"})
    provider = _provider(tmp_path, transport)

    token = await _cached_dhan_access_token("1000000001", provider, lambda: FakeConnection(store))

    assert token == "cached-token"
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_cache_entry_expiring_within_the_refresh_margin_is_treated_as_stale(
    tmp_path: Path,
) -> None:
    store: dict[str, tuple[str, datetime]] = {
        "1000000001": ("about-to-expire", datetime.now(UTC) + timedelta(minutes=2))
    }
    transport = FakeJsonTransport(
        {"accessToken": "renewed-token", "expiryTime": "2026-08-18T00:00:00+00:00"}
    )
    provider = _provider(tmp_path, transport)

    token = await _cached_dhan_access_token("1000000001", provider, lambda: FakeConnection(store))

    assert token == "renewed-token"
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_second_caller_reuses_the_token_the_first_caller_just_wrote(
    tmp_path: Path,
) -> None:
    """Simulates two processes (e.g. runtime + history) starting together: the second
    call's outer cache check runs after the first has already written a fresh token,
    so it must return that cached value instead of racing Dhan for its own."""
    store: dict[str, tuple[str, datetime]] = {}
    transport = FakeJsonTransport(
        {"accessToken": "first-writer-token", "expiryTime": "2026-08-18T00:00:00+00:00"}
    )
    provider = _provider(tmp_path, transport)
    connect = lambda: FakeConnection(store)  # noqa: E731

    first = await _cached_dhan_access_token("1000000001", provider, connect)
    second = await _cached_dhan_access_token("1000000001", provider, connect)

    assert first == second == "first-writer-token"
    assert transport.calls == 1
