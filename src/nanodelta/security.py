"""Durable identity, revocable sessions, API-key lifecycle, and security audit."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from nanodelta.operations import Actor
from nanodelta.persistence.migrations import Connection, Cursor

ROLES = frozenset({"viewer", "operator", "admin"})
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$00000000000000000000000000000000$"
    "4f261698a2c7c85d8f431f8480051230d66451a58a74787885018f2e1b650462"
)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    actual_salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode(), salt=actual_salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${actual_salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


@dataclass(frozen=True)
class AuthSession:
    token: str
    actor: Actor
    expires_at: datetime


class PostgresSecurityStore:
    """Fail-closed PostgreSQL identity store; raw secrets are never persisted or audited."""

    def __init__(
        self,
        connect: Callable[[], Connection],
        *,
        max_failures: int = 5,
        failure_window: timedelta = timedelta(minutes=15),
        lockout: timedelta = timedelta(minutes=30),
        session_ttl: timedelta = timedelta(hours=8),
    ) -> None:
        self._connect = connect
        self._max_failures = max_failures
        self._failure_window = failure_window
        self._lockout = lockout
        self._session_ttl = session_ttl

    def upsert_user(self, username: str, password: str, role: str) -> str:
        if role not in ROLES:
            raise ValueError("invalid role")
        normalized = username.strip().casefold()
        if not normalized:
            raise ValueError("username is required")
        user_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO auth.users(user_id,username,password_hash,role) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(username) DO UPDATE SET password_hash=EXCLUDED.password_hash,"
                "role=EXCLUDED.role,active=true,updated_at=now(),password_changed_at=now() "
                "RETURNING user_id",
                (user_id, normalized, hash_password(password), role),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("user upsert returned no identifier")
            actual_id = str(row[0])
            cursor.execute(
                "UPDATE auth.sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",
                (actual_id,),
            )
            self._audit(cursor, "user_upserted", actual_id, actual_id, None, True, {"role": role})
            connection.commit()
            return actual_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def disable_user(self, username: str, actor_id: str = "local-operator") -> bool:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE auth.users SET active=false,updated_at=now() WHERE username=%s "
                "RETURNING user_id",
                (username.strip().casefold(),),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "UPDATE auth.sessions SET revoked_at=now() WHERE user_id=%s "
                    "AND revoked_at IS NULL",
                    (row[0],),
                )
            self._audit(
                cursor,
                "user_disabled",
                actor_id,
                str(row[0]) if row else None,
                None,
                row is not None,
                {},
            )
            connection.commit()
            return row is not None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def login(self, username: str, password: str, source: str) -> AuthSession | None:
        normalized = username.strip().casefold()
        origin = source_hash(source)
        now = datetime.now(UTC)
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{normalized}:{origin}",),
            )
            cursor.execute(
                "SELECT failed_attempts,window_started_at,locked_until FROM auth.login_throttle "
                "WHERE username=%s AND source_hash=%s FOR UPDATE",
                (normalized, origin),
            )
            throttle = cast(tuple[int, datetime, datetime | None] | None, cursor.fetchone())
            if throttle and throttle[2] is not None and throttle[2] > now:
                self._audit(cursor, "login_locked", None, normalized, origin, False, {})
                connection.commit()
                return None
            cursor.execute(
                "SELECT user_id,password_hash,role FROM auth.users "
                "WHERE username=%s AND active=true",
                (normalized,),
            )
            row = cast(tuple[object, str, str] | None, cursor.fetchone())
            valid = verify_password(password, str(row[1]) if row else _DUMMY_PASSWORD_HASH)
            valid = row is not None and valid
            if not valid:
                failures = 1
                window_start = now
                if throttle and now - throttle[1] <= self._failure_window:
                    failures = int(throttle[0]) + 1
                    window_start = throttle[1]
                locked_until = now + self._lockout if failures >= self._max_failures else None
                cursor.execute(
                    "INSERT INTO auth.login_throttle(username,source_hash,failed_attempts,"
                    "window_started_at,locked_until) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT(username,source_hash) DO UPDATE SET "
                    "failed_attempts=EXCLUDED.failed_attempts,"
                    "window_started_at=EXCLUDED.window_started_at,locked_until=EXCLUDED.locked_until",
                    (normalized, origin, failures, window_start, locked_until),
                )
                self._audit(cursor, "login_failed", None, normalized, origin, False, {})
                connection.commit()
                return None
            assert row is not None
            cursor.execute(
                "DELETE FROM auth.login_throttle WHERE username=%s AND source_hash=%s",
                (normalized, origin),
            )
            raw_token = secrets.token_urlsafe(48)
            expires_at = now + self._session_ttl
            session_id = uuid.uuid4()
            cursor.execute(
                "INSERT INTO auth.sessions(session_id,user_id,token_hash,expires_at,source_hash) "
                "VALUES (%s,%s,%s,%s,%s)",
                (session_id, row[0], token_hash(raw_token), expires_at, origin),
            )
            self._audit(cursor, "login_succeeded", str(row[0]), str(row[0]), origin, True, {})
            connection.commit()
            return AuthSession(raw_token, Actor(str(row[0]), str(row[2])), expires_at)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def session_actor(self, token: str, *, touch: bool = True) -> Actor | None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT u.user_id,u.role FROM auth.sessions s "
                "JOIN auth.users u ON u.user_id=s.user_id "
                "WHERE s.token_hash=%s AND s.revoked_at IS NULL "
                "AND s.expires_at>now() AND u.active=true",
                (token_hash(token),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            if touch:
                cursor.execute(
                    "UPDATE auth.sessions SET last_seen_at=now() WHERE token_hash=%s",
                    (token_hash(token),),
                )
                connection.commit()
            return Actor(str(row[0]), str(row[1]))
        finally:
            connection.close()

    def revoke_session(self, token: str, actor_id: str | None = None) -> None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE auth.sessions SET revoked_at=now() WHERE token_hash=%s "
                "AND revoked_at IS NULL",
                (token_hash(token),),
            )
            self._audit(cursor, "session_revoked", actor_id, None, None, True, {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def authenticate_api_key(self, key: str) -> Actor | None:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE auth.api_keys SET last_used_at=now() WHERE key_hash=%s "
                "AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at>now()) RETURNING actor_id,role",
                (token_hash(key),),
            )
            row = cursor.fetchone()
            connection.commit()
            return Actor(str(row[0]), str(row[1])) if row else None
        finally:
            connection.close()

    def create_api_key(self, name: str, actor_id: str, role: str) -> tuple[str, str]:
        if role not in ROLES:
            raise ValueError("invalid role")
        raw = f"nd_{secrets.token_urlsafe(36)}"
        key_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO auth.api_keys(key_id,name,key_hash,actor_id,role) "
                "VALUES (%s,%s,%s,%s,%s)",
                (key_id, name, token_hash(raw), actor_id, role),
            )
            self._audit(
                cursor,
                "api_key_created",
                actor_id,
                key_id,
                None,
                True,
                {"name": name, "role": role},
            )
            connection.commit()
            return key_id, raw
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def revoke_api_key(self, key_id: str, actor_id: str) -> bool:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE auth.api_keys SET revoked_at=now() WHERE key_id=%s AND revoked_at IS NULL "
                "RETURNING key_id",
                (key_id,),
            )
            changed = cursor.fetchone() is not None
            self._audit(cursor, "api_key_revoked", actor_id, key_id, None, changed, {})
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _audit(
        cursor: Cursor,
        event: str,
        actor: str | None,
        subject: str | None,
        origin: str | None,
        success: bool,
        detail: dict[str, str],
    ) -> None:
        cursor.execute(
            "INSERT INTO auth.security_audit(event_id,event_type,actor_id,subject_id,"
            "source_hash,success,detail) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (uuid.uuid4(), event, actor, subject, origin, success, json.dumps(detail)),
        )
