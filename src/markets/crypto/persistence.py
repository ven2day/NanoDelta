"""Crypto repository factory."""

from typing import Any

from src.core.decisions import SchemaBoundDecisionRepository
from src.core.execution import SchemaBoundExecutionRepository
from src.core.features import SchemaBoundFeatureRepository
from src.core.persistence import SchemaBoundCandleRepository


def bind_candle_repository(store: Any) -> SchemaBoundCandleRepository:
    return SchemaBoundCandleRepository(store, market="CRYPTO", provider="UNCONFIGURED")


def bind_feature_repository(
    engine: Any, *, provider: str = "UNCONFIGURED"
) -> SchemaBoundFeatureRepository:
    """Bind Crypto Gold storage without claiming a specific venue adapter exists."""

    return SchemaBoundFeatureRepository(engine, market="CRYPTO", provider=provider)


def bind_decision_repository(
    engine: Any, *, provider: str = "UNCONFIGURED"
) -> SchemaBoundDecisionRepository:
    return SchemaBoundDecisionRepository(engine, market="CRYPTO", provider=provider)


def bind_execution_repository(
    engine: Any, *, provider: str = "UNCONFIGURED"
) -> SchemaBoundExecutionRepository:
    return SchemaBoundExecutionRepository(engine, market="CRYPTO", provider=provider)


__all__ = [
    "bind_candle_repository",
    "bind_decision_repository",
    "bind_execution_repository",
    "bind_feature_repository",
]
