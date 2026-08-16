"""Initial deterministic strategies. All calculations use closed bars only."""

from __future__ import annotations

from dataclasses import dataclass

from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.strategies.registry import StrategyDefinition, StrategyIdentity
from nanodelta.strategies.runtime import ClosedBar, StrategyContext, StrategySignal


def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    seed = sum(values[:period]) / period
    result = [seed]
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def _rsi(values: list[float], period: int) -> list[float]:
    if len(values) <= period:
        return []
    changes = [right - left for left, right in zip(values, values[1:], strict=False)]
    gain = sum(max(change, 0) for change in changes[:period]) / period
    loss = sum(max(-change, 0) for change in changes[:period]) / period
    result: list[float] = [100 if loss == 0 else 100 - 100 / (1 + gain / loss)]
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
        result.append(100 if loss == 0 else 100 - 100 / (1 + gain / loss))
    return result


def _true_ranges(bars: tuple[ClosedBar, ...]) -> list[float]:
    return [
        max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
        for previous, bar in zip(bars, bars[1:], strict=False)
    ]


def _atr(bars: tuple[ClosedBar, ...], period: int) -> float | None:
    ranges = _true_ranges(bars)
    if len(ranges) < period:
        return None
    value = sum(ranges[:period]) / period
    for item in ranges[period:]:
        value = (value * (period - 1) + item) / period
    return value


def _signal(
    action: AdvisoryAction, price: float, atr: float, confidence: float, cost_r: float
) -> StrategySignal:
    risk = max(atr, price * 0.001)
    if action is AdvisoryAction.BUY:
        stop, target = price - risk, price + risk * 2
    else:
        stop, target = price + risk, price - risk * 2
    return StrategySignal(action, confidence, price, stop, target, estimated_cost_r=cost_r)


@dataclass(frozen=True)
class StrategySpec:
    market: Market
    timeframe: str
    trade_horizon: str = "intraday"
    feature_set_version: int = 1
    version: str = "1.0.0"


class _BaseStrategy:
    minimum_bars: int
    definition: StrategyDefinition

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        identity = self.definition.identity
        if context.market is not identity.market:
            return False, "MARKET_NOT_SUPPORTED"
        if context.timeframe != identity.timeframe:
            return False, "TIMEFRAME_NOT_SUPPORTED"
        if context.trade_horizon != identity.trade_horizon:
            return False, "HORIZON_NOT_SUPPORTED"
        if context.feature_set_version != identity.feature_set_version:
            return False, "FEATURE_VERSION_NOT_SUPPORTED"
        if len(context.closed_bars) < self.minimum_bars:
            return False, "INSUFFICIENT_CLOSED_BARS"
        return True, "COMPATIBLE"


@dataclass(frozen=True)
class VwapPullbackParameters:
    ema_period: int = 20
    atr_period: int = 14
    cost_r: float = 0.06


class VwapPullbackStrategy(_BaseStrategy):
    """Session VWAP reclaim/rejection for real-volume NSE and Crypto instruments."""

    def __init__(
        self, spec: StrategySpec, params: VwapPullbackParameters = VwapPullbackParameters()
    ):
        if spec.market not in {Market.NSE, Market.CRYPTO}:
            raise ValueError("VWAP pullback requires real traded volume; Forex is not supported")
        self.params = params
        self.minimum_bars = max(params.ema_period + 1, params.atr_period + 2)
        identity = StrategyIdentity(
            spec.market,
            "vwap_pullback",
            spec.version,
            spec.timeframe,
            spec.trade_horizon,
            spec.feature_set_version,
        )
        self.definition = StrategyDefinition(
            identity,
            "vwap_pullback",
            (
                ("ema_period", str(params.ema_period)),
                ("atr_period", str(params.atr_period)),
                ("cost_r", str(params.cost_r)),
            ),
            "nanodelta.strategies.initial:VwapPullbackStrategy",
        )

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        bars = context.closed_bars
        volumes = [bar.volume for bar in bars]
        if sum(volumes) <= 0:
            return None
        typical = [(bar.high + bar.low + bar.close) / 3 for bar in bars]
        cumulative_volume = 0.0
        cumulative_value = 0.0
        vwaps: list[float] = []
        for price, volume in zip(typical, volumes, strict=True):
            cumulative_volume += volume
            cumulative_value += price * volume
            vwaps.append(cumulative_value / cumulative_volume if cumulative_volume else price)
        closes = [bar.close for bar in bars]
        ema = _ema(closes, self.params.ema_period)
        atr = _atr(bars, self.params.atr_period)
        if atr is None or len(ema) < 2:
            return None
        action: AdvisoryAction | None = None
        if closes[-2] <= vwaps[-2] and closes[-1] > vwaps[-1] and closes[-1] > ema[-1]:
            action = AdvisoryAction.BUY
        elif closes[-2] >= vwaps[-2] and closes[-1] < vwaps[-1] and closes[-1] < ema[-1]:
            action = AdvisoryAction.SELL
        return (
            None if action is None else _signal(action, closes[-1], atr, 0.65, self.params.cost_r)
        )


@dataclass(frozen=True)
class EmaRsiParameters:
    fast_ema: int = 9
    slow_ema: int = 21
    rsi_period: int = 14
    buy_rsi: float = 55
    sell_rsi: float = 45
    atr_period: int = 14
    cost_r: float = 0.06


class EmaRsiMomentumStrategy(_BaseStrategy):
    def __init__(self, spec: StrategySpec, params: EmaRsiParameters = EmaRsiParameters()):
        if params.fast_ema >= params.slow_ema:
            raise ValueError("fast EMA must be shorter than slow EMA")
        self.params = params
        self.minimum_bars = max(params.slow_ema + 2, params.rsi_period + 2, params.atr_period + 2)
        identity = StrategyIdentity(
            spec.market,
            "ema_rsi_momentum",
            spec.version,
            spec.timeframe,
            spec.trade_horizon,
            spec.feature_set_version,
        )
        self.definition = StrategyDefinition(
            identity,
            "momentum",
            tuple((name, str(value)) for name, value in vars(params).items()),
            "nanodelta.strategies.initial:EmaRsiMomentumStrategy",
        )

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        closes = [bar.close for bar in context.closed_bars]
        fast, slow = _ema(closes, self.params.fast_ema), _ema(closes, self.params.slow_ema)
        rsi, atr = (
            _rsi(closes, self.params.rsi_period),
            _atr(context.closed_bars, self.params.atr_period),
        )
        if min(len(fast), len(slow), len(rsi)) < 2 or atr is None:
            return None
        action: AdvisoryAction | None = None
        if fast[-2] <= slow[-2] and fast[-1] > slow[-1] and rsi[-1] >= self.params.buy_rsi:
            action = AdvisoryAction.BUY
        elif fast[-2] >= slow[-2] and fast[-1] < slow[-1] and rsi[-1] <= self.params.sell_rsi:
            action = AdvisoryAction.SELL
        return None if action is None else _signal(action, closes[-1], atr, 0.7, self.params.cost_r)


@dataclass(frozen=True)
class SuperTrendAdxParameters:
    atr_period: int = 14
    multiplier: float = 3.0
    adx_period: int = 14
    minimum_adx: float = 20
    cost_r: float = 0.06


def _adx(bars: tuple[ClosedBar, ...], period: int) -> float | None:
    if len(bars) < period * 2 + 1:
        return None
    trs, plus, minus = [], [], []
    for previous, bar in zip(bars, bars[1:], strict=False):
        trs.append(
            max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
        )
        up, down = bar.high - previous.high, previous.low - bar.low
        plus.append(up if up > down and up > 0 else 0)
        minus.append(down if down > up and down > 0 else 0)
    dx: list[float] = []
    for end in range(period, len(trs) + 1):
        tr = sum(trs[end - period : end])
        if tr == 0:
            dx.append(0)
            continue
        pdi = 100 * sum(plus[end - period : end]) / tr
        mdi = 100 * sum(minus[end - period : end]) / tr
        dx.append(0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    return sum(dx[-period:]) / period if len(dx) >= period else None


class SuperTrendAdxStrategy(_BaseStrategy):
    def __init__(
        self, spec: StrategySpec, params: SuperTrendAdxParameters = SuperTrendAdxParameters()
    ):
        self.params = params
        self.minimum_bars = max(params.atr_period + 3, params.adx_period * 2 + 1)
        identity = StrategyIdentity(
            spec.market,
            "supertrend_adx",
            spec.version,
            spec.timeframe,
            spec.trade_horizon,
            spec.feature_set_version,
        )
        self.definition = StrategyDefinition(
            identity,
            "trend",
            tuple((name, str(value)) for name, value in vars(params).items()),
            "nanodelta.strategies.initial:SuperTrendAdxStrategy",
        )

    def generate(self, context: StrategyContext) -> StrategySignal | None:
        bars = context.closed_bars
        atr, adx = _atr(bars, self.params.atr_period), _adx(bars, self.params.adx_period)
        if atr is None or adx is None or adx < self.params.minimum_adx:
            return None
        previous_mid = (bars[-2].high + bars[-2].low) / 2
        previous_upper, previous_lower = (
            previous_mid + self.params.multiplier * atr,
            previous_mid - self.params.multiplier * atr,
        )
        action: AdvisoryAction | None = None
        if bars[-2].close <= previous_upper and bars[-1].close > previous_upper:
            action = AdvisoryAction.BUY
        elif bars[-2].close >= previous_lower and bars[-1].close < previous_lower:
            action = AdvisoryAction.SELL
        return (
            None
            if action is None
            else _signal(
                action, bars[-1].close, atr, min(0.85, adx / 100 + 0.5), self.params.cost_r
            )
        )
