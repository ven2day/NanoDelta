"""Offline, next-bar, cost-aware strategy replay and walk-forward validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from nanodelta.contracts import AdvisoryAction
from nanodelta.strategies.runtime import ClosedBar, StrategyContext, StrategyPlugin
from nanodelta.strategies.validation import ValidationMetrics


@dataclass(frozen=True)
class BacktestPolicy:
    warmup_bars: int = 50
    maximum_holding_bars: int = 8
    fee_bps_each_side: float = 2.0
    slippage_bps_each_side: float = 3.0
    walk_forward_windows: int = 5
    risk_fraction_per_trade: float = 0.01

    def __post_init__(self) -> None:
        if self.warmup_bars < 2 or self.maximum_holding_bars < 1:
            raise ValueError("warmup and holding bars must be positive")
        if self.fee_bps_each_side < 0 or self.slippage_bps_each_side < 0:
            raise ValueError("costs cannot be negative")
        if self.walk_forward_windows < 1:
            raise ValueError("walk-forward windows must be positive")
        if not 0 < self.risk_fraction_per_trade <= 1:
            raise ValueError("risk fraction must be in (0, 1]")


@dataclass(frozen=True)
class BacktestTrade:
    signal_index: int
    entry_index: int
    exit_index: int
    action: AdvisoryAction
    gross_r: float
    cost_r: float
    net_r: float


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[BacktestTrade, ...]
    window_net_expectancies: tuple[float, ...]
    metrics: ValidationMetrics


def _context(plugin: StrategyPlugin, bars: tuple[ClosedBar, ...], index: int) -> StrategyContext:
    identity = plugin.definition.identity
    decision_time = (
        bars[index + 1].open_time
        if index + 1 < len(bars)
        else bars[index].open_time + timedelta(days=1)
    )
    return StrategyContext(
        market=identity.market,
        symbol="OFFLINE_FIXTURE",
        sector=None,
        timeframe=identity.timeframe,
        trade_horizon=identity.trade_horizon,
        feature_set_version=identity.feature_set_version,
        event_time=decision_time,
        gold_snapshot_ids=(f"offline-{index}",),
        features={"close": bars[index].close},
        closed_bars=bars[: index + 1],
    )


def _trade_return(
    signal_index: int,
    signal: object,
    bars: tuple[ClosedBar, ...],
    policy: BacktestPolicy,
) -> BacktestTrade:
    # Signal is calculated after bar N closes; bar N+1 open is the earliest legal fill.
    from nanodelta.strategies.runtime import StrategySignal

    if not isinstance(signal, StrategySignal):
        raise TypeError("signal must be StrategySignal")
    entry_index = signal_index + 1
    entry = bars[entry_index].open
    risk = abs(signal.reference_price - signal.stop_price)
    if risk <= 0:
        raise ValueError("signal risk must be positive")
    exit_price = bars[min(entry_index + policy.maximum_holding_bars - 1, len(bars) - 1)].close
    exit_index = min(entry_index + policy.maximum_holding_bars - 1, len(bars) - 1)
    for index in range(entry_index, exit_index + 1):
        bar = bars[index]
        if signal.action is AdvisoryAction.BUY:
            # When both occur in one OHLC bar, use the adverse stop-first assumption.
            if bar.low <= signal.stop_price:
                exit_price, exit_index = signal.stop_price, index
                break
            if bar.high >= signal.target_price:
                exit_price, exit_index = signal.target_price, index
                break
        else:
            if bar.high >= signal.stop_price:
                exit_price, exit_index = signal.stop_price, index
                break
            if bar.low <= signal.target_price:
                exit_price, exit_index = signal.target_price, index
                break
    gross = (exit_price - entry) / risk
    if signal.action is AdvisoryAction.SELL:
        gross = -gross
    round_trip_fraction = 2 * (policy.fee_bps_each_side + policy.slippage_bps_each_side) / 10_000
    cost_r = entry * round_trip_fraction / risk
    return BacktestTrade(
        signal_index, entry_index, exit_index, signal.action, gross, cost_r, gross - cost_r
    )


def _two_sided_sign_test(values: list[float]) -> float:
    nonzero = [value for value in values if value != 0]
    n = len(nonzero)
    if n == 0:
        return 1.0
    wins = sum(1 for value in nonzero if value > 0)
    tail = min(wins, n - wins)
    probability = sum(math.comb(n, k) for k in range(tail + 1)) / (2**n)
    return float(min(1.0, 2 * probability))


def replay_strategy(
    plugin: StrategyPlugin,
    bars: tuple[ClosedBar, ...],
    policy: BacktestPolicy,
    *,
    tested_hypotheses: int,
) -> BacktestResult:
    if tested_hypotheses < 1:
        raise ValueError("tested_hypotheses must be positive")
    trades: list[BacktestTrade] = []
    last_exit = -1
    final_signal_index = len(bars) - 2
    for index in range(policy.warmup_bars - 1, final_signal_index + 1):
        if index < last_exit:
            continue
        context = _context(plugin, bars, index)
        compatible, _ = plugin.compatibility(context)
        if not compatible:
            continue
        signal = plugin.generate(context)
        if signal is None:
            continue
        trade = _trade_return(index, signal, bars, policy)
        trades.append(trade)
        last_exit = trade.exit_index

    windows: list[list[float]] = [[] for _ in range(policy.walk_forward_windows)]
    denominator = max(1, len(bars))
    for trade in trades:
        window = min(
            policy.walk_forward_windows - 1,
            trade.signal_index * policy.walk_forward_windows // denominator,
        )
        windows[window].append(trade.net_r)
    expectancies = tuple(sum(window) / len(window) if window else 0.0 for window in windows)
    net_values = [trade.net_r for trade in trades]
    gross_values = [trade.gross_r for trade in trades]
    equity = peak = 0.0
    maximum_drawdown = 0.0
    for value in net_values:
        equity += value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    metrics = ValidationMetrics(
        trade_count=len(trades),
        walk_forward_windows=policy.walk_forward_windows,
        profitable_windows=sum(value > 0 for value in expectancies),
        gross_expectancy=sum(gross_values) / len(gross_values) if gross_values else 0.0,
        estimated_cost_per_trade=(
            sum(trade.cost_r for trade in trades) / len(trades) if trades else 0.0
        ),
        maximum_drawdown=maximum_drawdown * policy.risk_fraction_per_trade,
        p_value=_two_sided_sign_test(net_values),
        tested_hypotheses=tested_hypotheses,
    )
    return BacktestResult(tuple(trades), expectancies, metrics)
