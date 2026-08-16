"""Content-addressed immutable validation artifacts and explicit promotion workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from nanodelta.contracts import utc
from nanodelta.strategies.backtest import BacktestResult
from nanodelta.strategies.registry import StrategyApproval, StrategyIdentity, StrategyRegistry
from nanodelta.strategies.validation import ValidationResult


class PromotionStage(StrEnum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PAPER_APPROVED = "PAPER_APPROVED"


@dataclass(frozen=True)
class ValidationArtifact:
    schema_version: int
    identity_key: str
    validation_run_id: str
    generated_at: str
    source_data_id: str
    code_revision: str
    promotion_stage: PromotionStage
    validation: dict[str, object]
    walk_forward_net_expectancies: tuple[float, ...]
    trade_count: int
    content_sha256: str


def build_artifact(
    result: ValidationResult,
    backtest: BacktestResult,
    *,
    source_data_id: str,
    code_revision: str,
    stage: PromotionStage = PromotionStage.RESEARCH,
) -> ValidationArtifact:
    if stage is PromotionStage.PAPER_APPROVED:
        raise PermissionError("PAPER_APPROVED requires a separately recorded manual approval")
    if not source_data_id or not code_revision:
        raise ValueError("source data and code revision lineage are required")
    validation = asdict(result)
    validation["identity"] = asdict(result.identity)
    validation["identity"]["market"] = result.identity.market.value
    validation["evaluated_at"] = result.evaluated_at.isoformat()
    validation_json = json.dumps(validation, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(
        f"{source_data_id}|{code_revision}|{validation_json}|{backtest.window_net_expectancies}".encode()
    ).hexdigest()
    return ValidationArtifact(
        1,
        result.identity.key,
        result.validation_run_id,
        result.evaluated_at.isoformat(),
        source_data_id,
        code_revision,
        stage,
        validation,
        backtest.window_net_expectancies,
        len(backtest.trades),
        digest,
    )


def write_artifact(artifact: ValidationArtifact, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{artifact.validation_run_id}-{artifact.content_sha256[:12]}.json"
    payload = json.dumps(asdict(artifact), sort_keys=True, indent=2, default=str) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != payload:
            raise ValueError("immutable artifact path already contains different content") from None
    return path


def promote_to_paper(
    registry: StrategyRegistry,
    *,
    identity: StrategyIdentity,
    validation_run_id: str,
    approved_at: datetime,
    expires_at: datetime,
    approved_by: str,
    reason: str,
) -> StrategyApproval:
    """Manual promotion: registry still enforces an exact, passing validation."""
    approval = StrategyApproval.create(
        identity=identity,
        validation_run_id=validation_run_id,
        approved_at=utc(approved_at, "approved_at"),
        expires_at=utc(expires_at, "expires_at"),
        approved_by=approved_by,
        reason=reason,
    )
    registry.record_approval(approval)
    return approval
