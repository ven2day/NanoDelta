"""Secure Dhan 24-hour access-token generation from PIN/TOTP secret files."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nanodelta.contracts import utc
from nanodelta.persistence.migrations import Connection
from nanodelta.providers.base import HttpRequest, JsonTransport, ProviderClientError
from nanodelta.providers.transports import HttpxJsonTransport

_TOKEN_CACHE_LOCK_ID = 6_641_002_025


def generate_totp(secret: str, *, at: datetime, digits: int = 6, period_seconds: int = 30) -> str:
    """Generate RFC 6238 SHA-1 TOTP without persisting or logging the secret."""
    if digits < 6 or period_seconds < 1:
        raise ValueError("invalid TOTP settings")
    normalized = "".join(secret.split()).upper().rstrip("=")
    if not normalized:
        raise ValueError("TOTP secret is empty")
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    try:
        key = base64.b32decode(normalized + padding, casefold=True)
    except Exception as exc:
        raise ValueError("TOTP secret must be Base32") from exc
    counter = int(utc(at, "at").timestamp()) // period_seconds
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


@dataclass(frozen=True)
class DhanSecretFiles:
    pin_path: Path = field(repr=False)
    totp_secret_path: Path = field(repr=False)

    def read(self) -> tuple[str, str]:
        pin = self._read(self.pin_path, "Dhan PIN")
        secret = self._read(self.totp_secret_path, "Dhan TOTP secret")
        if not (pin.isdigit() and len(pin) == 6):
            raise ValueError("Dhan PIN file must contain exactly six digits")
        return pin, secret

    @staticmethod
    def _read(path: Path, label: str) -> str:
        if not path.is_file():
            raise FileNotFoundError(f"{label} file does not exist: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"{label} file is empty")
        return value


@dataclass(frozen=True)
class DhanAccessToken:
    value: str = field(repr=False)
    expires_at: datetime


class DhanTokenProvider:
    """Generate once and cache until shortly before Dhan's reported expiry."""

    _URL = "https://auth.dhan.co/app/generateAccessToken"

    def __init__(
        self,
        *,
        client_id: str,
        secrets: DhanSecretFiles,
        transport: JsonTransport | None = None,
        refresh_margin: timedelta = timedelta(minutes=5),
    ) -> None:
        if not client_id.strip() or refresh_margin < timedelta(0):
            raise ValueError("Dhan client ID or token refresh margin is invalid")
        self.client_id = client_id.strip()
        self._secrets = secrets
        self._transport = transport or HttpxJsonTransport()
        self._refresh_margin = refresh_margin
        self._cached: DhanAccessToken | None = None
        self._lock = asyncio.Lock()

    async def token(self, *, now: datetime) -> DhanAccessToken:
        now = utc(now, "now")
        async with self._lock:
            cached = self._cached
            if cached is not None and now < cached.expires_at - self._refresh_margin:
                return cached
            pin, secret = self._secrets.read()
            totp = generate_totp(secret, at=now)
            payload = await self._transport.request(
                HttpRequest(
                    method="POST",
                    url=self._URL,
                    params={"dhanClientId": self.client_id, "pin": pin, "totp": totp},
                )
            )
            token = self._parse(payload)
            self._cached = token
            return token

    @staticmethod
    def _parse(payload: Any) -> DhanAccessToken:
        if not isinstance(payload, dict):
            raise ProviderClientError("Dhan token response must be an object")
        raw_token = payload.get("accessToken")
        raw_expiry = payload.get("expiryTime")
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise ProviderClientError("Dhan token response has no accessToken")
        if not isinstance(raw_expiry, str):
            raise ProviderClientError("Dhan token response has no expiryTime")
        try:
            parsed = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProviderClientError("Dhan token expiry is invalid") from exc
        if parsed.tzinfo is None:
            # Dhan documents expiry in IST when no offset is supplied.
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
        return DhanAccessToken(raw_token, parsed.astimezone(UTC))


class StaticDhanTokenProvider:
    """Compatibility source for a manually supplied 24-hour access token."""

    def __init__(self, *, client_id: str, access_token: str) -> None:
        if not client_id.strip() or not access_token.strip():
            raise ValueError("Dhan client ID and access token are required")
        self.client_id = client_id.strip()
        self._access_token = access_token.strip()

    async def token(self, *, now: datetime) -> DhanAccessToken:
        now = utc(now, "now")
        return DhanAccessToken(self._access_token, now + timedelta(minutes=30))


async def resolve_dhan_access_token(
    client_id: str, *, connect: Callable[[], Connection] | None = None
) -> str:
    """Option A: a manually generated 24-hour token at DHAN_ACCESS_TOKEN_PATH.
    Option B: PIN+TOTP auto-generation via DhanTokenProvider (DhanSecretFiles at
    DHAN_PIN_PATH/DHAN_TOTP_SECRET_PATH). Prefers a manually supplied token when both
    are configured. Shared by every process that needs a Dhan access token at startup
    (realtime workers, historical backfill, the read API) so the paths can't drift.

    With `connect`, PIN+TOTP resolution is cached in Postgres and guarded by an
    advisory lock: every such process calls Dhan's generateAccessToken independently,
    and two processes starting within the same 30-second TOTP window would submit the
    same one-time code -- Dhan's replay protection accepts only the first, failing the
    other. The cache makes concurrent startups share one token instead of racing."""
    static_path = os.environ.get("DHAN_ACCESS_TOKEN_PATH", "").strip()
    if static_path:
        value = Path(static_path).read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError("DHAN_ACCESS_TOKEN_PATH points to an empty secret")
        return value
    pin_path = os.environ.get("DHAN_PIN_PATH", "").strip()
    totp_path = os.environ.get("DHAN_TOTP_SECRET_PATH", "").strip()
    if not (pin_path and totp_path):
        raise RuntimeError(
            "Dhan credentials are required: set DHAN_ACCESS_TOKEN_PATH, or both "
            "DHAN_PIN_PATH and DHAN_TOTP_SECRET_PATH"
        )
    provider = DhanTokenProvider(
        client_id=client_id,
        secrets=DhanSecretFiles(Path(pin_path), Path(totp_path)),
    )
    if connect is None:
        token = await provider.token(now=datetime.now(UTC))
        return token.value
    return await _cached_dhan_access_token(client_id, provider, connect)


def _read_cached_token(connection: Connection, client_id: str) -> tuple[str, datetime] | None:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT access_token, expires_at FROM control.dhan_access_tokens WHERE client_id = %s",
        (client_id,),
    )
    row = cursor.fetchone()
    if row is None or not isinstance(row[1], datetime):
        return None
    return str(row[0]), row[1]


def _fresh(cached: tuple[str, datetime] | None, *, now: datetime) -> bool:
    return cached is not None and now < cached[1] - timedelta(minutes=5)


async def _cached_dhan_access_token(
    client_id: str, provider: DhanTokenProvider, connect: Callable[[], Connection]
) -> str:
    probe = connect()
    try:
        cached = _read_cached_token(probe, client_id)
    finally:
        probe.close()
    if _fresh(cached, now=datetime.now(UTC)):
        assert cached is not None
        return cached[0]

    connection = connect()
    try:
        connection.cursor().execute("SELECT pg_advisory_lock(%s)", (_TOKEN_CACHE_LOCK_ID,))
        cached = _read_cached_token(connection, client_id)
        now = datetime.now(UTC)
        if _fresh(cached, now=now):
            assert cached is not None
            return cached[0]
        token = await provider.token(now=now)
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO control.dhan_access_tokens "
            "(client_id, access_token, expires_at, fetched_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (client_id) DO UPDATE SET "
            "access_token = EXCLUDED.access_token, expires_at = EXCLUDED.expires_at, "
            "fetched_at = EXCLUDED.fetched_at",
            (client_id, token.value, token.expires_at, now),
        )
        connection.commit()
        return token.value
    except Exception:
        connection.rollback()
        raise
    finally:
        try:
            connection.cursor().execute("SELECT pg_advisory_unlock(%s)", (_TOKEN_CACHE_LOCK_ID,))
        finally:
            connection.close()
