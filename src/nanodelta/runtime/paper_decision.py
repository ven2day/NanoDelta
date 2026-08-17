"""Composition of Gold features into governed, durable paper decisions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

from nanodelta.contracts import FeatureRecord, Market, stable_id, utc
from nanodelta.decisions import DecisionLedger
from nanodelta.orchestration import (
    AllocationPolicy,
    CyclePreconditions,
    PaperBatchExecutor,
    StagedDecisionPipeline,
)
from nanodelta.orchestration.decision_pipeline import CandidateReviewer, CycleMode, LlmReviewMode
from nanodelta.paper import PaperExecutionEngine
from nanodelta.paper.lifecycle import PaperPositionLifecycle
from nanodelta.persistence.migrations import Connection
from nanodelta.providers.dhan import QuoteSnapshot
from nanodelta.risk import PortfolioSnapshot, RiskEngine
from nanodelta.runtime.correlation import fetch_return_correlations
from nanodelta.runtime.index_feed import MARKET_INDEX_SYMBOL, SECTOR_INDEX_SYMBOL, VIX_SYMBOL
from nanodelta.runtime.portfolio_snapshot import build_portfolio_snapshot
from nanodelta.runtime.regime import (
    RegimeBreadth,
    classify_breadth,
    classify_index_regime,
    classify_risk_off,
    fetch_breadth_inputs,
    fetch_index_snapshot,
)
from nanodelta.runtime.technical_context import latest_technical_features, latest_technical_snapshot
from nanodelta.strategies import (
    TECHNICAL_FEATURE_VERSION,
    RegimeEvidence,
    StrategyContext,
    StrategyRegistry,
    StrategyRuntimeCatalog,
    SymbolRegimeLimits,
    TradeabilityLimits,
    classify_regime_label,
    evaluate_mtf_alignment,
    evaluate_symbol_regime,
    evaluate_tradeability,
)
from nanodelta.universe.sectors import sector_for

if TYPE_CHECKING:
    from nanodelta.observability import RuntimeMetrics

logger = logging.getLogger("nanodelta.runtime.paper_decision")

T = TypeVar("T")


def _run_sync(coroutine: Coroutine[object, object, T]) -> T:
    """Same sync-bridge pattern as llm_review.py / api/runtime.py -- process()
    is synchronous, but fetch_quotes is async. A dedicated thread gets its own
    fresh event loop so this can't collide with a caller's running loop."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


class QuoteFetcher(Protocol):
    async def fetch_quotes(self, symbols: Sequence[str]) -> Mapping[str, QuoteSnapshot]: ...


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
        llm_mode: LlmReviewMode = LlmReviewMode.OFF,
        reviewer: CandidateReviewer | None = None,
        quote_client: QuoteFetcher | None = None,
    ) -> None:
        if not account_id.strip() or equity <= 0 or max_feature_age_seconds <= 0:
            raise ValueError("paper account, equity and feature age must be positive")
        self._connect = connect
        self._ledger = ledger
        self._tradeability = tradeability
        self._symbol_regime = symbol_regime
        self._quote_client = quote_client
        self._quote_cache: dict[str, QuoteSnapshot] = {}
        self._quote_cache_at: dict[str, datetime] = {}
        self._pipeline = StagedDecisionPipeline(
            registry=registry,
            strategies=catalog,
            ledger=ledger,
            allocation_policy=allocation,
            llm_mode=llm_mode,
            reviewer=reviewer,
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
        self._refresh_quotes({feature.symbol for feature in eligible}, now)
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
        quote = self._quote_cache.get(feature.symbol)
        tradeable, tradeability_reason = evaluate_tradeability(
            candles,
            values["atr_14"],
            self._tradeability,
            timeframe=feature.timeframe,
            circuit_limits=(
                (quote.lower_circuit_limit, quote.upper_circuit_limit)
                if quote is not None
                else None
            ),
            best_bid=quote.best_bid if quote is not None else None,
            best_ask=quote.best_ask if quote is not None else None,
        )
        symbol_fit, _symbol_fit_reason = evaluate_symbol_regime(values, self._symbol_regime)
        # Deterministic strategy router: which strategy family even gets tried is
        # decided by this discrete regime label (see technical.py's
        # required_regime_labels on each strategy) -- TRENDING/RANGING/COMPRESSION,
        # not the continuous symbol_fit score above, which still applies within
        # whichever family runs.
        regime_label = classify_regime_label(values, self._symbol_regime)
        sector = sector_for(feature.symbol)
        market_fit, sector_fit = self._market_sector_fit(feature.market, sector, now)
        mtf_alignment = self._mtf_alignment(feature, values)
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
                market_fit=market_fit,
                sector_fit=sector_fit,
                symbol_fit=symbol_fit,
                mtf_alignment=mtf_alignment,
                symbol_regime_label=regime_label,
            ),
        )

    _HIGHER_TIMEFRAME = {"1m": "5m", "5m": "15m", "15m": "1h", "30m": "1h", "1h": "1d"}
    _REGIME_TIMEFRAME = "15m"
    _REGIME_CACHE_TTL_SECONDS = 60.0
    _QUOTE_CACHE_TTL_SECONDS = 30.0

    def _refresh_quotes(self, symbols: set[str], now: datetime) -> None:
        """Batched, TTL-cached circuit-limit/spread refresh -- one REST call
        per stale batch instead of one per symbol, staying well under Dhan's
        quote-endpoint rate limit. A fetch failure is logged and skipped, not
        raised: circuit/spread checks simply stay unavailable for this cycle
        rather than crashing an otherwise-healthy decision cycle."""
        if self._quote_client is None or not symbols:
            return
        stale = {
            symbol
            for symbol in symbols
            if symbol not in self._quote_cache_at
            or (now - self._quote_cache_at[symbol]).total_seconds() >= self._QUOTE_CACHE_TTL_SECONDS
        }
        if not stale:
            return
        try:
            fetched = _run_sync(self._quote_client.fetch_quotes(sorted(stale)))
        except Exception:
            logger.exception("quote refresh failed")
            return
        for symbol in stale:
            snapshot = fetched.get(symbol)
            if snapshot is not None:
                self._quote_cache[symbol] = snapshot
            self._quote_cache_at[symbol] = now

    def _mtf_alignment(self, feature: FeatureRecord, values: Mapping[str, float]) -> float:
        """Confirms the symbol's own-timeframe direction against the next
        timeframe up. Stays neutral (no penalty, no boost) when there's no
        configured higher timeframe or it hasn't warmed up yet -- an unavailable
        confirmation is not evidence of misalignment."""
        higher = self._HIGHER_TIMEFRAME.get(feature.timeframe)
        if higher is None:
            return evaluate_mtf_alignment(None)
        connection = self._connect()
        started = time.perf_counter()
        result = "success"
        try:
            higher_values = latest_technical_features(
                connection, feature.market, feature.symbol, higher
            )
        except Exception:
            result = "error"
            raise
        finally:
            connection.close()
            if self._metrics is not None:
                self._metrics.observe_database(
                    feature.market, "mtf_alignment", result, time.perf_counter() - started
                )
        if higher_values is None:
            return evaluate_mtf_alignment(None)
        current_bullish = values["ema_9"] > values["ema_21"]
        higher_bullish = higher_values["ema_9"] > higher_values["ema_21"]
        return evaluate_mtf_alignment(current_bullish == higher_bullish)

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
            index_values = (
                fetch_index_snapshot(connection, MARKET_INDEX_SYMBOL, self._REGIME_TIMEFRAME)
                if market is Market.NSE
                else None
            )
            if index_values is not None:
                breadth = classify_index_regime(index_values)
                vix_values = fetch_index_snapshot(connection, VIX_SYMBOL, self._REGIME_TIMEFRAME)
                if vix_values is not None:
                    risk_off = classify_risk_off(vix_values["close"])
                    breadth = RegimeBreadth(
                        f"{breadth.label}_{risk_off.label}", breadth.fit * risk_off.fit
                    )
            else:
                symbols = self._regime_universe_symbols(connection, market)
                adx, bullish = fetch_breadth_inputs(
                    connection, market, symbols, self._REGIME_TIMEFRAME
                )
                breadth = classify_breadth(adx, bullish)
        except Exception:
            result = "error"
            raise
        finally:
            connection.close()
            if self._metrics is not None:
                self._metrics.observe_database(
                    market, "market_regime", result, time.perf_counter() - started
                )
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
            sector_index = SECTOR_INDEX_SYMBOL.get(sector) if market is Market.NSE else None
            index_values = (
                fetch_index_snapshot(connection, sector_index, self._REGIME_TIMEFRAME)
                if sector_index is not None
                else None
            )
            if index_values is not None:
                breadth = classify_index_regime(index_values)
            else:
                symbols = [
                    symbol
                    for symbol in self._regime_universe_symbols(connection, market)
                    if sector_for(symbol) == sector
                ]
                adx, bullish = fetch_breadth_inputs(
                    connection, market, symbols, self._REGIME_TIMEFRAME
                )
                breadth = classify_breadth(adx, bullish, minimum_symbols=3)
        except Exception:
            result = "error"
            raise
        finally:
            connection.close()
            if self._metrics is not None:
                self._metrics.observe_database(
                    market, "sector_regime", result, time.perf_counter() - started
                )
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
