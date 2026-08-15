"""Deterministic, cost-aware validation gates for strategy approval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nanodelta.contracts import stable_id, utc
from nanodelta.strategies.registry import StrategyIdentity


@dataclass(frozen=True)
class ValidationMetrics:
    trade_count: int
    walk_forward_windows: int
    profitable_windows: int
    gross_expectancy: float
    estimated_cost_per_trade: float
    maximum_drawdown: float
    p_value: float
    tested_hypotheses: int

    @property
    def net_expectancy(self) -> float:
        return self.gross_expectancy - self.estimated_cost_per_trade


@dataclass(frozen=True)
class ValidationPolicy:
    minimum_trades: int = 30
    minimum_walk_forward_windows: int = 3
    minimum_profitable_window_ratio: float = 0.6
    minimum_net_expectancy: float = 0.0
    maximum_drawdown: float = 0.25
    family_wise_alpha: float = 0.05

    def __post_init__(self) -> None:
        if self.minimum_trades < 1 or self.minimum_walk_forward_windows < 1:
            raise ValueError("minimum sample sizes must be positive")
        if not 0 < self.minimum_profitable_window_ratio <= 1:
            raise ValueError("profitable window ratio must be in (0, 1]")
        if not 0 < self.family_wise_alpha < 1:
            raise ValueError("family_wise_alpha must be in (0, 1)")


@dataclass(frozen=True)
class ValidationResult:
    validation_run_id: str
    identity: StrategyIdentity
    evaluated_at: datetime
    passed: bool
    rejection_reasons: tuple[str, ...]
    metrics: ValidationMetrics
    policy: ValidationPolicy


def validate_strategy(
    identity: StrategyIdentity,
    metrics: ValidationMetrics,
    policy: ValidationPolicy,
    *,
    evaluated_at: datetime,
) -> ValidationResult:
    if metrics.walk_forward_windows < 1 or metrics.tested_hypotheses < 1:
        raise ValueError("walk-forward windows and tested hypotheses must be positive")
    if not 0 <= metrics.profitable_windows <= metrics.walk_forward_windows:
        raise ValueError("profitable_windows is outside the evaluated window count")
    if metrics.estimated_cost_per_trade < 0 or metrics.maximum_drawdown < 0:
        raise ValueError("cost and drawdown cannot be negative")
    if not 0 <= metrics.p_value <= 1:
        raise ValueError("p_value must be in [0, 1]")

    reasons: list[str] = []
    if metrics.trade_count < policy.minimum_trades:
        reasons.append("INSUFFICIENT_TRADES")
    if metrics.walk_forward_windows < policy.minimum_walk_forward_windows:
        reasons.append("INSUFFICIENT_WALK_FORWARD_WINDOWS")
    elif (
        metrics.profitable_windows / metrics.walk_forward_windows
        < policy.minimum_profitable_window_ratio
    ):
        reasons.append("UNSTABLE_WALK_FORWARD_PERFORMANCE")
    if metrics.net_expectancy <= policy.minimum_net_expectancy:
        reasons.append("NON_POSITIVE_COST_ADJUSTED_EXPECTANCY")
    if metrics.maximum_drawdown > policy.maximum_drawdown:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    if metrics.p_value > policy.family_wise_alpha / metrics.tested_hypotheses:
        reasons.append("BONFERRONI_SIGNIFICANCE_FAILED")

    evaluated_at = utc(evaluated_at, "evaluated_at")
    run_id = stable_id(
        identity.key,
        evaluated_at.isoformat(),
        metrics,
        policy,
    )
    return ValidationResult(
        validation_run_id=run_id,
        identity=identity,
        evaluated_at=evaluated_at,
        passed=not reasons,
        rejection_reasons=tuple(reasons),
        metrics=metrics,
        policy=policy,
    )
