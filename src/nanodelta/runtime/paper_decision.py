"""Composition of Gold features into governed, durable paper decisions."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from nanodelta.contracts import FeatureRecord, Market, stable_id, utc
from nanodelta.decisions import DecisionLedger
from nanodelta.orchestration import (
    AllocationPolicy,
    CyclePreconditions,
    PaperBatchExecutor,
    StagedDecisionPipeline,
)
from nanodelta.orchestration.decision_pipeline import CycleMode
from nanodelta.paper import PaperExecutionEngine
from nanodelta.paper.lifecycle import PaperPositionLifecycle
from nanodelta.persistence.migrations import Connection
from nanodelta.risk import PortfolioSnapshot, RiskEngine
from nanodelta.runtime.correlation import fetch_return_correlations
from nanodelta.runtime.portfolio_snapshot import build_portfolio_snapshot
from nanodelta.runtime.regime import RegimeBreadth, classify_breadth, fetch_breadth_inputs
from nanodelta.runtime.technical_context import latest_technical_snapshot
from nanodelta.strategies import (
    TECHNICAL_FEATURE_VERSION,
    RegimeEvidence,
    StrategyContext,
    StrategyRegistry,
    StrategyRuntimeCatalog,
    SymbolRegimeLimits,
    TradeabilityLimits,
    evaluate_symbol_regime,
    evaluate_tradeability,
)
from nanodelta.universe.sectors import sector_for

if TYPE_CHECKING:
    from nanodelta.observability import RuntimeMetrics


@dataclass(frozen=True)
class PaperDecisionResult:
    """Bounded operational result for one deterministic Gold-input cycle."""

    market: Market
    cycle_id: str
    mode: CycleMode
    candidate_count: int
    allocation_count: int
    risk_decision_count: int
    order_count: int
    exit_count: int


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
        tradeability: TradeabilityLimits,
        symbol_regime: SymbolRegimeLimits,
        max_feature_age_seconds: float = 180,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        metrics: RuntimeMetrics | None = None,
        lifecycle: PaperPositionLifecycle | None = None,
        entry_session_open: Callable[[Market, datetime], bool] = lambda _market, _at: True,
    ) -> None:
        if not account_id.strip() or equity <= 0 or max_feature_age_seconds <= 0:
            raise ValueError("paper account, equity and feature age must be positive")
        self._connect = connect
        self._ledger = ledger
        self._tradeability = tradeability
        self._symbol_regime = symbol_regime
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
        self._market_regime_cache: RegimeBreadth | None = None
        self._market_regime_cache_at: datetime | None = None
        self._sector_regime_cache: dict[str, RegimeBreadth] = {}
        self._sector_regime_cache_at: dict[str, datetime] = {}
        self._lifecycle = lifecycle
        self._entry_session_open = entry_session_open

    def process(
        self,
        features: tuple[FeatureRecord, ...],
        *,
        evaluated_at: datetime | None = None,
    ) -> PaperDecisionResult | None:
        if not features:
            return None
        now = utc(evaluated_at or self._clock(), "evaluated_at")
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
            return PaperDecisionResult(
                market,
                stable_id(
                    "paper-exit-only-cycle",
                    market.value,
                    self._account_id,
                    tuple(sorted(feature.record_id for feature in features)),
                ),
                CycleMode.EXITS_ONLY,
                0,
                0,
                0,
                0,
                len(exited_symbols),
            )
        result = self._pipeline.run(
            contexts,
            preconditions=CyclePreconditions(
                True,
                True,
                self._entry_session_open(market, now),
                True,
            ),
            evaluated_at=now,
            live_quotes={(feature.market, feature.symbol): feature.close for feature in features},
            existing_symbols=frozenset(
                (position.market, position.symbol) for position in portfolio.positions
            ),
            correlations=self._correlations(market, {feature.symbol for feature in eligible}),
        )
        batch = self._batch.execute(
            result,
            account_id=self._account_id,
            portfolio=portfolio,
            evaluated_at=now,
        )
        if self._lifecycle is not None and batch.receipts:
            self._lifecycle.register(result.allocations, batch.receipts)
        return PaperDecisionResult(
            market,
            result.cycle_id,
            result.mode,
            len(result.candidates),
            len(result.allocations),
            len(batch.risk_decisions),
            len(batch.receipts),
            len(exited_symbols),
        )

    def _correlations(
        self, market: Market, symbols: set[str]
    ) -> dict[tuple[str, str], float]:
        if len(symbols) < 2:
            return {}
        connection = self._connect()
        started = time.perf_counter()
        result = "success"
        try:
            return fetch_return_correlations(connection, market, sorted(symbols))
        except Exception:
            result = "error"
            raise
        finally:
            connection.close()
            if self._metrics is not None:
                self._metrics.observe_database(
                    market, "correlations", result, time.perf_counter() - started
                )

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
        sector = sector_for(feature.symbol)
        market_fit, sector_fit = self._market_sector_fit(feature.market, sector, now)
        return StrategyContext(
            feature.market,
            feature.symbol,
            sector,
            feature.timeframe,
            "intraday",
            feature.feature_version,
            feature.event_time,
            (feature.record_id,),
            values,
            fresh=0 <= age <= self._max_age,
            regime=RegimeEvidence(market_fit=market_fit, sector_fit=sector_fit),
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
            snapshot = latest_technical_snapshot(
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
        if snapshot is None:
            return None
        values, candles = snapshot
        tradeable, tradeability_reason = evaluate_tradeability(
            candles, values["atr_14"], self._tradeability
        )
        symbol_fit, symbol_regime_reason = evaluate_symbol_regime(values, self._symbol_regime)
        if symbol_regime_reason.startswith("NO_TREND"):
            # Deterministic strategy router: every technical strategy registered today
            # (VWAP pullback, EMA/RSI continuation, SuperTrend/ADX) is trend-following.
            # There is no ranging/compression strategy family to route a no-trend
            # symbol to instead, so the honest routing decision is to not run the
            # technical path at all this cycle rather than let a trend strategy fire
            # into conditions it wasn't designed for.
            return None
        sector = sector_for(feature.symbol)
        market_fit, sector_fit = self._market_sector_fit(feature.market, sector, now)
        age = (now - feature.event_time.astimezone(UTC)).total_seconds()
        return StrategyContext(
            feature.market,
            feature.symbol,
            sector,
            feature.timeframe,
            "intraday",
            TECHNICAL_FEATURE_VERSION,
            feature.event_time,
            (feature.record_id,),
            values,
            fresh=0 <= age <= self._max_age,
            tradeable=tradeable,
            tradeability_reason=tradeability_reason,
            regime=RegimeEvidence(
                market_fit=market_fit, sector_fit=sector_fit, symbol_fit=symbol_fit
            ),
        )

    _REGIME_TIMEFRAME = "15m"
    _REGIME_CACHE_TTL_SECONDS = 60.0

    def _market_sector_fit(
        self, market: Market, sector: str | None, now: datetime
    ) -> tuple[float, float]:
        market_breadth = self._market_regime(market, now)
        sector_breadth = self._sector_regime(market, sector, now) if sector else None
        sector_fit = sector_breadth.fit if sector_breadth is not None else 1.0
        return market_breadth.fit, sector_fit

    def _regime_universe_symbols(self, connection: Connection, market: Market) -> list[str]:
        cursor = connection.cursor()
        cursor.execute(
            f"SELECT DISTINCT symbol FROM {market.value}_silver.candles "
            "WHERE timeframe=%s AND is_settled=true",
            (self._REGIME_TIMEFRAME,),
        )
        return [str(row[0]) for row in cursor.fetchall()]

    def _market_regime(self, market: Market, now: datetime) -> RegimeBreadth:
        cached_at = self._market_regime_cache_at
        if (
            self._market_regime_cache is not None
            and cached_at is not None
            and (now - cached_at).total_seconds() < self._REGIME_CACHE_TTL_SECONDS
        ):
            return self._market_regime_cache
        connection = self._connect()
        started = time.perf_counter()
        result = "success"
        try:
            symbols = self._regime_universe_symbols(connection, market)
            adx, bullish = fetch_breadth_inputs(
                connection, market, symbols, self._REGIME_TIMEFRAME
            )
        except Exception:
            result = "error"
            raise
        finally:
            connection.close()
            if self._metrics is not None:
                self._metrics.observe_database(
                    market, "market_regime", result, time.perf_counter() - started
                )
        breadth = classify_breadth(adx, bullish)
        self._market_regime_cache = breadth
        self._market_regime_cache_at = now
        return breadth

    def _sector_regime(self, market: Market, sector: str, now: datetime) -> RegimeBreadth:
        cached_at = self._sector_regime_cache_at.get(sector)
        cached = self._sector_regime_cache.get(sector)
        if (
            cached is not None
            and cached_at is not None
            and (now - cached_at).total_seconds() < self._REGIME_CACHE_TTL_SECONDS
        ):
            return cached
        connection = self._connect()
        started = time.perf_counter()
        result = "success"
        try:
            symbols = [
                symbol
                for symbol in self._regime_universe_symbols(connection, market)
                if sector_for(symbol) == sector
            ]
            adx, bullish = fetch_breadth_inputs(
                connection, market, symbols, self._REGIME_TIMEFRAME
            )
        except Exception:
            result = "error"
            raise
        finally:
            connection.close()
            if self._metrics is not None:
                self._metrics.observe_database(
                    market, "sector_regime", result, time.perf_counter() - started
                )
        breadth = classify_breadth(adx, bullish, minimum_symbols=3)
        self._sector_regime_cache[sector] = breadth
        self._sector_regime_cache_at[sector] = now
        return breadth

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
