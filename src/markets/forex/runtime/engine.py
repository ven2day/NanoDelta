"""FOREX worker selected by the existing DeltaQuant runtime entry point."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, deque
from datetime import UTC, datetime
from typing import Any

from src.agents.prediction import PredictionAgent
from src.backtesting.strategy_eligibility import (
    EligibilityStatus,
    StrategyEligibility,
    StrategyEligibilityRegistry,
)
from src.core.candidates import SignalEngine, TradeHorizon
from src.core.candidates.signals import StrategyType
from src.core.candles import CandleStore
from src.core.strategies.config import PRODUCTION_STRATEGY_NAMES
from src.finops import get_cost_tracker
from src.markets.forex.broker.oanda import OandaMarketDataProvider
from src.markets.forex.eligibility import create_registry
from src.markets.forex.market_data import create_forex_market_data_provider
from src.markets.forex.ml import ForexModelRegistry, create_artifact_registry
from src.markets.forex.persistence import (
    bind_candle_repository,
    bind_decision_repository,
    bind_execution_repository,
    bind_feature_repository,
    bind_raw_market_repository,
    bind_trading_repository,
)
from src.markets.forex.risk import ForexRiskLimits, quote_to_home_rate
from src.markets.forex.runtime.cycle import ForexSettledCandleCycle
from src.markets.forex.runtime.paper_positions import ForexPaperPositionManager
from src.markets.forex.strategies import build_strategy_config
from src.markets.snapshots import MarketSnapshotStore
from src.observability.tracing import record_trading_cycle

logger = logging.getLogger(__name__)


def _ensure_classic_strategy_placeholders(
    registry: StrategyEligibilityRegistry,
    model_version: str,
    timeframes: tuple[str, ...],
) -> int:
    """Seed SHADOW placeholders for the always-on classic strategies.

    ``eligible_strategy_types`` runs every non-production-family strategy
    (breakout, momentum, ema_psar, ...) on every timeframe, but
    ``ensure_production_strategy_placeholders`` only seeds the production family.
    Without a registry record those live signals report ELIGIBILITY_REGISTRY_MISSING
    instead of the accurate SHADOW state. These are never executable and have no
    forex OOS validation path yet; the placeholder only makes their status honest.
    """
    now = datetime.now(UTC).isoformat()
    classic = [s.value for s in StrategyType if s.value not in PRODUCTION_STRATEGY_NAMES]
    created = 0
    for name in classic:
        for timeframe in timeframes:
            if registry.latest(name, timeframe, market="FOREX") is not None:
                continue
            registry.register(
                StrategyEligibility(
                    strategy_name=name,
                    timeframe=timeframe,
                    model_version=model_version,
                    validation_status=EligibilityStatus.SHADOW,
                    validated_at=now,
                    validation_window={"kind": "classic_strategy_placeholder", "validated": False},
                    oos_trade_count=0,
                    oos_profit_factor=None,
                    oos_max_drawdown=None,
                    oos_win_rate=None,
                    minimum_model_confidence=0.0,
                    minimum_strategy_confidence=0.0,
                    status_reason="Classic shared strategy; no forex OOS validation path yet",
                    artifact_reference=None,
                    requires_ml=False,
                    regime_performance={},
                    validation_metadata={"executable": False, "family": "classic"},
                    market="FOREX",
                )
            )
            created += 1
    return created


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


async def _annotate_open_positions(
    provider: OandaMarketDataProvider,
    positions: list[dict[str, Any]],
    home_currency: str,
) -> list[dict[str, Any]]:
    """Add live current_price + unrealized P&L (pips and home-currency) to each
    open paper position, reusing OANDA quotes. Never fabricates: if a quote or
    conversion is unavailable the position is passed through unannotated.
    """
    if not positions:
        return positions
    try:
        instruments = await provider.list_instruments()
        by_symbol = {inst.symbol: inst for inst in instruments}
        prices = await provider.get_prices([inst.symbol for inst in instruments])
    except Exception:
        return positions
    conversion: dict[tuple[str, str], float] = {}
    for symbol, quote in prices.items():
        inst = by_symbol.get(symbol)
        if inst is not None and quote.mid > 0:
            conversion[(inst.base_currency, inst.quote_currency)] = quote.mid
    annotated: list[dict[str, Any]] = []
    for position in positions:
        row = dict(position)
        symbol = str(position.get("symbol", ""))
        inst = by_symbol.get(symbol)
        quote = prices.get(symbol)
        if inst is not None and quote is not None and quote.mid > 0:
            entry = float(position.get("entry_price", 0.0) or 0.0)
            units = float(position.get("units", 0.0) or 0.0)
            signed = 1.0 if str(position.get("side", "")).upper() == "BUY" else -1.0
            current = float(quote.mid)
            row["current_price"] = current
            if inst.pip_size > 0:
                row["unrealized_pips"] = (current - entry) / inst.pip_size * signed
            try:
                rate = quote_to_home_rate(inst, home_currency, conversion)
                row["unrealized_pnl_home"] = units * (current - entry) * signed * rate
            except ValueError:
                pass
        annotated.append(row)
    return annotated


async def run_forex_market_worker(settings: Any) -> None:
    """Run OANDA Practice market data and shared-strategy paper cycles.

    There is intentionally no broker-execution provider in this function. Selecting
    OANDA LIVE changes only the data endpoint and produces a prominent warning; all
    no OANDA order route is constructed. Only local-paper fills can be persisted.
    """

    if str(settings.market).upper() != "FOREX":
        raise ValueError("Forex worker requires MARKET=FOREX")
    if settings.trading_mode != "paper" or settings.allow_live_orders:
        raise RuntimeError("Forex Phase 1 requires paper mode and ALLOW_LIVE_ORDERS=false")
    provider = create_forex_market_data_provider(settings)
    logger.warning(
        "MARKET: FOREX | PROVIDER: OANDA | OANDA ENVIRONMENT: %s | EXECUTION: DISABLED",
        str(settings.oanda_environment).upper(),
    )
    if str(settings.oanda_environment).lower() == "live":
        logger.critical("OANDA LIVE DATA ENVIRONMENT SELECTED; ORDER EXECUTION REMAINS DISABLED")
    health = await provider.validate_connectivity()
    if not health.healthy:
        await provider.close()
        raise RuntimeError(f"Forex market data failed startup checks: {health.message}")
    stream_instruments = await provider.list_instruments()
    await provider.start_price_stream([item.symbol for item in stream_instruments])
    stream_received = await provider.wait_for_stream_message(
        float(settings.oanda_stream_startup_timeout_seconds)
    )
    if not stream_received:
        logger.warning(
            "OANDA pricing stream is %s; REST/candle processing remains available",
            provider.stream_health_state().value,
        )

    candle_store = None
    trading_repository = None
    feature_repository = None
    decision_repository = None
    execution_repository = None
    model_registry = None
    if settings.market_history_store_enabled:
        raw_candle_store = CandleStore(
            settings.market_history_database_url or settings.database_url,
            enable_timescale=settings.market_history_enable_timescale,
            require_timescale=settings.market_history_require_timescale,
            schema=str(getattr(settings, "forex_db_schema", "forex")),
        )
        candle_store = bind_candle_repository(raw_candle_store)
        provider.client.set_raw_event_sink(bind_raw_market_repository(raw_candle_store.engine))
        trading_repository = bind_trading_repository(raw_candle_store.engine)
        feature_repository = bind_feature_repository(raw_candle_store.engine)
        decision_repository = bind_decision_repository(raw_candle_store.engine)
        execution_repository = bind_execution_repository(raw_candle_store.engine)
        model_registry = ForexModelRegistry(raw_candle_store.engine)
    config = build_strategy_config(settings)
    engine = SignalEngine(
        mean_reversion_stop_loss_pct=settings.mean_reversion_stop_loss_pct,
        mean_reversion_target_pct=settings.mean_reversion_target_pct,
        production_config=config,
    )
    registry = create_registry(settings)
    prediction_agent = PredictionAgent(artifact_registry=create_artifact_registry(settings))

    def _parse_timeframes(raw: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.strip() for item in str(raw).split(",") if item.strip()))

    # Two trade horizons run each cycle: the original intraday/swing pass and an
    # additive 5m scalp pass. They must stay on *disjoint* timeframe sets so their
    # candidate identities / idempotency keys can never collide in the shared
    # store (a same-symbol 15m swing and 5m scalp are distinct grains). Scalp
    # timeframes that overlap intraday are dropped for that reason.
    intraday_timeframes = _parse_timeframes(settings.forex_intraday_timeframes) or (
        "15m",
        "30m",
        "1h",
        "4h",
    )
    scalp_enabled = bool(getattr(settings, "forex_scalp_enabled", False))
    scalp_timeframes = tuple(
        tf for tf in _parse_timeframes(getattr(settings, "forex_scalp_timeframes", "5m"))
        if tf not in intraday_timeframes
    )
    seeded = _ensure_classic_strategy_placeholders(
        registry, config.version, tuple(dict.fromkeys(intraday_timeframes + scalp_timeframes))
    )
    if seeded:
        logger.info("Seeded %s classic-strategy SHADOW placeholders", seeded)

    def _build_cycle(trade_horizon: str) -> ForexSettledCandleCycle:
        return ForexSettledCandleCycle(
            provider=provider,
            signal_engine=engine,
            strategy_config=config,
            eligibility_registry=registry,
            risk_limits=ForexRiskLimits.from_settings(settings, trade_horizon=trade_horizon),
            candle_store=candle_store,
            trading_repository=trading_repository,
            feature_repository=feature_repository,
            decision_repository=decision_repository,
            execution_repository=execution_repository,
            prediction_agent=prediction_agent,
            model_registry=model_registry,
            settings=settings,
        )

    intraday_cycle = _build_cycle("SWING")
    scalp_cycle = _build_cycle("SCALP") if (scalp_enabled and scalp_timeframes) else None
    if scalp_enabled and not scalp_timeframes:
        logger.warning(
            "FOREX_SCALP_ENABLED but no scalp-only timeframes remain after removing overlap "
            "with intraday timeframes %s; scalp pass skipped",
            intraday_timeframes,
        )
    logger.warning(
        "FOREX HORIZONS | INTRADAY: %s | SCALP: %s",
        ",".join(intraday_timeframes),
        (",".join(scalp_timeframes) if scalp_cycle is not None else "DISABLED"),
    )
    if bool(getattr(settings, "forex_allow_unvalidated_paper", False)):
        logger.warning(
            "FOREX_ALLOW_UNVALIDATED_PAPER=true — PAPER_APPROVED eligibility gate is LIFTED; "
            "SHADOW strategies will create paper decisions. Deterministic risk engine, "
            "MTF-conflict and ML/LLM review still apply; no live OANDA orders are ever routed."
        )
    runtime_id = str(getattr(settings, "runtime_id", "forex"))
    snapshot_store = MarketSnapshotStore(
        str(settings.market_snapshot_root),
        writer_runtime_id=runtime_id,
    )
    position_manager = (
        ForexPaperPositionManager(provider, trading_repository, execution_repository)
        if trading_repository is not None
        else None
    )
    latest_intraday_candidates: list[dict[str, Any]] = []
    latest_scalp_candidates: list[dict[str, Any]] = []
    # Funnel metrics are held from the last cycle that actually processed a
    # settled candle, so a quiet cycle (no new bar) doesn't blank the funnel
    # while the candidate list below still shows that cycle's survivors.
    latest_intraday_metrics: dict[str, Any] = {}
    latest_scalp_metrics: dict[str, Any] = {}

    async def _run_pass(
        pass_cycle: ForexSettledCandleCycle,
        pass_timeframes: tuple[str, ...],
        pass_horizon: TradeHorizon,
        candle_count: int = 260,
    ) -> Any:
        result = await pass_cycle.run(
            timeframes=pass_timeframes,
            trade_horizon=pass_horizon,
            candle_count=candle_count,
        )
        metrics = result.metrics
        if metrics.feature_snapshots:
            record_trading_cycle(
                {
                    "market": "FOREX",
                    "provider": "OANDA",
                    "provider_environment": str(settings.oanda_environment).upper(),
                    "execution_mode": "paper",
                    "trade_horizon": pass_horizon.value,
                    "changed_timeframes": ",".join(pass_timeframes),
                    "symbols_considered": metrics.instruments_requested,
                    "symbols_scanned": metrics.instruments_scanned,
                    "technical_candidates": (
                        metrics.technical_buy_candidates + metrics.technical_sell_candidates
                    ),
                    "ml_evaluated": metrics.ml_evaluated,
                    "ml_qualified": metrics.ml_qualified,
                    "ml_abstained": metrics.ml_abstained,
                    "qwen_reviewed": metrics.qwen_reviewed,
                    "all_llm_calls": metrics.qwen_external_calls,
                    "candidate_qwen_calls": metrics.qwen_external_calls,
                    "market_context_calls": 0,
                    "qwen_cache_hits": metrics.qwen_cache_hits,
                    "qwen_external_calls": metrics.qwen_external_calls,
                    "qwen_input_tokens": metrics.qwen_input_tokens,
                    "qwen_output_tokens": metrics.qwen_output_tokens,
                    "risk_approved": metrics.risk_approved,
                    "orders_created": metrics.paper_orders,
                    "final_buy": metrics.final_buy,
                    "final_sell": metrics.final_sell,
                    "cycle_duration_ms": metrics.total_duration_ms,
                }
            )
        logger.info(
            "Forex %s settled-candle cycle: %s", pass_horizon.value, metrics.to_dict()
        )
        return result

    cycle_number = 0
    home_currency = str(settings.forex_account_home_currency)
    recent_cycle_durations: deque[float] = deque(maxlen=300)
    try:
        while True:
            cycle_number += 1
            intraday_result = await _run_pass(
                intraday_cycle, intraday_timeframes, TradeHorizon.SWING
            )
            if intraday_result.metrics.feature_snapshots:
                latest_intraday_candidates = list(intraday_result.technical_candidates)
                latest_intraday_metrics = intraday_result.metrics.to_dict()
            scalp_result = None
            if scalp_cycle is not None:
                # Deeper 5m window: enough bars for indicator warm-up on fast
                # candles and to seed the CandleStore for 5m OOS validation.
                scalp_result = await _run_pass(
                    scalp_cycle, scalp_timeframes, TradeHorizon.SCALP, candle_count=500
                )
                if scalp_result.metrics.feature_snapshots:
                    latest_scalp_candidates = list(scalp_result.technical_candidates)
                    latest_scalp_metrics = scalp_result.metrics.to_dict()
            # Intraday metrics remain the primary cycle summary; scalp is published
            # alongside it. The persisted positions/decisions below already span both
            # horizons (one shared trading_repository), so they need no merging.
            metrics = intraday_result.metrics
            latest_technical_candidates = (
                latest_intraday_candidates + latest_scalp_candidates
            )
            qwen_input = metrics.qwen_input_tokens + (
                scalp_result.metrics.qwen_input_tokens if scalp_result is not None else 0
            )
            qwen_output = metrics.qwen_output_tokens + (
                scalp_result.metrics.qwen_output_tokens if scalp_result is not None else 0
            )
            qwen_calls = metrics.qwen_external_calls + (
                scalp_result.metrics.qwen_external_calls if scalp_result is not None else 0
            )
            position_metrics = (
                (await position_manager.manage()).to_dict()
                if position_manager is not None
                else {
                    "positions_monitored": 0,
                    "positions_closed": 0,
                    "stop_exits": 0,
                    "target_exits": 0,
                    "stale_or_missing_quotes": 0,
                }
            )
            positions = (
                await asyncio.to_thread(trading_repository.load_open_positions)
                if trading_repository is not None
                else []
            )
            positions = await _annotate_open_positions(provider, positions, home_currency)
            final_decisions = (
                await asyncio.to_thread(
                    trading_repository.load_final_decisions,
                    days=7,
                    limit=250,
                )
                if trading_repository is not None
                else []
            )
            # --- Observability: precise timestamps, cycle timing, rejection funnel ---
            scan_completed_at = datetime.now(UTC).isoformat()
            candidate_candle_ts = [
                str(c.get("settled_candle_timestamp"))
                for c in latest_technical_candidates
                if c.get("settled_candle_timestamp")
            ]
            latest_candle_timestamp = max(candidate_candle_ts) if candidate_candle_ts else None
            cycle_duration_ms = float(
                (latest_intraday_metrics or metrics.to_dict()).get("total_duration_ms", 0.0) or 0.0
            ) + float((latest_scalp_metrics or {}).get("total_duration_ms", 0.0) or 0.0)
            previous_cycle_duration_ms = (
                recent_cycle_durations[-1] if recent_cycle_durations else None
            )
            recent_cycle_durations.append(cycle_duration_ms)
            durations = list(recent_cycle_durations)
            rejection_counter = Counter(
                str(c.get("rejection_reason"))
                for c in latest_technical_candidates
                if c.get("rejection_reason")
            )
            total_rejected = sum(rejection_counter.values())
            rejection_summary = [
                {
                    "reason": reason,
                    "count": count,
                    "pct": round(100.0 * count / total_rejected, 1) if total_rejected else 0.0,
                }
                for reason, count in rejection_counter.most_common()
            ]
            finops = get_cost_tracker()
            daily_llm = finops.daily_summary("FOREX")
            session_llm = finops.session_summary("FOREX")
            published = snapshot_store.publish(
                "FOREX",
                status={
                    "status": "HEALTHY",
                    "provider": "OANDA",
                    "environment": str(settings.oanda_environment).upper(),
                    "data": "LIVE",
                    "pricing_stream": provider.stream_health_state().value,
                    "execution": "PAPER",
                    "broker_orders": "OFF",
                    "runtime_id": runtime_id,
                    "cycle": cycle_number,
                    # Precise timestamps + timing (ISO 8601 UTC) so the UI never
                    # shows a bare "60s" without an anchor.
                    "scan_completed_at": scan_completed_at,
                    "pricing_snapshot_at": scan_completed_at,
                    "latest_candle_timestamp": latest_candle_timestamp,
                    "cycle_duration_ms": round(cycle_duration_ms, 1),
                    "previous_cycle_duration_ms": (
                        round(previous_cycle_duration_ms, 1)
                        if previous_cycle_duration_ms is not None
                        else None
                    ),
                    "cycle_p50_ms": round(_percentile(durations, 0.50), 1),
                    "cycle_p95_ms": round(_percentile(durations, 0.95), 1),
                    "rejection_summary": rejection_summary,
                    "rejected_total": total_rejected,
                    # Truthful risk/health context for the dashboard cockpit. The
                    # frontend derives used-margin / open-risk / currency exposure
                    # from the open-position payloads against this equity.
                    "account_equity": float(settings.forex_paper_account_equity),
                    "account_home_currency": str(settings.forex_account_home_currency),
                    "risk_per_trade": float(settings.forex_risk_per_trade),
                    "scalp_risk_per_trade": float(settings.forex_scalp_risk_per_trade),
                    "daily_loss_limit_fraction": float(settings.forex_daily_loss_limit_fraction),
                    "max_margin_utilization": float(settings.forex_max_margin_utilization),
                    "forex_kill_switch": bool(getattr(settings, "forex_kill_switch", False)),
                    "global_kill_switch": bool(getattr(settings, "global_kill_switch", False)),
                    "timescale_connected": candle_store is not None,
                    "model_registry_connected": model_registry is not None,
                    "cycle_metrics": latest_intraday_metrics or metrics.to_dict(),
                    "scalp_cycle_metrics": (
                        (latest_scalp_metrics or None) if scalp_cycle is not None else None
                    ),
                    # Live per-cycle counts (0 on a quiet cycle) kept distinct from
                    # the sticky funnel above, for anyone watching cadence.
                    "cycle_metrics_live": metrics.to_dict(),
                    "horizons": {
                        "intraday_timeframes": list(intraday_timeframes),
                        "scalp_enabled": scalp_cycle is not None,
                        "scalp_timeframes": (
                            list(scalp_timeframes) if scalp_cycle is not None else []
                        ),
                    },
                    "position_management": position_metrics,
                    "llm_finops": daily_llm,
                    "llm_session": session_llm,
                    "llm_cycle": {
                        "candidate_review_calls": qwen_calls,
                        "calls": qwen_calls,
                        "input_tokens": qwen_input,
                        "output_tokens": qwen_output,
                        "total_tokens": qwen_input + qwen_output,
                    },
                },
                # `signals` is the public final-decision contract.  Technical
                # candidates have a separate diagnostic endpoint and can never
                # masquerade as paper BUY/SELL decisions again.
                signals=final_decisions,
                positions=positions,
                technical_candidates=latest_technical_candidates,
                strategies=[
                    record.to_dict()
                    for record in registry.load_all()
                    if record.market.upper() == "FOREX"
                ],
                models=(
                    [record.to_dict() for record in model_registry.list_records(market="FOREX")]
                    if model_registry is not None
                    else []
                ),
                force=True,
            )
            if not published:
                logger.warning(
                    "Forex dashboard snapshot is degraded; trading cycle continues: %s",
                    snapshot_store.metrics("FOREX"),
                )
            await asyncio.sleep(settings.forex_cycle_wake_seconds)
    finally:
        await provider.close()
