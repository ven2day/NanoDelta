"""Immutable Raw/Bronze market events and failure-isolated sinks."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from src.config.secrets import redact_secrets
from src.core.models import Market, MarketProvider

logger = logging.getLogger(__name__)


class RawEventType(StrEnum):
    CANDLE = "CANDLE"
    QUOTE = "QUOTE"
    TRADE = "TRADE"
    ORDER_BOOK = "ORDER_BOOK"
    INSTRUMENT = "INSTRUMENT"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _json_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    safe = deepcopy(redact_secrets(dict(payload)))
    encoded = json.dumps(
        safe,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return encoded, digest


@dataclass(frozen=True)
class RawMarketEvent:
    """Exact secret-safe provider payload plus reproducible source identity."""

    event_id: str
    market: Market
    provider: MarketProvider
    event_type: RawEventType
    symbol: str
    channel: str
    source_event_time: datetime
    received_at: datetime
    payload_json_text: str = field(repr=False, compare=False)
    payload_hash: str
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("Raw event_id cannot be empty")
        if not self.symbol.strip():
            raise ValueError("Raw symbol cannot be empty")
        if not self.channel.strip():
            raise ValueError("Raw channel cannot be empty")
        if not self.schema_version.strip():
            raise ValueError("Raw schema_version cannot be empty")
        _aware_utc(self.source_event_time, "source_event_time")
        _aware_utc(self.received_at, "received_at")

    @classmethod
    def create(
        cls,
        *,
        market: Market,
        provider: MarketProvider,
        event_type: RawEventType,
        symbol: str,
        channel: str,
        source_event_time: datetime,
        payload: Mapping[str, Any],
        received_at: datetime | None = None,
        source_event_id: str = "",
        schema_version: str = "1",
    ) -> RawMarketEvent:
        encoded_payload, payload_hash = _json_payload(payload)
        source_utc = _aware_utc(source_event_time, "source_event_time")
        received_utc = _aware_utc(received_at or datetime.now(UTC), "received_at")
        identity = "|".join(
            (
                market.value,
                provider.value,
                event_type.value,
                symbol.strip().upper(),
                channel.strip().lower(),
                source_event_id.strip(),
                source_utc.isoformat(),
                payload_hash,
            )
        )
        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return cls(
            event_id=event_id,
            market=market,
            provider=provider,
            event_type=event_type,
            symbol=symbol.strip().upper(),
            channel=channel.strip().lower(),
            source_event_time=source_utc,
            received_at=received_utc,
            payload_json_text=encoded_payload,
            payload_hash=payload_hash,
            schema_version=schema_version,
        )

    @property
    def payload(self) -> Mapping[str, Any]:
        """Return a fresh read-only view; callers cannot mutate the stored JSON."""

        decoded = json.loads(self.payload_json_text)
        if not isinstance(decoded, dict):  # defensive; create() always serializes a mapping
            raise TypeError("Raw event payload must decode to an object")
        return MappingProxyType(decoded)

    def payload_json(self) -> str:
        return self.payload_json_text


class RawEventSink(Protocol):
    def persist(self, event: RawMarketEvent) -> bool: ...


class InMemoryRawEventSink:
    """Idempotent sink for tests and local composition."""

    def __init__(self) -> None:
        self._events: dict[str, RawMarketEvent] = {}

    def persist(self, event: RawMarketEvent) -> bool:
        if event.event_id in self._events:
            return False
        self._events[event.event_id] = event
        return True

    @property
    def events(self) -> tuple[RawMarketEvent, ...]:
        return tuple(self._events.values())


def emit_raw_event(sink: RawEventSink | None, event: RawMarketEvent) -> bool:
    """Persist observability data without allowing Bronze failure to stop trading."""

    if sink is None:
        return False
    try:
        return sink.persist(event)
    except Exception:
        logger.warning(
            "Raw market-event persistence failed: market=%s provider=%s type=%s",
            event.market.value,
            event.provider.value,
            event.event_type.value,
            exc_info=True,
        )
        return False
