"""Database-backed, deterministic validation of registered strategy plugins."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from nanodelta.contracts import AdvisoryAction, Market
from nanodelta.persistence.migrations import Connection
from nanodelta.strategies.registry import StrategyRegistry
from nanodelta.strategies.runtime import StrategyContext, StrategyPlugin
from nanodelta.strategies.validation import (
    ValidationMetrics,
    ValidationPolicy,
    ValidationResult,
    validate_strategy,
)


def _one_sided_sign_test(wins: int, sample: int) -> float:
    if sample == 0:
        return 1.0
    tail = sum(int(math.comb(sample, index)) for index in range(wins, sample + 1))
    return min(1.0, float(tail) / float(2**sample))


def _maximum_drawdown(returns: Sequence[float]) -> float:
    equity = peak = 1.0
    maximum = 0.0
    for value in returns:
        equity *= max(0.0, 1 + value)
        peak = max(peak, equity)
        maximum = max(maximum, 0.0 if peak == 0 else (peak - equity) / peak)
    return maximum


class PostgresStrategyEvaluator:
    """Evaluates a plugin from immutable Gold rows and persists its evidence."""

    def __init__(self, connect: Callable[[], Connection], registry: StrategyRegistry) -> None:
        self._connect = connect
        self._registry = registry

    def evaluate(
        self,
        plugin: StrategyPlugin,
        *,
        policy: ValidationPolicy,
        estimated_round_trip_cost: float,
        tested_hypotheses: int,
        evaluated_at: datetime | None = None,
    ) -> ValidationResult:
        if estimated_round_trip_cost < 0 or tested_hypotheses < 1:
            raise ValueError("cost must be non-negative and hypotheses must be positive")
        contexts = self._load_contexts(plugin)
        trade_returns: list[float] = []
        window_returns = [0.0] * policy.minimum_walk_forward_windows
        evaluated_index = 0
        for index, context in enumerate(contexts[:-1]):
            if contexts[index + 1].symbol != context.symbol:
                continue
            compatible, _ = plugin.compatibility(context)
            signal = plugin.generate(context) if compatible else None
            if signal is None:
                continue
            next_close = contexts[index + 1].features["close"]
            direction = 1.0 if signal.action is AdvisoryAction.BUY else -1.0
            gross = direction * (next_close - signal.reference_price) / signal.reference_price
            trade_returns.append(gross)
            window = min(
                len(window_returns) - 1,
                evaluated_index * len(window_returns) // max(1, len(contexts) - 1),
            )
            window_returns[window] += gross - estimated_round_trip_cost
            evaluated_index += 1
        count = len(trade_returns)
        metrics = ValidationMetrics(
            trade_count=count,
            walk_forward_windows=len(window_returns),
            profitable_windows=sum(value > 0 for value in window_returns),
            gross_expectancy=sum(trade_returns) / count if count else 0.0,
            estimated_cost_per_trade=estimated_round_trip_cost,
            maximum_drawdown=_maximum_drawdown(
                [value - estimated_round_trip_cost for value in trade_returns]
            ),
            p_value=_one_sided_sign_test(
                sum(value > estimated_round_trip_cost for value in trade_returns), count
            ),
            tested_hypotheses=tested_hypotheses,
        )
        result = validate_strategy(
            plugin.definition.identity,
            metrics,
            policy,
            evaluated_at=evaluated_at or datetime.now(UTC),
        )
        self._registry.record_validation(result)
        return result

    def _load_contexts(self, plugin: StrategyPlugin) -> tuple[StrategyContext, ...]:
        identity = plugin.definition.identity
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                f"SELECT record_id,symbol,timeframe,event_time,feature_version,features "
                f"FROM {identity.market.value}_gold.feature_snapshots "
                "WHERE timeframe=%s AND feature_version=%s ORDER BY symbol,event_time",
                (identity.timeframe, identity.feature_set_version),
            )
            return tuple(
                self._context(identity.market, identity.trade_horizon, row)
                for row in cursor.fetchall()
            )
        finally:
            connection.close()

    @staticmethod
    def _context(market: Market, horizon: str, row: tuple[object, ...]) -> StrategyContext:
        raw = cast(Any, row[5])
        values = cast(dict[str, object], raw if isinstance(raw, dict) else json.loads(str(raw)))
        return StrategyContext(
            market=market,
            symbol=str(row[1]),
            sector=None,
            timeframe=str(row[2]),
            trade_horizon=horizon,
            feature_set_version=int(cast(Any, row[4])),
            event_time=cast(datetime, row[3]),
            gold_snapshot_ids=(str(row[0]),),
            features={name: float(cast(Any, value)) for name, value in values.items()},
        )
