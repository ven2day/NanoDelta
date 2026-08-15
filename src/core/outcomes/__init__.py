"""Shared closed-trade Outcome contracts and persistence."""

from src.core.outcomes.record import OutcomeRecord
from src.core.outcomes.repository import SchemaBoundOutcomeRepository, persist_outcome_records

__all__ = ["OutcomeRecord", "SchemaBoundOutcomeRepository", "persist_outcome_records"]
