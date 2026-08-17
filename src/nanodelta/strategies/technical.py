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
    *,
    required_regime_labels: frozenset[str] | None = None,
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
    if (
        required_regime_labels is not None
        and context.regime.symbol_regime_label not in required_regime_labels
    ):
        return False, "REGIME_MISMATCH"
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
    required_regime_labels: ClassVar[frozenset[str]] = frozenset({"TRENDING"})

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(
            self.definition,
            self.required_features,
            context,
            required_regime_labels=self.required_regime_labels,
        )

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
    required_regime_labels: ClassVar[frozenset[str]] = frozenset({"TRENDING"})

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(
            self.definition,
            self.required_features,
            context,
            required_regime_labels=self.required_regime_labels,
        )

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
    required_regime_labels: ClassVar[frozenset[str]] = frozenset({"TRENDING"})

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(
            self.definition,
            self.required_features,
            context,
            required_regime_labels=self.required_regime_labels,
        )

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


@dataclass(frozen=True)
class MeanReversionStrategy:
    """RANGING regime: fades price stretched away from its own mean (EMA-21)
    with RSI confirming the extreme, targeting reversion back to that mean --
    not a fixed R-multiple, since the whole premise is "back toward the
    average", not "away from it"."""

    definition: StrategyDefinition
    oversold_rsi: float = 30.0
    overbought_rsi: float = 70.0
    minimum_stretch_atr: float = 1.5
    stop_atr: float = 1.0
    base_confidence: float = 0.55

    required_features: ClassVar[frozenset[str]] = frozenset(
        {"close", "rsi_14", "atr_14", "ema_21"}
    )
    required_regime_labels: ClassVar[frozenset[str]] = frozenset({"RANGING"})

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(
            self.definition,
            self.required_features,
            context,
            required_regime_labels=self.required_regime_labels,
        )

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        compatible, _ = self.compatibility(context)
        if not compatible:
            return None
        feature = context.features
        close, rsi, atr, mean = (
            feature["close"],
            feature["rsi_14"],
            feature["atr_14"],
            feature["ema_21"],
        )
        if atr <= 0:
            return None
        stretch_atr = (close - mean) / atr
        if rsi <= self.oversold_rsi and stretch_atr <= -self.minimum_stretch_atr:
            action = AdvisoryAction.BUY
        elif rsi >= self.overbought_rsi and stretch_atr >= self.minimum_stretch_atr:
            action = AdvisoryAction.SELL
        else:
            return None
        stop_distance = max(self.stop_atr * atr, close * 0.0005)
        stop = close - stop_distance if action is AdvisoryAction.BUY else close + stop_distance
        target = mean
        if action is AdvisoryAction.BUY and not (stop < close < target):
            return None
        if action is AdvisoryAction.SELL and not (target < close < stop):
            return None
        confidence = self.base_confidence + min(abs(stretch_atr) * 0.05, 0.2)
        return StrategySignal(
            action, max(0.0, min(confidence, 0.95)), close, stop, target, estimated_cost_r=0.05
        )


@dataclass(frozen=True)
class RangeReversalStrategy:
    """RANGING regime: fades a touch of the 20-bar Donchian range edge that
    closes back inside the range -- a real reversal signature (rejection),
    not just any touch."""

    definition: StrategyDefinition
    touch_tolerance_pct: float = 0.002
    stop_atr: float = 1.0
    reward_risk: float = 1.5
    confidence: float = 0.55

    required_features: ClassVar[frozenset[str]] = frozenset(
        {"close", "high", "low", "range_high_20", "range_low_20", "atr_14"}
    )
    required_regime_labels: ClassVar[frozenset[str]] = frozenset({"RANGING"})

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(
            self.definition,
            self.required_features,
            context,
            required_regime_labels=self.required_regime_labels,
        )

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        compatible, _ = self.compatibility(context)
        if not compatible:
            return None
        feature = context.features
        close, high, low = feature["close"], feature["high"], feature["low"]
        range_high, range_low = feature["range_high_20"], feature["range_low_20"]
        touched_low = low <= range_low * (1 + self.touch_tolerance_pct)
        touched_high = high >= range_high * (1 - self.touch_tolerance_pct)
        if touched_low and close > range_low:
            action = AdvisoryAction.BUY
        elif touched_high and close < range_high:
            action = AdvisoryAction.SELL
        else:
            return None
        return _signal(
            action,
            close=close,
            atr=feature["atr_14"],
            stop_atr=self.stop_atr,
            reward_risk=self.reward_risk,
            confidence=self.confidence,
        )


@dataclass(frozen=True)
class SupportReversalStrategy:
    """RANGING regime: like RangeReversalStrategy but anchored on the nearer-
    term 10-bar range and required to be volume-confirmed -- a real, distinct
    signal (a well-participated bounce off a recent level) rather than the
    same check with different constants."""

    definition: StrategyDefinition
    touch_tolerance_pct: float = 0.0015
    minimum_volume_ratio: float = 1.2
    stop_atr: float = 0.8
    reward_risk: float = 1.3
    confidence: float = 0.55

    required_features: ClassVar[frozenset[str]] = frozenset(
        {"close", "high", "low", "range_high_10", "range_low_10", "atr_14", "volume_ratio_20"}
    )
    required_regime_labels: ClassVar[frozenset[str]] = frozenset({"RANGING"})

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(
            self.definition,
            self.required_features,
            context,
            required_regime_labels=self.required_regime_labels,
        )

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        compatible, _ = self.compatibility(context)
        if not compatible:
            return None
        feature = context.features
        if feature["volume_ratio_20"] < self.minimum_volume_ratio:
            return None
        close, high, low = feature["close"], feature["high"], feature["low"]
        range_high, range_low = feature["range_high_10"], feature["range_low_10"]
        touched_low = low <= range_low * (1 + self.touch_tolerance_pct)
        touched_high = high >= range_high * (1 - self.touch_tolerance_pct)
        if touched_low and close > range_low:
            action = AdvisoryAction.BUY
        elif touched_high and close < range_high:
            action = AdvisoryAction.SELL
        else:
            return None
        return _signal(
            action,
            close=close,
            atr=feature["atr_14"],
            stop_atr=self.stop_atr,
            reward_risk=self.reward_risk,
            confidence=self.confidence,
        )


@dataclass(frozen=True)
class BreakoutStrategy:
    """COMPRESSION regime: price expands beyond the 20-bar Donchian range with
    volume confirmation -- a real range-expansion signature, not just any new
    high/low."""

    definition: StrategyDefinition
    minimum_volume_ratio: float = 1.3
    stop_atr: float = 1.2
    reward_risk: float = 2.0
    confidence: float = 0.55

    required_features: ClassVar[frozenset[str]] = frozenset(
        {"close", "range_high_20", "range_low_20", "atr_14", "volume_ratio_20"}
    )
    required_regime_labels: ClassVar[frozenset[str]] = frozenset({"COMPRESSION"})

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(
            self.definition,
            self.required_features,
            context,
            required_regime_labels=self.required_regime_labels,
        )

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        compatible, _ = self.compatibility(context)
        if not compatible:
            return None
        feature = context.features
        if feature["volume_ratio_20"] < self.minimum_volume_ratio:
            return None
        close = feature["close"]
        if close > feature["range_high_20"]:
            action = AdvisoryAction.BUY
        elif close < feature["range_low_20"]:
            action = AdvisoryAction.SELL
        else:
            return None
        return _signal(
            action,
            close=close,
            atr=feature["atr_14"],
            stop_atr=self.stop_atr,
            reward_risk=self.reward_risk,
            confidence=self.confidence,
        )


@dataclass(frozen=True)
class OpeningRangeBreakoutStrategy:
    """COMPRESSION regime, NSE-session-aware: trades a break of the first 15
    minutes' high/low (technical_features.py's opening_range_high/low), the
    conventional ORB definition. Only present in context.features once the
    opening window has actually closed, so no separate time gate is needed
    here."""

    definition: StrategyDefinition
    stop_atr: float = 1.0
    reward_risk: float = 1.8
    confidence: float = 0.6

    required_features: ClassVar[frozenset[str]] = frozenset(
        {"close", "opening_range_high", "opening_range_low", "atr_14"}
    )
    required_regime_labels: ClassVar[frozenset[str]] = frozenset({"COMPRESSION"})

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(
            self.definition,
            self.required_features,
            context,
            required_regime_labels=self.required_regime_labels,
        )

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        compatible, _ = self.compatibility(context)
        if not compatible:
            return None
        feature = context.features
        close = feature["close"]
        if close > feature["opening_range_high"]:
            action = AdvisoryAction.BUY
        elif close < feature["opening_range_low"]:
            action = AdvisoryAction.SELL
        else:
            return None
        return _signal(
            action,
            close=close,
            atr=feature["atr_14"],
            stop_atr=self.stop_atr,
            reward_risk=self.reward_risk,
            confidence=self.confidence,
        )


@dataclass(frozen=True)
class VolumeBreakoutStrategy:
    """COMPRESSION regime: a new 5-bar local high/low confirmed by an unusually
    large volume surge -- distinct from BreakoutStrategy by using a much
    shorter lookback and a materially higher volume bar, representing a sharp,
    immediate move rather than a slower range expansion."""

    definition: StrategyDefinition
    minimum_volume_ratio: float = 2.0
    stop_atr: float = 1.0
    reward_risk: float = 1.8
    confidence: float = 0.55

    required_features: ClassVar[frozenset[str]] = frozenset(
        {"close", "range_high_5", "range_low_5", "atr_14", "volume_ratio_20"}
    )
    required_regime_labels: ClassVar[frozenset[str]] = frozenset({"COMPRESSION"})

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return _compatibility(
            self.definition,
            self.required_features,
            context,
            required_regime_labels=self.required_regime_labels,
        )

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        compatible, _ = self.compatibility(context)
        if not compatible:
            return None
        feature = context.features
        if feature["volume_ratio_20"] < self.minimum_volume_ratio:
            return None
        close = feature["close"]
        if close > feature["range_high_5"]:
            action = AdvisoryAction.BUY
        elif close < feature["range_low_5"]:
            action = AdvisoryAction.SELL
        else:
            return None
        return _signal(
            action,
            close=close,
            atr=feature["atr_14"],
            stop_atr=self.stop_atr,
            reward_risk=self.reward_risk,
            confidence=self.confidence,
        )


TechnicalStrategy: TypeAlias = (
    VwapPullbackStrategy
    | EmaRsiContinuationStrategy
    | SuperTrendAdxStrategy
    | MeanReversionStrategy
    | RangeReversalStrategy
    | SupportReversalStrategy
    | BreakoutStrategy
    | OpeningRangeBreakoutStrategy
    | VolumeBreakoutStrategy
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
    strategies.append(
        MeanReversionStrategy(
            _definition(
                Market.NSE,
                "mean_reversion_rsi",
                "15m",
                "mean_reversion_rsi",
                (("oversold_rsi", "30"), ("overbought_rsi", "70")),
                "nanodelta.strategies.technical:MeanReversionStrategy",
            )
        )
    )
    strategies.append(
        RangeReversalStrategy(
            _definition(
                Market.NSE,
                "range_reversal",
                "15m",
                "range_reversal",
                (("range_period", "20"), ("touch_tolerance_pct", "0.002")),
                "nanodelta.strategies.technical:RangeReversalStrategy",
            )
        )
    )
    strategies.append(
        SupportReversalStrategy(
            _definition(
                Market.NSE,
                "support_reversal",
                "15m",
                "support_reversal",
                (("range_period", "10"), ("minimum_volume_ratio", "1.2")),
                "nanodelta.strategies.technical:SupportReversalStrategy",
            )
        )
    )
    strategies.append(
        BreakoutStrategy(
            _definition(
                Market.NSE,
                "breakout",
                "15m",
                "breakout",
                (("range_period", "20"), ("minimum_volume_ratio", "1.3")),
                "nanodelta.strategies.technical:BreakoutStrategy",
            )
        )
    )
    strategies.append(
        OpeningRangeBreakoutStrategy(
            _definition(
                Market.NSE,
                "opening_range_breakout",
                "15m",
                "orb",
                (("opening_range_minutes", "15"),),
                "nanodelta.strategies.technical:OpeningRangeBreakoutStrategy",
            )
        )
    )
    strategies.append(
        VolumeBreakoutStrategy(
            _definition(
                Market.NSE,
                "volume_breakout",
                "15m",
                "volume_breakout",
                (("range_period", "5"), ("minimum_volume_ratio", "2.0")),
                "nanodelta.strategies.technical:VolumeBreakoutStrategy",
            )
        )
    )
    return tuple(strategies)
