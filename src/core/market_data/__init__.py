"""Shared Raw/Bronze and Canonical/Silver market-data contracts."""

from src.core.market_data.quality import (
    CanonicalDataIssue,
    CanonicalizationResult,
    validate_canonical_candle,
    validate_canonical_quote,
)
from src.core.market_data.raw import (
    InMemoryRawEventSink,
    RawEventSink,
    RawEventType,
    RawMarketEvent,
    emit_raw_event,
)
from src.core.market_data.repository import SchemaBoundRawMarketRepository

__all__ = [
    "CanonicalDataIssue",
    "CanonicalizationResult",
    "InMemoryRawEventSink",
    "RawEventSink",
    "RawEventType",
    "RawMarketEvent",
    "SchemaBoundRawMarketRepository",
    "emit_raw_event",
    "validate_canonical_candle",
    "validate_canonical_quote",
]
