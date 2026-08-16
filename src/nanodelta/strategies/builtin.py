"""Small deterministic strategies that consume NanoDelta Gold feature version 1.

Definitions are registered at runtime, but they are never auto-approved.  The
existing validation and approval tables remain the admission authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.strategies.registry import StrategyDefinition, StrategyIdentity
from nanodelta.strategies.runtime import StrategyContext, StrategySignal


@dataclass
class MomentumContinuationStrategy:
    definition: StrategyDefinition
    minimum_return: float = 0.001
    minimum_body: float = 0.0005
    stop_range_multiple: float = 1.0
    reward_risk: float = 1.5

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        required = {"close", "return_1", "range_pct", "body_pct"}
        if not required.issubset(context.features):
            return False, "REQUIRED_FEATURES_MISSING"
        return True, "COMPATIBLE"

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        close = context.features["close"]
        change = context.features["return_1"]
        body = context.features["body_pct"]
        if abs(change) < self.minimum_return or abs(body) < self.minimum_body:
            return None
        if change * body <= 0:
            return None
        action = AdvisoryAction.BUY if change > 0 else AdvisoryAction.SELL
        distance = close * max(context.features["range_pct"], 0.0005) * self.stop_range_multiple
        stop = close - distance if action is AdvisoryAction.BUY else close + distance
        target = (
            close + distance * self.reward_risk
            if action is AdvisoryAction.BUY
            else close - distance * self.reward_risk
        )
        confidence = min(0.95, 0.5 + abs(change) * 20 + abs(body) * 10)
        return StrategySignal(action, confidence, close, stop, target, estimated_cost_r=0.05)


def builtin_strategies() -> tuple[MomentumContinuationStrategy, ...]:
    result = []
    for market in Market:
        identity = StrategyIdentity(
            market,
            "momentum_continuation",
            "1.0.0",
            "1m",
            "intraday",
            1,
        )
        definition = StrategyDefinition(
            identity,
            "momentum",
            (
                ("minimum_return", "0.001"),
                ("minimum_body", "0.0005"),
                ("stop_range_multiple", "1.0"),
                ("reward_risk", "1.5"),
            ),
            "nanodelta.strategies.builtin:MomentumContinuationStrategy",
        )
        result.append(MomentumContinuationStrategy(definition))
    return tuple(result)
