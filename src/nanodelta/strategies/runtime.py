"""Runtime strategy plugin contracts with no central regime matrix."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nanodelta.contracts import AdvisoryAction, Market, stable_id, utc
from nanodelta.strategies.registry import StrategyDefinition, StrategyIdentity


@dataclass(frozen=True)
class RegimeEvidence:
    market_fit: float = 1.0
    sector_fit: float = 1.0
    symbol_fit: float = 1.0
    mtf_alignment: float = 1.0

    def __post_init__(self) -> None:
        values = (self.market_fit, self.sector_fit, self.symbol_fit, self.mtf_alignment)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("regime scoring terms must be finite and non-negative")


@dataclass(frozen=True)
class StrategyContext:
    market: Market
    symbol: str
    sector: str | None
    timeframe: str
    trade_horizon: str
    feature_set_version: int
    event_time: datetime
    gold_snapshot_ids: tuple[str, ...]
    features: Mapping[str, float]
    settled: bool = True
    complete: bool = True
    adjusted: bool = True
    fresh: bool = True
    warm: bool = True
    tradeable: bool = True
    tradeability_reason: str = "TRADEABLE"
    regime: RegimeEvidence = RegimeEvidence()

    def __post_init__(self) -> None:
        if not self.symbol or not self.timeframe or not self.trade_horizon:
            raise ValueError("symbol, timeframe, and trade_horizon are required")
        if self.feature_set_version < 1 or not self.gold_snapshot_ids:
            raise ValueError("positive feature version and Gold lineage are required")
        if any(not math.isfinite(value) for value in self.features.values()):
            raise ValueError("strategy features must be finite")
        utc(self.event_time, "event_time")


@dataclass(frozen=True)
class StrategySignal:
    action: AdvisoryAction
    confidence: float
    reference_price: float
    stop_price: float
    target_price: float
    estimated_cost_r: float = 0.0
    historical_expectancy_r: float = 0.0
    ml_tilt_r: float = 0.0

    def __post_init__(self) -> None:
        if self.action is AdvisoryAction.ABSTAIN:
            raise ValueError("a strategy signal must be BUY or SELL")
        values = (
            self.confidence,
            self.reference_price,
            self.stop_price,
            self.target_price,
            self.estimated_cost_r,
            self.historical_expectancy_r,
            self.ml_tilt_r,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("signal values must be finite")
        if not 0 <= self.confidence <= 1 or self.reference_price <= 0:
            raise ValueError("signal confidence or reference price is invalid")
        if self.estimated_cost_r < 0:
            raise ValueError("estimated cost cannot be negative")
        if self.action is AdvisoryAction.BUY and not (
            self.stop_price < self.reference_price < self.target_price
        ):
            raise ValueError("BUY geometry requires stop < entry < target")
        if self.action is AdvisoryAction.SELL and not (
            self.target_price < self.reference_price < self.stop_price
        ):
            raise ValueError("SELL geometry requires target < entry < stop")


@dataclass(frozen=True)
class DeterministicCandidate:
    candidate_id: str
    identity: StrategyIdentity
    approval_id: str
    symbol: str
    sector: str | None
    event_time: datetime
    gold_snapshot_ids: tuple[str, ...]
    signal: StrategySignal
    regime: RegimeEvidence

    @classmethod
    def create(
        cls,
        definition: StrategyDefinition,
        approval_id: str,
        context: StrategyContext,
        signal: StrategySignal,
    ) -> DeterministicCandidate:
        candidate_id = stable_id(
            definition.identity.key,
            context.symbol,
            utc(context.event_time, "event_time").isoformat(),
            signal.action.value,
            *context.gold_snapshot_ids,
        )
        return cls(
            candidate_id,
            definition.identity,
            approval_id,
            context.symbol,
            context.sector,
            context.event_time,
            context.gold_snapshot_ids,
            signal,
            context.regime,
        )


class StrategyPlugin(Protocol):
    """A future strategy implements this protocol and registers once."""

    definition: StrategyDefinition

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]: ...

    def generate(self, context: StrategyContext) -> StrategySignal | None: ...


class StrategyRuntimeCatalog:
    def __init__(self) -> None:
        self._plugins: dict[StrategyIdentity, StrategyPlugin] = {}

    def register(self, plugin: StrategyPlugin) -> None:
        identity = plugin.definition.identity
        existing = self._plugins.get(identity)
        if existing is not None and existing is not plugin:
            raise ValueError("runtime strategy identity is already registered")
        self._plugins[identity] = plugin

    def require(self, identity: StrategyIdentity) -> StrategyPlugin:
        try:
            return self._plugins[identity]
        except KeyError as exc:
            raise LookupError("approved strategy has no runtime plugin") from exc
