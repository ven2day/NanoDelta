"""Exact-identity strategy registry with approval-based runtime admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from nanodelta.contracts import Market, stable_id, utc

if TYPE_CHECKING:
    from nanodelta.strategies.validation import ValidationResult


@dataclass(frozen=True, order=True)
class StrategyIdentity:
    market: Market
    strategy_id: str
    strategy_version: str
    timeframe: str
    trade_horizon: str
    feature_set_version: int

    def __post_init__(self) -> None:
        for name in ("strategy_id", "strategy_version", "timeframe", "trade_horizon"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} cannot be empty")
        if self.feature_set_version < 1:
            raise ValueError("feature_set_version must be positive")

    @property
    def key(self) -> str:
        return stable_id(
            self.market.value,
            self.strategy_id,
            self.strategy_version,
            self.timeframe,
            self.trade_horizon,
            self.feature_set_version,
        )


@dataclass(frozen=True)
class StrategyDefinition:
    identity: StrategyIdentity
    family: str
    parameters: tuple[tuple[str, str], ...]
    implementation_ref: str

    def __post_init__(self) -> None:
        if not self.family.strip() or not self.implementation_ref.strip():
            raise ValueError("family and implementation_ref cannot be empty")
        names = [name for name, _ in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("strategy parameter names must be unique")


class ApprovalState(StrEnum):
    APPROVED = "APPROVED"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class StrategyApproval:
    approval_id: str
    identity: StrategyIdentity
    validation_run_id: str
    state: ApprovalState
    approved_at: datetime
    expires_at: datetime
    approved_by: str
    reason: str

    @classmethod
    def create(
        cls,
        *,
        identity: StrategyIdentity,
        validation_run_id: str,
        approved_at: datetime,
        expires_at: datetime,
        approved_by: str,
        reason: str,
    ) -> StrategyApproval:
        approved_at = utc(approved_at, "approved_at")
        expires_at = utc(expires_at, "expires_at")
        if expires_at <= approved_at:
            raise ValueError("approval must expire after it is approved")
        if not validation_run_id or not approved_by or not reason:
            raise ValueError("validation_run_id, approved_by, and reason are required")
        return cls(
            approval_id=stable_id(identity.key, validation_run_id, approved_at.isoformat()),
            identity=identity,
            validation_run_id=validation_run_id,
            state=ApprovalState.APPROVED,
            approved_at=approved_at,
            expires_at=expires_at,
            approved_by=approved_by,
            reason=reason,
        )

    def is_current(self, at: datetime) -> bool:
        return (
            self.state is ApprovalState.APPROVED
            and self.approved_at <= utc(at, "at") < self.expires_at
        )

    def revoke(self, reason: str) -> StrategyApproval:
        if not reason.strip():
            raise ValueError("revocation reason cannot be empty")
        return StrategyApproval(
            approval_id=self.approval_id,
            identity=self.identity,
            validation_run_id=self.validation_run_id,
            state=ApprovalState.REVOKED,
            approved_at=self.approved_at,
            expires_at=self.expires_at,
            approved_by=self.approved_by,
            reason=reason,
        )


class StrategyRegistry:
    """In-memory runtime registry; persistence adapters can hydrate these artifacts."""

    def __init__(self) -> None:
        self._definitions: dict[StrategyIdentity, StrategyDefinition] = {}
        self._validations: dict[str, ValidationResult] = {}
        self._approvals: dict[str, StrategyApproval] = {}

    def register(self, definition: StrategyDefinition) -> None:
        existing = self._definitions.get(definition.identity)
        if existing is not None and existing != definition:
            raise ValueError("strategy identity is immutable; publish a new version")
        self._definitions[definition.identity] = definition

    def record_validation(self, result: ValidationResult) -> None:
        if result.identity not in self._definitions:
            raise ValueError("cannot validate an unregistered strategy")
        existing = self._validations.get(result.validation_run_id)
        if existing is not None and existing != result:
            raise ValueError("validation artifacts are immutable")
        self._validations[result.validation_run_id] = result

    def record_approval(self, approval: StrategyApproval) -> None:
        if approval.identity not in self._definitions:
            raise ValueError("cannot approve an unregistered strategy")
        validation = self._validations.get(approval.validation_run_id)
        if validation is None or validation.identity != approval.identity:
            raise ValueError("approval requires its exact-identity validation artifact")
        if not validation.passed:
            raise PermissionError("a failed validation cannot be approved")
        existing = self._approvals.get(approval.approval_id)
        if existing is not None and existing != approval:
            raise ValueError("approval artifacts are immutable")
        self._approvals[approval.approval_id] = approval

    def revoke(self, approval_id: str, reason: str) -> None:
        self._approvals[approval_id] = self._approvals[approval_id].revoke(reason)

    def require_approval(self, identity: StrategyIdentity, *, at: datetime) -> StrategyApproval:
        if identity not in self._definitions:
            raise LookupError("strategy identity is not registered")
        current = [
            approval
            for approval in self._approvals.values()
            if approval.identity == identity and approval.is_current(at)
        ]
        if not current:
            raise PermissionError("no current approval for exact strategy identity")
        return max(current, key=lambda approval: approval.approved_at)

    def eligible(
        self,
        *,
        market: Market,
        timeframe: str,
        trade_horizon: str,
        feature_set_version: int,
        at: datetime,
    ) -> tuple[StrategyDefinition, ...]:
        result = []
        for identity, definition in self._definitions.items():
            if (
                identity.market == market
                and identity.timeframe == timeframe
                and identity.trade_horizon == trade_horizon
                and identity.feature_set_version == feature_set_version
            ):
                try:
                    self.require_approval(identity, at=at)
                except PermissionError:
                    continue
                result.append(definition)
        return tuple(sorted(result, key=lambda item: item.identity))
