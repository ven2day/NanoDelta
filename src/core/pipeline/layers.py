"""Authoritative six-layer vocabulary and processing boundaries.

The layers describe persisted or reproducible data products. Engines, ML jobs, and
trading agents are processors: they read one or more layers and write another. Keeping
those ideas separate prevents a market adapter or an agent from becoming an accidental
execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Generic, TypeVar

from src.core.models import Market


class PipelineLayer(StrEnum):
    """Ordered data products shared by NSE, Forex, and Crypto."""

    RAW = "RAW"
    CANONICAL = "CANONICAL"
    FEATURE = "FEATURE"
    DECISION = "DECISION"
    EXECUTION = "EXECUTION"
    OUTCOME = "OUTCOME"


class ProcessingRole(StrEnum):
    """Processors which operate between layers; these are not storage layers."""

    INGESTION = "INGESTION"
    NORMALIZATION = "NORMALIZATION"
    FEATURE_ENGINE = "FEATURE_ENGINE"
    STRATEGY_ENGINE = "STRATEGY_ENGINE"
    ML_INFERENCE = "ML_INFERENCE"
    TRADING_AGENTS = "TRADING_AGENTS"
    RISK_ENGINE = "RISK_ENGINE"
    EXECUTION_ENGINE = "EXECUTION_ENGINE"
    OUTCOME_ENGINE = "OUTCOME_ENGINE"
    ML_TRAINING = "ML_TRAINING"


@dataclass(frozen=True)
class ProcessingBoundary:
    """Layers a processor may read and write."""

    reads: frozenset[PipelineLayer]
    writes: frozenset[PipelineLayer]

    def may_read(self, layer: PipelineLayer) -> bool:
        return layer in self.reads

    def may_write(self, layer: PipelineLayer) -> bool:
        return layer in self.writes


_ROLE_BOUNDARIES = {
    ProcessingRole.INGESTION: ProcessingBoundary(
        reads=frozenset(),
        writes=frozenset({PipelineLayer.RAW}),
    ),
    ProcessingRole.NORMALIZATION: ProcessingBoundary(
        reads=frozenset({PipelineLayer.RAW}),
        writes=frozenset({PipelineLayer.CANONICAL}),
    ),
    ProcessingRole.FEATURE_ENGINE: ProcessingBoundary(
        reads=frozenset({PipelineLayer.CANONICAL}),
        writes=frozenset({PipelineLayer.FEATURE}),
    ),
    ProcessingRole.STRATEGY_ENGINE: ProcessingBoundary(
        reads=frozenset({PipelineLayer.FEATURE}),
        writes=frozenset({PipelineLayer.DECISION}),
    ),
    ProcessingRole.ML_INFERENCE: ProcessingBoundary(
        reads=frozenset({PipelineLayer.FEATURE}),
        writes=frozenset({PipelineLayer.DECISION}),
    ),
    ProcessingRole.TRADING_AGENTS: ProcessingBoundary(
        reads=frozenset({PipelineLayer.FEATURE, PipelineLayer.DECISION}),
        writes=frozenset({PipelineLayer.DECISION}),
    ),
    ProcessingRole.RISK_ENGINE: ProcessingBoundary(
        reads=frozenset({PipelineLayer.DECISION, PipelineLayer.EXECUTION}),
        writes=frozenset({PipelineLayer.DECISION}),
    ),
    ProcessingRole.EXECUTION_ENGINE: ProcessingBoundary(
        reads=frozenset({PipelineLayer.DECISION}),
        writes=frozenset({PipelineLayer.EXECUTION}),
    ),
    ProcessingRole.OUTCOME_ENGINE: ProcessingBoundary(
        reads=frozenset(
            {
                PipelineLayer.CANONICAL,
                PipelineLayer.DECISION,
                PipelineLayer.EXECUTION,
            }
        ),
        writes=frozenset({PipelineLayer.OUTCOME}),
    ),
    ProcessingRole.ML_TRAINING: ProcessingBoundary(
        reads=frozenset({PipelineLayer.FEATURE, PipelineLayer.OUTCOME}),
        # Models are versioned artifacts, not another medallion layer.
        writes=frozenset(),
    ),
}

ROLE_BOUNDARIES = MappingProxyType(_ROLE_BOUNDARIES)


def role_boundary(role: ProcessingRole | str) -> ProcessingBoundary:
    """Return the authoritative boundary for a processing role."""

    return ROLE_BOUNDARIES[ProcessingRole(str(role).strip().upper())]


class LayerBoundaryError(ValueError):
    """Raised when lineage or a layer transition violates the shared contract."""


_ALLOWED_TRANSITIONS = {
    PipelineLayer.RAW: frozenset({PipelineLayer.CANONICAL}),
    PipelineLayer.CANONICAL: frozenset({PipelineLayer.FEATURE}),
    PipelineLayer.FEATURE: frozenset({PipelineLayer.DECISION}),
    PipelineLayer.DECISION: frozenset({PipelineLayer.EXECUTION}),
    PipelineLayer.EXECUTION: frozenset({PipelineLayer.OUTCOME}),
    # Outcome feedback is input to later feature/model work, never direct execution.
    PipelineLayer.OUTCOME: frozenset({PipelineLayer.FEATURE}),
}


def assert_transition(source: PipelineLayer, target: PipelineLayer) -> None:
    """Reject layer skipping and unsafe feedback paths."""

    if target not in _ALLOWED_TRANSITIONS[source]:
        raise LayerBoundaryError(f"Illegal pipeline transition: {source.value} -> {target.value}")


@dataclass(frozen=True)
class LayerLineage:
    """Minimum identity carried by every cross-layer record."""

    market: Market
    provider: str
    symbol: str
    event_timestamp: datetime
    received_timestamp: datetime
    correlation_id: str
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise LayerBoundaryError("Lineage provider cannot be empty")
        if not self.symbol.strip():
            raise LayerBoundaryError("Lineage symbol cannot be empty")
        if not self.correlation_id.strip():
            raise LayerBoundaryError("Lineage correlation_id cannot be empty")
        if not self.schema_version.strip():
            raise LayerBoundaryError("Lineage schema_version cannot be empty")
        if self.event_timestamp.tzinfo is None or self.received_timestamp.tzinfo is None:
            raise LayerBoundaryError("Lineage timestamps must be timezone-aware")

    @property
    def event_timestamp_utc(self) -> datetime:
        return self.event_timestamp.astimezone(UTC)

    @property
    def received_timestamp_utc(self) -> datetime:
        return self.received_timestamp.astimezone(UTC)


PayloadT = TypeVar("PayloadT")
NextPayloadT = TypeVar("NextPayloadT")


@dataclass(frozen=True)
class LayerEnvelope(Generic[PayloadT]):
    """A typed data product with immutable market and correlation lineage."""

    layer: PipelineLayer
    lineage: LayerLineage
    payload: PayloadT
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None:
            raise LayerBoundaryError("Envelope created_at must be timezone-aware")

    def advance(
        self,
        target: PipelineLayer,
        payload: NextPayloadT,
        *,
        created_at: datetime | None = None,
    ) -> LayerEnvelope[NextPayloadT]:
        """Create the next product without permitting market or lineage substitution."""

        assert_transition(self.layer, target)
        return LayerEnvelope(
            layer=target,
            lineage=replace(self.lineage),
            payload=payload,
            created_at=created_at or datetime.now(UTC),
        )
