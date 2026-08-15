"""Shared paper Execution-layer contracts and persistence."""

from src.core.execution.record import (
    ExecutionMode,
    ExecutionRecord,
    ExecutionStatus,
    FillRecord,
    OrderIntent,
    PositionReference,
    execution_record_from_result,
    stable_execution_id,
)
from src.core.execution.repository import SchemaBoundExecutionRepository, persist_execution_records

__all__ = [
    "ExecutionMode",
    "ExecutionRecord",
    "ExecutionStatus",
    "FillRecord",
    "OrderIntent",
    "PositionReference",
    "SchemaBoundExecutionRepository",
    "execution_record_from_result",
    "persist_execution_records",
    "stable_execution_id",
]
