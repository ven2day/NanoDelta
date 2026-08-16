"""Deterministic technical strategy plugins over declared Gold v2 features."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, TypeAlias

from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.strategies.registry import StrategyDefinition, StrategyIdentity
from nanodelta.strategies.runtime import StrategyContext, StrategySignal

TECHNICAL_FEATURE_VERSION = 2


def _compatibility(
    definition: StrategyDefinition,
    required: frozenset[str],
    context: StrategyContext,
) -> tuple[bool, str]:
    identity = definition.identity
    if context.market is not identity.market:
        return False, "MARKET_MISMATCH"
    if context.timeframe != identity.timeframe:
        return False, "TIMEFRAME_MISMATCH"
    if context.trade_horizon != identity.trade_horizon:
        return False, "TRADE_HORIZON_MISMATCH"
    if context.feature_set_version != identity.feature_set_version:
        return False, "FEATURE_VERSION_MISMATCH"
    if not required.issubset(context.features):
        return False, "REQUIRED_FEATURES_MISSING"
    return True, "COMPATIBLE"


def _signal(
    action: AdvisoryAction,
    *,
    close: float,
    atr: float,
    stop_atr: float,
    reward_risk: float,
    confidence: float,
) -> StrategySignal:
    distance = max(atr * stop_atr, close * 0.0005)
    stop = close - distance if action is AdvisoryAction.BUY else close + distance
    target = (
        close + distance * reward_risk
        if action is AdvisoryAction.BUY
        else close - distance * reward_risk
    )
    return StrategySignal(
        action,
        max(0.0, min(confidence, 0.95)),
        close,
        stop,
        target,
        estimated_cost_r=0.05,
    )


@dataclass(frozen=True)
class VwapPullbackStrategy:
    definition: StrategyDefinition
    pullback_tolerance: float = 0.0015
    minimum_volume_ratio: float = 0.8
    stop_atr: float = 1.2
    reward_risk: float = 1.5

    required_features: ClassVar[frozenset[str]] = frozenset(
        {"close", "high", "low", "vwap", "ema_21", "atr_14", "volume_ratio_20"}
    )

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(self.definition, self.required_features, context)

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        compatible, _ = self.compatibility(context)
        if not compatible:
            return None
        feature = context.features
        close, vwap, ema = feature["close"], feature["vwap"], feature["ema_21"]
        if feature["volume_ratio_20"] < self.minimum_volume_ratio:
            return None
        touched_from_above = feature["low"] <= vwap * (1 + self.pullback_tolerance)
        touched_from_below = feature["high"] >= vwap * (1 - self.pullback_tolerance)
        if close > vwap and close > ema and touched_from_above:
            action = AdvisoryAction.BUY
        elif close < vwap and close < ema and touched_from_below:
            action = AdvisoryAction.SELL
        else:
            return None
        displacement = abs(close - vwap) / close
        return _signal(
            action,
            close=close,
            atr=feature["atr_14"],
            stop_atr=self.stop_atr,
            reward_risk=self.reward_risk,
            confidence=0.55 + min(displacement * 20, 0.2),
        )


@dataclass(frozen=True)
class EmaRsiContinuationStrategy:
    definition: StrategyDefinition
    buy_rsi_minimum: float = 52
    buy_rsi_maximum: float = 70
    sell_rsi_minimum: float = 30
    sell_rsi_maximum: float = 48
    stop_atr: float = 1.3
    reward_risk: float = 1.8

    required_features: ClassVar[frozenset[str]] = frozenset(
        {"close", "ema_9", "ema_21", "rsi_14", "atr_14", "body_pct"}
    )

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(self.definition, self.required_features, context)

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        compatible, _ = self.compatibility(context)
        if not compatible:
            return None
        feature = context.features
        rsi = feature["rsi_14"]
        if (
            feature["ema_9"] > feature["ema_21"]
            and self.buy_rsi_minimum <= rsi <= self.buy_rsi_maximum
            and feature["body_pct"] > 0
        ):
            action = AdvisoryAction.BUY
        elif (
            feature["ema_9"] < feature["ema_21"]
            and self.sell_rsi_minimum <= rsi <= self.sell_rsi_maximum
            and feature["body_pct"] < 0
        ):
            action = AdvisoryAction.SELL
        else:
            return None
        separation = abs(feature["ema_9"] - feature["ema_21"]) / feature["close"]
        return _signal(
            action,
            close=feature["close"],
            atr=feature["atr_14"],
            stop_atr=self.stop_atr,
            reward_risk=self.reward_risk,
            confidence=0.55 + min(separation * 30, 0.2),
        )


@dataclass(frozen=True)
class SuperTrendAdxStrategy:
    definition: StrategyDefinition
    minimum_adx: float = 25
    stop_atr: float = 1.5
    reward_risk: float = 2.0

    required_features: ClassVar[frozenset[str]] = frozenset(
        {"close", "supertrend", "supertrend_direction", "adx_14", "atr_14"}
    )

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(self.definition, self.required_features, context)

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        compatible, _ = self.compatibility(context)
        if not compatible:
            return None
        feature = context.features
        if feature["adx_14"] < self.minimum_adx:
            return None
        if feature["supertrend_direction"] > 0 and feature["close"] > feature["supertrend"]:
            action = AdvisoryAction.BUY
        elif feature["supertrend_direction"] < 0 and feature["close"] < feature["supertrend"]:
            action = AdvisoryAction.SELL
        else:
            return None
        return _signal(
            action,
            close=feature["close"],
            atr=feature["atr_14"],
            stop_atr=self.stop_atr,
            reward_risk=self.reward_risk,
            confidence=0.55 + min((feature["adx_14"] - self.minimum_adx) / 100, 0.25),
        )


TechnicalStrategy: TypeAlias = (
    VwapPullbackStrategy | EmaRsiContinuationStrategy | SuperTrendAdxStrategy
)


def _definition(
    market: Market,
    strategy_id: str,
    timeframe: str,
    family: str,
    parameters: tuple[tuple[str, str], ...],
    implementation: str,
) -> StrategyDefinition:
    return StrategyDefinition(
        StrategyIdentity(
            market,
            strategy_id,
            "1.0.0",
            timeframe,
            "intraday",
            TECHNICAL_FEATURE_VERSION,
        ),
        family,
        parameters,
        implementation,
    )


def technical_strategies() -> tuple[TechnicalStrategy, ...]:
    """Return definitions only; this function creates no validation or approval artifacts."""
    strategies: list[TechnicalStrategy] = []
    for market in (Market.NSE, Market.CRYPTO):
        definition = _definition(
            market,
            "vwap_pullback",
            "5m",
            "mean_reversion",
            (("pullback_tolerance", "0.0015"), ("minimum_volume_ratio", "0.8")),
            "nanodelta.strategies.technical:VwapPullbackStrategy",
        )
        strategies.append(VwapPullbackStrategy(definition))
    for market in Market:
        definition = _definition(
            market,
            "ema_rsi_continuation",
            "15m",
            "momentum",
            (("ema_fast", "9"), ("ema_slow", "21"), ("rsi_period", "14")),
            "nanodelta.strategies.technical:EmaRsiContinuationStrategy",
        )
        strategies.append(EmaRsiContinuationStrategy(definition))
    for market, timeframe in (
        (Market.NSE, "15m"),
        (Market.FOREX, "1h"),
        (Market.CRYPTO, "15m"),
    ):
        definition = _definition(
            market,
            "supertrend_adx",
            timeframe,
            "trend",
            (("atr_period", "14"), ("atr_multiplier", "3"), ("adx_period", "14")),
            "nanodelta.strategies.technical:SuperTrendAdxStrategy",
        )
        strategies.append(SuperTrendAdxStrategy(definition))
    return tuple(strategies)
