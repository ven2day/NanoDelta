"""Composition of Gold features into governed, durable paper decisions."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from nanodelta.contracts import FeatureRecord, Market
from nanodelta.decisions import DecisionLedger
from nanodelta.orchestration import (
    AllocationPolicy,
    CyclePreconditions,
    PaperBatchExecutor,
    StagedDecisionPipeline,
)
from nanodelta.paper import PaperExecutionEngine
from nanodelta.paper.lifecycle import PaperPositionLifecycle
from nanodelta.persistence.migrations import Connection
from nanodelta.risk import PortfolioSnapshot, RiskEngine
from nanodelta.runtime.portfolio_snapshot import build_portfolio_snapshot
from nanodelta.runtime.technical_context import latest_technical_features
from nanodelta.strategies import (
    TECHNICAL_FEATURE_VERSION,
    StrategyContext,
    StrategyRegistry,
    StrategyRuntimeCatalog,
)

if TYPE_CHECKING:
    from nanodelta.observability import RuntimeMetrics


class PaperDecisionService:
    """Runs one deterministic paper decision cycle for newly materialized Gold rows."""

    def __init__(
        self,
        *,
        connect: Callable[[], Connection],
        registry: StrategyRegistry,
        catalog: StrategyRuntimeCatalog,
        ledger: DecisionLedger,
        risk: RiskEngine,
        execution: PaperExecutionEngine,
        allocation: AllocationPolicy,
        account_id: str,
        equity: float,
        max_feature_age_seconds: float = 180,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        metrics: RuntimeMetrics | None = None,
        lifecycle: PaperPositionLifecycle | None = None,
    ) -> None:
        if not account_id.strip() or equity <= 0 or max_feature_age_seconds <= 0:
            raise ValueError("paper account, equity and feature age must be positive")
        self._connect = connect
        self._ledger = ledger
        self._pipeline = StagedDecisionPipeline(
            registry=registry,
            strategies=catalog,
            ledger=ledger,
            allocation_policy=allocation,
        )
        self._batch = PaperBatchExecutor(
            registry=registry,
            risk=risk,
            execution=execution,
            ledger=ledger,
        )
        self._account_id = account_id
        self._equity = equity
        self._max_age = max_feature_age_seconds
        self._clock = clock
        self._metrics = metrics
        self._lifecycle = lifecycle

    def process(self, features: tuple[FeatureRecord, ...]) -> None:
        if not features:
            return
        now = self._clock()
        markets = {feature.market for feature in features}
        if len(markets) != 1:
            raise ValueError("one paper decision cycle cannot mix markets")
        market = next(iter(markets))
        marks = self._latest_marks(market)
        for feature in features:
            marks[feature.symbol] = feature.close
        portfolio = self._portfolio(market, marks, now)
        exited_symbols: set[str] = set()
        if self._lifecycle is not None:
            outcomes = self._lifecycle.manage(
                market=market,
                account_id=self._account_id,
                marks=marks,
                portfolio=portfolio,
                gold_snapshot_ids={feature.symbol: feature.record_id for feature in features},
                evaluated_at=now,
            )
            exited_symbols = {outcome.symbol for outcome in outcomes}
            if outcomes:
                portfolio = self._portfolio(market, marks, now)
        eligible = [feature for feature in features if feature.symbol not in exited_symbols]
        basic_contexts = tuple(self._basic_context(feature, now) for feature in eligible)
        technical_contexts = []
        for feature in eligible:
            technical = self._technical_context(feature, now)
            if technical is not None:
                technical_contexts.append(technical)
        contexts = basic_contexts + tuple(technical_contexts)
        if not contexts:
            return
        result = self._pipeline.run(
            contexts,
            preconditions=CyclePreconditions(True, True, True, True),
            evaluated_at=now,
            live_quotes={(feature.market, feature.symbol): feature.close for feature in features},
            existing_symbols=frozenset(
                (position.market, position.symbol) for position in portfolio.positions
            ),
        )
        batch = self._batch.execute(
            result,
            account_id=self._account_id,
            portfolio=portfolio,
            evaluated_at=now,
        )
        if self._lifecycle is not None and batch.receipts:
            self._lifecycle.register(result.allocations, batch.receipts)

    def _portfolio(
        self, market: Market, marks: dict[str, float], now: datetime
    ) -> PortfolioSnapshot:
        connection = self._connect()
        started = time.perf_counter()
        result = "success"
        try:
            return build_portfolio_snapshot(
                connection,
                market=market,
                account_id=self._account_id,
                equity=self._equity,
                mark_prices=marks,
                now=now,
            )
        except Exception:
            result = "error"
            raise
        finally:
            connection.close()
            if self._metrics is not None:
                self._metrics.observe_database(
                    market, "portfolio_snapshot", result, time.perf_counter() - started
                )

    def _basic_context(self, feature: FeatureRecord, now: datetime) -> StrategyContext:
        age = (now - feature.event_time.astimezone(UTC)).total_seconds()
        values: dict[str, float] = {
            "close": feature.close,
            "return_1": feature.return_1,
            "range_pct": feature.range_pct,
            "body_pct": feature.body_pct,
        }
        if feature.volume_change is not None:
            values["volume_change"] = feature.volume_change
        return StrategyContext(
            feature.market,
            feature.symbol,
            None,
            feature.timeframe,
            "intraday",
            feature.feature_version,
            feature.event_time,
            (feature.record_id,),
            values,
            fresh=0 <= age <= self._max_age,
        )

    def _technical_context(self, feature: FeatureRecord, now: datetime) -> StrategyContext | None:
        """VWAP/EMA-RSI/SuperTrend strategies need real indicator values, computed
        from a window of settled candles -- not the single-candle basic features
        momentum uses. Returns None when there isn't yet enough settled history for
        every indicator to warm up; that's an expected state for a newly tracked
        symbol, not an error."""
        connection = self._connect()
        started = time.perf_counter()
        result = "success"
        try:
            values = latest_technical_features(
                connection, feature.market, feature.symbol, feature.timeframe
            )
        except Exception:
            result = "error"
            raise
        finally:
            connection.close()
            if self._metrics is not None:
                self._metrics.observe_database(
                    feature.market, "technical_features", result, time.perf_counter() - started
                )
        if values is None:
            return None
        age = (now - feature.event_time.astimezone(UTC)).total_seconds()
        return StrategyContext(
            feature.market,
            feature.symbol,
            None,
            feature.timeframe,
            "intraday",
            TECHNICAL_FEATURE_VERSION,
            feature.event_time,
            (feature.record_id,),
            values,
            fresh=0 <= age <= self._max_age,
        )

    def _latest_marks(self, market: Market) -> dict[str, float]:
        connection = self._connect()
        started = time.perf_counter()
        result = "success"
        try:
            cursor = connection.cursor()
            cursor.execute(
                f"SELECT DISTINCT ON (symbol) symbol,close FROM {market.value}_silver.candles "
                "WHERE is_settled=true ORDER BY symbol,open_time DESC"
            )
            return {str(row[0]): float(cast(Any, row[1])) for row in cursor.fetchall()}
        except Exception:
            result = "error"
            raise
        finally:
            connection.close()
            if self._metrics is not None:
                self._metrics.observe_database(
                    market, "latest_marks", result, time.perf_counter() - started
                )
