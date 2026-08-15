"""
₹DeltaQuant Live Trading Dashboard

Enhanced with:
- Real historical indicator calculation (replaces fabricated indicators)
- Real trade execution via paper engine (replaces random P&L)
- Dynamic exit management (trailing stops, time exits, partial profits)
- Performance tracking for strategy learning
"""

import asyncio
import dataclasses
import json
import logging
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

# Force UTF-8 stdout/stderr: Windows defaults to the system codepage (cp1252) whenever
# stdout isn't a live console (redirected to a file/pipe, e.g. under nohup or `| Tee-Object`),
# which can't encode ₹ or other non-ASCII characters used in banners/log messages and
# crashes the whole process with an unhandled UnicodeEncodeError.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from rich.console import Console

from src.agents.graph import create_trading_graph, run_trading_cycle
from src.agents.pre_llm import evaluate_signal_eligibility
from src.agents.prediction import MODEL_VERSION, PredictionAgent
from src.agents.risk_compliance import check_kill_switch, compute_return_correlations
from src.agents.scalp_training_scheduler import AutomaticScalpTrainingScheduler
from src.agents.shared_context import get_shared_market_context_service
from src.backtesting.production_strategy_registration import (
    ensure_production_strategy_placeholders,
)
from src.backtesting.strategy_eligibility import (
    eligibility_registry_from_settings,
    resolve_eligibility_environment,
)
from src.backtesting.validation_scheduler import AutomaticValidationScheduler
from src.config import get_settings
from src.core.aggregation import (
    FeatureSnapshot,
    aggregate_strategy_signals,
    eligible_strategy_types,
    evaluate_registered_strategies,
)
from src.core.candidates import SignalEngine
from src.core.candles import CandleStore
from src.core.decisions import (
    DecisionStatus,
    decision_record_from_payload,
    persist_decision_records,
    stable_candidate_id,
)
from src.core.features import build_market_relative_context, persist_feature_snapshots
from src.core.indicators import (
    FEATURE_SET_VERSION,
    IndicatorResult,
    Timeframe,
    get_indicator_cache,
)
from src.core.market_data import RawEventSink
from src.core.ml import ModelGrain
from src.core.paths import RUN_ROOT
from src.core.risk import market_kill_switch_active
from src.core.utils.events import emit_candle_settled
from src.core.utils.formatting import fmt_optional, plain_english_fallback_cause
from src.core.utils.logging_config import configure_runtime_logging
from src.dashboard.stats import TradingDashboard
from src.finops import get_alert_manager, get_cost_tracker
from src.markets.nse.broker.dhan.websocket import NSE_WATCHLIST, refresh_watchlist
from src.markets.nse.execution.averaging import CappedAveragingPolicy
from src.markets.nse.execution.exit_manager import ExitManager
from src.markets.nse.execution.finalize import TradeFinalizer
from src.markets.nse.execution.journal import TradeJournal
from src.markets.nse.execution.lifecycle import PaperTradeLifecycleStore
from src.markets.nse.execution.paper_engine import LocalPaperEngine
from src.markets.nse.execution.preflight import (
    executable_market_quote_is_fresh,
    reviewed_price_is_fresh,
)
from src.markets.nse.execution.runtime_mode import RuntimeExecutionMode
from src.markets.nse.execution.service import ExecutionService, IdempotencyStore
from src.markets.nse.execution.signal_log import SignalLogger, SignalRecord
from src.markets.nse.market_data.historical_feed import HistoricalDataFeed
from src.markets.nse.market_data.history_ingestion import (
    HistoricalIngestionJob,
    HistoricalIngestionScheduler,
    parse_ingestion_timeframes,
)
from src.markets.nse.market_data.history_manager import HistoryManager
from src.markets.nse.market_data.manager import MarketDataManager
from src.markets.nse.ml import NSEModelRegistry
from src.markets.nse.persistence import (
    bind_decision_repository,
    bind_feature_repository,
    bind_raw_market_repository,
)
from src.markets.nse.risk import calculate_position_size
from src.markets.nse.risk.daily_state import DailyRiskStore
from src.markets.nse.risk.guards import DrawdownTracker, is_circuit_locked
from src.markets.nse.risk.pretrade import FinalPaperOrder, PaperRiskReservations
from src.markets.nse.runtime.state import MarketStateStore
from src.markets.nse.sessions import is_trading_window, now_ist
from src.markets.nse.strategies import build_strategy_config
from src.markets.nse.strategies.candidate_policy import CandidateAction, evaluate_long_candidate
from src.markets.nse.strategies.scalp_ranking import scalp_performance_key
from src.markets.nse.strategies.scalp_scan import run_scalp_scan_detailed
from src.markets.nse.strategies.signal_ranking import (
    rank_signals,
    select_diversified_signals,
    select_stage1_candidates,
    technical_pre_rank,
)
from src.markets.nse.universe.discovery import StockDiscovery
from src.markets.snapshots import MarketSnapshotStore
from src.memory import feedback
from src.memory.classifier import MistakeClassifier
from src.memory.database import AgentMemoryDB
from src.memory.injection import MemoryInjector
from src.memory.performance_tracker import get_performance_tracker
from src.notifications.telegram import get_notifier
from src.observability.signal_funnel import (
    FunnelAudit,
    RejectionReason,
    scalp_status_reason,
    strategy_eligibility_rows,
)
from src.observability.tracing import (
    record_candidate_execution,
    record_runtime_stage,
    record_trading_cycle,
    setup_tracing,
)
from src.profit import ProfitGoalEngine
from src.signal_discovery.live import DiscoveredSignalScorer
from src.signal_discovery.operators import build_ohlcv_panel
from src.signal_discovery.scheduler import AutomaticDiscoveryScheduler
from src.signal_discovery.store import DiscoveredSignalStore
from src.webui.schema import stats_to_dict

console = Console()
logger = logging.getLogger(__name__)

# Safety cap on the kill-switch flatten loop: each pass closes at least the FIFO-first
# position of every open symbol, so this far exceeds the passes any realistic book needs.
MAX_FLATTEN_PASSES = 20


_TIMEFRAME_BY_VALUE = {tf.value: tf for tf in Timeframe}


def parse_signal_timeframes(raw: str) -> list[Timeframe]:
    """Parse a comma-separated ``signal_timeframes`` setting into Timeframe enums.

    Unknown tokens are ignored (logged); falls back to Daily if nothing parses so the
    pipeline always has at least one timeframe to run.
    """
    timeframes = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        tf = _TIMEFRAME_BY_VALUE.get(token)
        if tf is None:
            logger.warning(f"Unknown signal timeframe '{token}' in signal_timeframes setting")
            continue
        timeframes.append(tf)
    return timeframes or [Timeframe.D1]


def calculate_real_indicators(
    history_manager: HistoryManager, symbol: str, timeframe: Timeframe = Timeframe.D1
):
    """
    Calculate REAL indicators from historical data for a given candle timeframe.

    When ``signals_exclude_forming_bar`` is set, the still-forming bar is excluded on
    every timeframe to prevent repainting. Results are memoized via the shared
    IndicatorCache (keyed by symbol + timeframe) so unchanged data is not recomputed.
    """
    indicators, _ = calculate_real_indicator_snapshot(history_manager, symbol, timeframe)
    return indicators


def calculate_real_indicator_snapshot(
    history_manager: HistoryManager, symbol: str, timeframe: Timeframe = Timeframe.D1
) -> tuple[IndicatorResult | None, Any | None]:
    """Return indicators and the exact settled frame used to calculate them.

    Keeping the frame lets prediction, scalp confirmation, discovery scoring, and
    candidate evaluation reuse one immutable cycle snapshot instead of re-entering the
    history TTL logic later in a long full-universe cycle.
    """
    df = get_real_indicator_frame(history_manager, symbol, timeframe)
    if df is None:
        return None, None

    try:
        indicators = get_indicator_cache().get_or_compute(df, symbol, timeframe=timeframe)
        return indicators, df
    except Exception as e:
        logger.warning(f"Indicator calc failed for {symbol} [{timeframe.value}]: {e}")
        return None, None


def get_real_indicator_frame(
    history_manager: HistoryManager, symbol: str, timeframe: Timeframe = Timeframe.D1
) -> Any | None:
    """Load the exact settled frame used by the signal scanner, without computing it."""
    settings = get_settings()
    if settings.signals_exclude_forming_bar:
        df = history_manager.get_settled_history(symbol, timeframe, bars=200)
    elif timeframe == Timeframe.D1:
        df = history_manager.get_history(symbol, bars=200, include_forming=True)
    else:
        df = history_manager.get_multi_timeframe_history(symbol, timeframe, bars=200)
    return df if df is not None and len(df) >= 26 else None


async def wait_for_next_cycle(
    wait_time: int,
    dashboard: TradingDashboard,
    refresh_dashboard: Callable[[], Awaitable[None]],
) -> None:
    """Maintain scheduler cadence after both active and no-change wake-ups."""

    dashboard.set_next_cycle_countdown(wait_time)
    for _ in range(max(0, wait_time)):
        await asyncio.sleep(1)
        await refresh_dashboard()


async def run_live_trading():
    """Run the permanent local-paper workflow with live or simulated market data."""

    settings = get_settings()
    if str(getattr(settings, "market", "NSE")).upper() != "NSE":
        raise RuntimeError("The NSE runtime cannot start another market domain")
    if (
        settings.trading_mode != "paper"
        or settings.execution_mode != "local_paper"
        or settings.allow_live_orders
    ):
        raise RuntimeError(
            "DeltaQuant is permanently paper-only: require TRADING_MODE=paper, "
            "EXECUTION_MODE=local_paper, and ALLOW_LIVE_ORDERS=false."
        )
    runtime_mode = RuntimeExecutionMode.parse(settings.paper_execution_mode)
    simulated_session = runtime_mode is RuntimeExecutionMode.MOCK
    force_window_active = settings.force_trading_window and simulated_session
    if settings.force_trading_window and not simulated_session:
        raise RuntimeError("FORCE_TRADING_WINDOW is only permitted in MOCK mode")
    data_namespace = runtime_mode.namespace
    run_id = f"RUN-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    # Initialize dashboard
    data_source = "simulated" if simulated_session else "dhan"
    dashboard = TradingDashboard()
    dashboard.start(
        balance=settings.paper_wallet_balance, mode=settings.trading_mode, data_source=data_source
    )
    dashboard.stats.execution_mode = settings.execution_mode
    dashboard.stats.quote_source = data_source
    dashboard.stats.candle_source = data_source
    dashboard.stats.broker_orders_enabled = bool(settings.allow_live_orders)

    # Setup tracing
    tracing_enabled = setup_tracing()
    dashboard.stats.log_activity(
        f"Langfuse: {'enabled' if tracing_enabled else 'disabled'}", "INFO"
    )

    # Create trading graph (now includes support agents)
    graph = create_trading_graph(include_support_agents=settings.enable_news_analysis)
    dashboard.stats.log_activity("Trading graph compiled (with support agents)", "SUCCESS")

    # Initialize memory & performance tracker
    memory_db = AgentMemoryDB(namespace=data_namespace)
    perf_tracker = get_performance_tracker(data_namespace)
    dashboard.stats.log_activity("Memory database + performance tracker ready", "INFO")

    # Learning feedback loop: classify closed trades into lessons and measure whether the
    # lessons that were injected actually helped. Optional (adds an LLM call per loss).
    mistake_classifier = MistakeClassifier() if settings.enable_learning else None
    memory_injector = MemoryInjector(memory_db=memory_db) if settings.enable_learning else None
    # Lesson IDs that were in the agents' context when each position was opened.
    dashboard.stats.log_activity(
        f"Learning loop: {'enabled' if settings.enable_learning else 'disabled'}", "INFO"
    )

    # Initialize paper trading engine
    paper_engine = LocalPaperEngine(
        initial_balance=settings.paper_wallet_balance,
        execution_mode=runtime_mode,
        namespace=data_namespace,
    )
    dashboard.sync_paper_account(paper_engine.get_stats())
    dashboard.stats.log_activity(
        f"Paper engine: Rs. {paper_engine.get_balance():,.0f} balance", "INFO"
    )

    # Persistent history of every signal considered (approved/rejected), independent of
    # actual fills (TradeJournal) — powers the web UI's 7-day signal history view. Live and
    # backfill records share one Postgres table (src.markets.nse.execution.signal_log), distinguished
    # by the `source` column, so read_recent() already returns both together.
    signal_logger = SignalLogger(namespace=data_namespace)

    # Durable trade journal (records every entry/exit). Persists to DATABASE_URL; falls back
    # to a non-persistent in-memory DB (logged loudly) if that is unreachable.
    journal = TradeJournal(namespace=data_namespace)
    lifecycle_store = PaperTradeLifecycleStore()
    averaging_policy = CappedAveragingPolicy(
        enabled=settings.paper_averaging_enabled,
        max_adds=settings.paper_averaging_max_adds,
        trigger_pct=settings.paper_averaging_trigger_pct,
        add_fraction=settings.paper_averaging_add_fraction,
    )
    dashboard.stats.log_activity(
        f"Trade journal: {'persistent' if journal.is_persistent else 'in-memory (NOT durable)'}",
        "INFO" if journal.is_persistent else "WARNING",
    )

    # Durable-outbox finalize step (journal close + performance record + lesson crediting)
    # for a fully-closed canonical trade — one reason-agnostic path shared by every close
    # (target/stop/trailing/time/stale/regime-change/kill-switch) and by the startup replay
    # of any trade a crash left CLOSED-but-unfinalized. See src/execution/finalize.py.
    trade_finalizer = TradeFinalizer(
        lifecycle_store=lifecycle_store,
        perf_tracker=perf_tracker,
        journal=journal,
        enable_learning=settings.enable_learning,
        mistake_classifier=mistake_classifier,
        memory_injector=memory_injector,
        memory_db=memory_db,
        on_lesson=lambda mistake: dashboard.stats.log_activity(
            f"Lesson learned: [{mistake.severity}] {mistake.category}", "INFO"
        ),
    )

    # Unified execution service: one mode-switched path for order submission with
    # idempotency (no double-submit on retry/restart) and shadow-mode safety.
    execution_service = ExecutionService.from_settings(
        engine=paper_engine,
        idempotency=IdempotencyStore(namespace=data_namespace),
    )
    effective = execution_service.effective_mode.value
    real_orders_label = "YES" if execution_service.real_orders_active else "NO"
    dashboard.stats.log_activity(
        f"Execution mode: {effective} | REAL ORDERS: {real_orders_label}"
        + (" (SHADOW — mirrors live, sends no real orders)" if effective == "shadow" else ""),
        "WARNING" if execution_service.real_orders_active else "INFO",
    )

    # Live modes only: attach the broker executor and reconcile against the broker at
    # startup (broker = source of truth). Dormant by default — effective mode is shadow
    # unless allow_live_orders=True and Dhan creds are present.
    # Initialize exit manager
    exit_manager = ExitManager(
        trailing_atr_multiplier=1.5,
        breakeven_r_threshold=1.0,
        max_hold_minutes=240,
        partial_profit_r=1.0,
        partial_exit_pct=0.5,
        state_file=RUN_ROOT / "nse" / f"exit_manager_state.{data_namespace}.json",
        intraday_square_off_enabled=settings.intraday_square_off_enabled,
        intraday_square_off_time=settings.intraday_square_off_time,
        intraday_timeframes=tuple(
            token.strip() for token in settings.intraday_timeframes.split(",") if token.strip()
        ),
    )
    reconciliation = lifecycle_store.reconcile(
        paper_engine.get_positions(),
        exit_manager,
        order_lookup=paper_engine.get_orders_for_trade,
        namespace=data_namespace,
    )
    dashboard.stats.log_activity(
        f"Paper lifecycle reconciliation: {reconciliation.summary()}",
        "INFO" if reconciliation.in_sync else "WARNING",
    )
    if reconciliation.ambiguous > 0:
        # The ledger owns making itself reconcilable; it could not here (see
        # PaperTradeLifecycleStore.reconcile docstring) — this must be surfaced loudly, not
        # silently traded through, since exit-manager/wallet state may be wrong for these
        # trade_ids until a human resolves them.
        await get_alert_manager().alert(
            "lifecycle_reconciliation_ambiguous",
            f"{reconciliation.ambiguous} trade(s) could not be reconciled at startup "
            f"({reconciliation.summary()}) — investigate before trusting exit-manager/wallet "
            "state for them.",
            level="CRITICAL",
        )

    # Durable-outbox drain: any trade the ledger already marked CLOSED but whose journal/
    # performance/lesson fan-out never completed (a crash between the two) — replay it now
    # rather than leaving it silently uncredited forever.
    replayed = trade_finalizer.replay_unfinalized(data_namespace)
    for stale in replayed:
        dashboard.stats.log_activity(
            f"Startup replay: finalized crash-interrupted close for {stale['symbol']} "
            f"(trade_id={stale['trade_id']})",
            "WARNING",
        )
    if replayed:
        dashboard.stats.log_activity(
            f"Startup replay: finalized {len(replayed)} crash-interrupted close(s).",
            "WARNING",
        )

    # Profit-target goal engine: derive a risk-bounded plan from the configured target.
    # Advisory only — it never changes position sizing or relaxes risk limits.
    goal_engine = ProfitGoalEngine()
    goal_plan = goal_engine.build_plan(paper_engine.get_balance())
    dashboard.stats.goal_enabled = goal_plan.enabled
    dashboard.stats.goal_feasible = goal_plan.feasible
    dashboard.stats.goal_target_amount = goal_plan.monthly_target_amount
    if goal_plan.enabled:
        wr = goal_plan.required_win_rate
        wr_str = "n/a" if wr == float("inf") else f"{wr:.0%}"
        dashboard.stats.log_activity(
            f"Profit goal: Rs.{goal_plan.monthly_target_amount:,.0f}/mo "
            f"(needs ~{wr_str} win rate @ {goal_plan.expected_trades_per_day}/day; "
            f"{'feasible' if goal_plan.feasible else 'NOT feasible within risk'})",
            "INFO" if goal_plan.feasible else "WARNING",
        )
        for warning in goal_plan.warnings:
            dashboard.stats.log_activity(f"Goal: {warning}", "WARNING")
    else:
        dashboard.stats.log_activity("Profit goal: disabled (no target configured)", "INFO")

    # Intraday drawdown tracker (includes unrealized P&L) — feeds the kill switch's drawdown
    # limb, which was previously dead because the loop fed a hardcoded max_drawdown of 0.
    # Seed from the engine's actual equity (it may have RESTORED a persisted wallet/positions
    # on startup), not the static configured balance — otherwise peak/current are wrong on a
    # restart and the drawdown halt mis-fires (or never fires).
    starting_equity = paper_engine.get_total_value()
    drawdown_tracker = DrawdownTracker(
        peak_equity=starting_equity,
        current_equity=starting_equity,
    )

    # Daily trade count / P&L for the risk engine's daily limits — persisted to Postgres and
    # scoped to the IST calendar day (NOT reset by a restart, unlike the tracker above, which
    # is deliberately session-scoped). A plain in-memory counter would let the daily trade cap
    # and daily loss limit reset every time the process restarts, defeating the point of a
    # *daily* cap.
    daily_risk_store = DailyRiskStore(namespace=data_namespace)

    # Signal engine
    production_strategy_config = build_strategy_config(settings)
    signal_engine = SignalEngine(
        mean_reversion_stop_loss_pct=settings.mean_reversion_stop_loss_pct,
        mean_reversion_target_pct=settings.mean_reversion_target_pct,
        production_config=production_strategy_config,
    )
    # A SEPARATE SignalEngine instance for the scalp horizon (settings.scalp_enabled) --
    # tighter stop/target sizing than swing, matching a 5-15m holding period. The swing
    # `signal_engine` above is never touched or shared; this exists purely additively.
    scalp_signal_engine = SignalEngine(
        default_stop_loss_pct=settings.scalp_stop_loss_pct,
        default_target_pct=settings.scalp_target_pct,
        mean_reversion_stop_loss_pct=settings.mean_reversion_stop_loss_pct,
        mean_reversion_target_pct=settings.mean_reversion_target_pct,
        production_config=production_strategy_config,
    )
    # Full role set the scalp assessment matrix/confirmation is built over (req 10:
    # 5m=execution, 15m=primary, 30m=directional, 1h=context, 4h=optional macro).
    scalp_confirmation_timeframes = parse_signal_timeframes(settings.scalp_confirmation_timeframes)
    # Subset eligible to actually originate/anchor a new ScalpOpportunity (typically
    # just 5m -- the execution timeframe); the rest of scalp_confirmation_timeframes
    # only contributes confirmation context, never originates a candidate on its own.
    scalp_origin_timeframes = parse_signal_timeframes(settings.scalp_signal_timeframes)
    prediction_agent = PredictionAgent()
    discovery_timeframes = parse_signal_timeframes(settings.signal_discovery_timeframes)
    discovered_signal_scorers: dict[Timeframe, DiscoveredSignalScorer] = {}
    if settings.signal_discovery_enabled:
        for timeframe in discovery_timeframes:
            discovered_signal_scorers[timeframe] = DiscoveredSignalScorer(
                DiscoveredSignalStore(
                    Path(settings.signal_discovery_output_dir) / timeframe.value,
                    ic_threshold=settings.signal_discovery_ic_threshold,
                    p_value_threshold=settings.signal_discovery_p_value_threshold,
                ),
                max_probability_tilt=settings.signal_discovery_max_probability_tilt,
                min_cross_section=settings.signal_discovery_min_cross_section,
            )
    discovery_scheduler = (
        AutomaticDiscoveryScheduler(dashboard.stats.log_activity)
        if settings.signal_discovery_enabled and settings.signal_discovery_auto_run
        else None
    )
    # Unlike discovery_scheduler above, these two are always constructed
    # (not gated on their *_auto_run setting) -- maybe_start() already checks
    # that setting internally every cycle, and a manual "Run now" trigger
    # (see /api/system/jobs/{name}/run) needs a live instance to call even
    # when automatic scheduling is off, which is the common case.
    validation_scheduler = AutomaticValidationScheduler(dashboard.stats.log_activity)
    scalp_training_scheduler = AutomaticScalpTrainingScheduler(dashboard.stats.log_activity)
    signal_timeframes = parse_signal_timeframes(settings.signal_timeframes)
    if simulated_session:
        signal_timeframes = [
            Timeframe.M5,
            Timeframe.M15,
            Timeframe.M30,
            Timeframe.H1,
            Timeframe.H4,
        ]
    dashboard.stats.log_activity(
        f"Signal timeframes: {', '.join(tf.value for tf in signal_timeframes)}", "INFO"
    )
    eligibility_registry = eligibility_registry_from_settings(
        settings,
        runtime_timeframes=[timeframe.value for timeframe in signal_timeframes],
    )
    created_strategy_placeholders = ensure_production_strategy_placeholders(
        eligibility_registry,
        production_strategy_config,
    )
    if created_strategy_placeholders:
        dashboard.stats.log_activity(
            f"Registered {len(created_strategy_placeholders)} new strategy grains as SHADOW",
            "INFO",
        )
    eligibility_environment = resolve_eligibility_environment(
        runtime_mode.value,
        requested_execution_mode=str(settings.execution_mode),
        trading_mode=str(settings.trading_mode),
    )
    dashboard.stats.strategy_eligibility_registry = strategy_eligibility_rows(eligibility_registry)
    if settings.signal_discovery_enabled:
        mode = (
            f"automatic every {settings.signal_discovery_refresh_hours:g}h"
            if discovery_scheduler is not None
            else "load-only"
        )
        dashboard.stats.log_activity(
            f"Signal discovery: {', '.join(tf.value for tf in discovery_timeframes)} ({mode})",
            "INFO",
        )
    if settings.strategy_validation_auto_run:
        dashboard.stats.log_activity(
            f"Strategy validation: automatic every "
            f"{settings.strategy_validation_refresh_hours:g}h",
            "INFO",
        )
    if settings.scalp_training_auto_run:
        dashboard.stats.log_activity(
            f"Scalp ML training: automatic every {settings.scalp_training_refresh_hours:g}h",
            "INFO",
        )

    # The configured CSV is the trading universe. Do not run a second full-universe
    # quote scan for ranking before the live Dhan feed starts: it consumes the same
    # account-wide quote budget and can make the initial live poll fail.
    dashboard.stats.log_activity("Loading configured stock universe...", "INFO")
    discovery = StockDiscovery(max_stocks=settings.max_active_stocks)
    trading_symbols = discovery.universe
    dashboard.stats.log_activity(f"Loaded {len(trading_symbols)} symbols from the universe", "INFO")

    # Resolve Dhan security IDs for the discovered universe before anything tries to
    # subscribe/quote against it — a stale/small hardcoded watchlist here is exactly
    # what silently drops non-hardcoded symbols from live Dhan data (see the unmapped-
    # symbols check below). Network fetch, so keep it off the event loop.
    if settings.market_data_source == "dhan" and settings.enable_dhan_instrument_lookup:
        loop = asyncio.get_running_loop()
        resolved = await loop.run_in_executor(None, refresh_watchlist, trading_symbols)
        dashboard.stats.log_activity(
            f"Resolved {len(resolved)}/{len(trading_symbols)} discovered symbols against "
            "DhanHQ's instrument master",
            "INFO",
        )

    # Initialize history manager and pre-fetch data
    dashboard.stats.log_activity("Pre-fetching historical data for real indicators...", "INFO")
    synthetic_history_allowed = runtime_mode is RuntimeExecutionMode.MOCK
    if settings.enable_synthetic_history and not synthetic_history_allowed:
        dashboard.stats.log_activity(
            "Synthetic history requested but blocked by the paper-only testing safety gate",
            "WARNING",
        )
    elif synthetic_history_allowed:
        dashboard.stats.log_activity(
            "TESTING ONLY: synthetic daily history enabled (Dhan quote/history calls disabled)",
            "WARNING",
        )
    candle_store = None
    raw_event_sink: RawEventSink | None = None
    feature_repository = None
    decision_repository = None
    nse_model_registry = None
    history_ingestion_scheduler = None
    if settings.market_history_store_enabled and not simulated_session:
        try:
            history_database_url = settings.market_history_database_url or settings.database_url
            candle_store = CandleStore(
                history_database_url,
                enable_timescale=settings.market_history_enable_timescale,
                require_timescale=settings.market_history_require_timescale,
                schema=str(getattr(settings, "nse_db_schema", "nse")),
            )
            nse_model_registry = NSEModelRegistry(candle_store.engine)
            raw_event_sink = bind_raw_market_repository(candle_store.engine)
            feature_repository = bind_feature_repository(candle_store.engine)
            decision_repository = bind_decision_repository(candle_store.engine)
            storage_engine = "TimescaleDB" if candle_store.timescale_enabled else "PostgreSQL"
            dashboard.stats.log_activity(
                f"Market-history store ready ({storage_engine}; local-first ML reads)",
                "SUCCESS",
            )
        except Exception as exc:
            dashboard.stats.log_activity(
                f"Market-history store unavailable; API/cache fallback remains active: {exc}",
                "WARNING",
            )
            logger.warning("Market-history store initialization failed", exc_info=True)

    market_manager = MarketDataManager(
        symbols=trading_symbols,
        execution_mode=runtime_mode,
        raw_event_sink=raw_event_sink,
    )
    history_manager = HistoryManager(
        symbols=trading_symbols,
        lookback_period="3mo",
        allow_synthetic=synthetic_history_allowed,
        # Always wired, not gated by synthetic_history_allowed (that flag only governs
        # the old all-Dhan-disabled startup-seed path above). HistoryManager itself now
        # re-checks market hours on every call and only falls back to this stream while
        # the market is genuinely closed -- see get_multi_timeframe_history's docstring.
        # Real DhanHQ history still powers the startup prefetch below and every call
        # made during real market hours.
        simulated_stream=(
            market_manager.simulated_data if runtime_mode is RuntimeExecutionMode.MOCK else None
        ),
        candle_store=candle_store,
        raw_event_sink=raw_event_sink,
    )
    market_state = MarketStateStore()
    position_quote_event = asyncio.Event()

    # Once each native timeframe has been bootstrapped, live WebSocket quotes update
    # its forming candle locally. This turns subsequent 250-symbol scans into memory/
    # CPU work instead of another full historical-API sweep every few minutes.
    def _update_intraday_history(quote: Any) -> None:
        market_state.update_quote(quote.symbol, quote)
        position_quote_event.set()
        if not quote.is_live:
            return
        history_manager.append_intraday_quote(
            symbol=quote.symbol,
            price=quote.last_price,
            cumulative_volume=quote.volume,
            timestamp=quote.timestamp,
        )

    market_manager.on_quote = _update_intraday_history
    fetch_results = history_manager.prefetch_all()
    loaded = sum(1 for v in fetch_results.values() if v)
    history_kind = "synthetic demo" if synthetic_history_allowed else "real"
    dashboard.stats.log_activity(
        f"Historical data loaded: {loaded}/{len(trading_symbols)} symbols ({history_kind})",
        "SUCCESS",
    )

    # Seed any already-open positions with an approximate last-known price from
    # settled historical candles -- real data, works regardless of market hours
    # (unlike live quotes, which MarketDataManager deliberately withholds while the
    # market is closed; see its start() docstring). Display-only, one-time, at
    # startup: this never touches exit_manager or market_manager's quote cache, so
    # stop-loss/target checks still only ever act on a genuine live quote, never
    # this approximation.
    open_position_symbols = {p.symbol for p in paper_engine.get_positions()}
    if open_position_symbols:
        approx_prices: dict[str, float] = {}
        for symbol in open_position_symbols:
            try:
                frame = history_manager.get_settled_history(symbol, Timeframe.M5, 5)
                if frame is not None and not frame.empty:
                    approx_prices[symbol] = float(frame["close"].iloc[-1])
            except Exception:
                logger.debug("Approximate price seed failed for %s", symbol, exc_info=True)
        if approx_prices:
            paper_engine.update_positions_pnl(approx_prices)
            dashboard.stats.log_activity(
                f"Seeded {len(approx_prices)}/{len(open_position_symbols)} open position(s) "
                "with an approximate last-known price (settled candle, not a live quote)",
                "INFO",
            )

    if (
        candle_store is not None
        and settings.market_history_auto_sync
        and settings.enable_dhan_historical_data
    ):
        ingestion_job = HistoricalIngestionJob(
            candle_store,
            HistoricalDataFeed(
                symbols=trading_symbols,
                raw_event_sink=raw_event_sink,
            ),
            lookback_days=settings.market_history_lookback_days,
            overlap_days=settings.market_history_overlap_days,
            progress=dashboard.stats.log_activity,
            readiness_callback=market_state.set_readiness,
        )
        history_ingestion_scheduler = HistoricalIngestionScheduler(
            ingestion_job,
            refresh_hours=settings.market_history_refresh_hours,
            allow_during_market=settings.market_history_sync_during_market,
        )
        ingestion_timeframes = parse_ingestion_timeframes(settings.market_history_timeframes)
        history_ingestion_scheduler.start(trading_symbols, ingestion_timeframes)
        dashboard.stats.log_activity(
            f"Background history sync queued: {settings.market_history_lookback_days} days, "
            f"{', '.join(timeframe.value for timeframe in ingestion_timeframes)}",
            "INFO",
        )

    console.print("\n[bold green]₹DeltaQuant Live Trading System Starting...[/]")
    # "LIVE"/"SIMULATED" here describes the PRICE FEED only (real-time NSE data vs a demo
    # feed when the market's closed) — it says nothing about money. `effective` (computed
    # above from execution_mode/allow_live_orders) is what actually gates whether any order
    # can touch a real account, so spell that out explicitly to avoid the two being confused.
    data_mode = "MOCK SIMULATED" if simulated_session else "MARKET_PAPER / DHAN"
    money_mode = "REAL ORDERS" if effective == "live" else "PAPER (no real orders)"
    console.print(
        f"[dim]Data feed: {data_mode} | Execution: {money_mode} | "
        f"Stocks: {len(trading_symbols)} | Press Ctrl+C to stop[/]\n"
    )
    if force_window_active:
        console.print(
            "[bold red]TESTING MODE: FORCE_TRADING_WINDOW=true — trading cycles will run "
            "regardless of market hours or weekday.[/]\n"
        )
        dashboard.stats.log_activity(
            "TESTING MODE: FORCE_TRADING_WINDOW=true — market-hours/weekday check bypassed",
            "WARNING",
        )
    await asyncio.sleep(1)

    # Start market data
    is_live = await market_manager.start()
    dashboard.stats.data_source = market_manager.data_source
    dashboard.stats.quote_source = market_manager.data_source
    dashboard.stats.candle_source = (
        "simulated"
        if simulated_session
        else "timescaledb+dhan_incremental"
        if candle_store is not None
        else "dhan_history"
    )
    dashboard.stats.data_lineage = {
        "execution_mode": settings.execution_mode,
        "runtime_mode": runtime_mode.value,
        "market_data_source": settings.market_data_source,
        "quote_source": dashboard.stats.quote_source,
        "candle_source": dashboard.stats.candle_source,
        "is_simulated": simulated_session,
        "simulated_data_executable": runtime_mode is RuntimeExecutionMode.MOCK,
        "broker_orders_enabled": bool(settings.allow_live_orders),
    }
    source_suffix = (
        " (MOCK simulated; refreshed each cycle)"
        if simulated_session
        else " (REST polling; refreshed each cycle)"
        if market_manager.data_source == "dhan_rest"
        else " (entries paused until a verified NSE session)"
        if market_manager.data_source == "market_closed"
        else ""
    )
    dashboard.stats.log_activity(
        f"Data source: {market_manager.data_source}{source_suffix}",
        "SUCCESS",
    )

    # The Dhan live feed only knows how to subscribe to its own hardcoded symbol table
    # (`NSE_WATCHLIST`, ~20 large-caps) — completely decoupled from StockDiscovery's much
    # wider universe. Any discovered symbol outside that table is silently dropped from the
    # subscription (subscribe() still reports success even with zero instruments), so a
    # discovery run with poor overlap looks identical to a broken feed. Surface it loudly.
    if market_manager.data_source == "dhan":
        unmapped = [s for s in trading_symbols if s not in NSE_WATCHLIST]
        if unmapped:
            dashboard.stats.log_activity(
                f"No live Dhan quotes for {len(unmapped)}/{len(trading_symbols)} discovered "
                f"symbols (not in the live feed's watchlist): {', '.join(unmapped)}",
                "WARNING",
            )
        if len(unmapped) == len(trading_symbols):
            dashboard.stats.log_activity(
                "ZERO discovered symbols are covered by the live WebSocket feed — quotes "
                "for this cycle will come from REST-polled DhanHQ quotes instead "
                "(refresh()/QuotesFeed), or restart to re-roll discovery.",
                "ERROR",
            )

    # A live WebSocket connection only opens the socket and subscribes — quotes are
    # actually delivered by reading it in `listen()`. Without pumping that loop
    # concurrently with the trading cycle below, `market_manager.quotes` never
    # populates and every cycle sees "no candidates" forever. Run it as a background
    # task; it handles its own errors/reconnect-signalling internally and never raises.
    listen_task: asyncio.Task | None = None
    if is_live:
        listen_task = asyncio.create_task(market_manager.listen())
        dashboard.stats.log_activity("WebSocket listener started", "INFO")

    # Optional live web dashboard (FastAPI+WebSocket), mirroring the CLI panels in a
    # browser. Strictly additive: the terminal dashboard behaves identically whether
    # this is on or off. Imported lazily so a plain `uv sync` (no `web` extra) never
    # hits a missing-module error for users who haven't opted in.
    hub = None
    web_server = None
    web_task: asyncio.Task | None = None
    market_snapshot_store = MarketSnapshotStore(
        str(settings.market_snapshot_root),
        writer_runtime_id=str(getattr(settings, "runtime_id", "nse")),
    )
    if settings.enable_web_ui:
        from src.api.health import health_check
        from src.markets.api_registry import MarketApiRegistry, MarketApiView
        from src.webui.candles import dataframe_to_candles
        from src.webui.server import ConnectionHub, WebUIServer

        async def _get_health_dict(full: bool) -> dict[str, Any]:
            return (await health_check(include_slow_checks=full)).to_dict()

        def _port_open(host: str, port: int) -> bool:
            import socket

            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except OSError:
                return False

        def _job_block(
            state_path: Path,
            *,
            auto_run: bool,
            refresh_hours: float,
            scheduler: Any,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {}
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            last_epoch = payload.get("last_completed_epoch")
            hours_since = (
                (datetime.now(UTC).timestamp() - last_epoch) / 3600 if last_epoch else None
            )
            state = (
                "running"
                if scheduler is not None and scheduler.is_running
                else ("never_run" if last_epoch is None else "idle")
            )
            # Different schedulers report per-sub-task results under different keys
            # (a flat "status" string for scalp training, an "intervals"/"timeframes"
            # dict of per-interval results for validation/discovery) -- check both
            # shapes generically so a real failure is visible here regardless of
            # which job produced it, not just buried in the raw state file.
            sub_results = list(payload.get("intervals", payload.get("timeframes", {})).values())
            last_run_failed = str(payload.get("status", "")).startswith("error") or any(
                str(value).startswith("error") for value in sub_results
            )
            block: dict[str, Any] = {
                "enabled": auto_run,
                "last_run_at": (
                    datetime.fromtimestamp(last_epoch, tz=UTC).isoformat() if last_epoch else None
                ),
                "hours_since_run": round(hours_since, 2) if hours_since is not None else None,
                "refresh_interval_hours": refresh_hours,
                "stale": hours_since is None or hours_since >= refresh_hours,
                "state": state,
                "last_run_failed": last_run_failed if last_epoch is not None else False,
            }
            if extra:
                block.update(extra)
            return block

        async def _get_jobs_status() -> dict[str, Any]:
            from urllib.parse import urlsplit

            db = urlsplit(settings.database_url)
            ts_url = settings.market_history_database_url or ""
            ts = urlsplit(ts_url) if ts_url else None
            local_services = [
                {"name": "backend", "required": True, "running": True, "detail": "self"},
                {
                    "name": "frontend",
                    "required": True,
                    "running": _port_open("127.0.0.1", settings.web_frontend_port),
                    "detail": f"port {settings.web_frontend_port}",
                },
                {
                    "name": "postgres",
                    "required": True,
                    "running": _port_open(db.hostname or "127.0.0.1", db.port or 5432),
                    "detail": f"port {db.port or 5432}",
                },
                {
                    "name": "timescaledb",
                    "required": False,
                    "running": bool(ts and ts.hostname)
                    and _port_open(ts.hostname or "127.0.0.1", ts.port or 5433),
                    "detail": f"port {ts.port if ts else 5433}",
                },
            ]
            validation_state = Path(settings.strategy_eligibility_registry_dir) / (
                "auto_validation_state.json"
            )
            background_jobs = {
                "signal_discovery": _job_block(
                    Path(settings.signal_discovery_output_dir) / "auto_run_state.json",
                    auto_run=settings.signal_discovery_auto_run,
                    refresh_hours=settings.signal_discovery_refresh_hours,
                    scheduler=discovery_scheduler,
                ),
                "strategy_validation": _job_block(
                    validation_state,
                    auto_run=settings.strategy_validation_auto_run,
                    refresh_hours=settings.strategy_validation_refresh_hours,
                    scheduler=validation_scheduler,
                ),
                "scalp_training": _job_block(
                    Path("data/prediction_models") / "auto_scalp_training_state.json",
                    auto_run=settings.scalp_training_auto_run,
                    refresh_hours=settings.scalp_training_refresh_hours,
                    scheduler=scalp_training_scheduler,
                ),
            }
            return {"local_services": local_services, "background_jobs": background_jobs}

        async def _run_job(name: str) -> bool:
            """Manual 'Run now' trigger: bypasses the staleness check, still
            refuses to double-start a job that's already running."""
            if name == "strategy_validation":
                return validation_scheduler.force_run()
            if name == "scalp_training":
                return scalp_training_scheduler.force_run()
            if name == "signal_discovery":
                if discovery_scheduler is None:
                    return False
                return discovery_scheduler.force_run(history_manager, trading_symbols)
            return False

        def _get_candles(
            symbol: str, timeframe: str, limit: int, preview_simulated: bool = False
        ) -> list[dict[str, Any]]:
            """Historical OHLCV for the dashboard's Charts tab. Reuses the same
            HistoryManager the trading loop itself computes indicators/signals from —
            never a second, independently-fetched price series (see the
            simulator-coherence work: one instrument state, one price process) —
            UNLESS the symbol has an open position that was itself opened on
            simulated data (Position.entry_data_source == "simulated"), in which
            case its chart must show the same simulated candles actually driving
            that position's price/exits, not real history it was never priced
            against. Same lineage-separation principle as manager.py's
            get_real_quotes()/get_simulated_quotes() for position P&L; the
            frontend badges this via the position's own entry_data_source field,
            not anything in this response shape.

            ``preview_simulated`` is a separate, explicit opt-in: lets the Charts
            tab preview the simulated pipeline is genuinely alive for any symbol
            (e.g. while no position has opened yet to prove it via the branch
            above), without ever making simulated data the default for a symbol
            nothing has actually traded on.
            """
            has_simulated_position = any(
                p.symbol == symbol and p.entry_data_source == "simulated"
                for p in paper_engine.get_positions()
            )
            if has_simulated_position or preview_simulated:
                frame = market_manager.simulated_data.get_history(symbol, timeframe, bars=limit)
                return dataframe_to_candles(frame)

            if timeframe == Timeframe.D1.value:
                frame = history_manager.get_history(symbol, bars=limit)
            else:
                try:
                    tf = Timeframe(timeframe)
                except ValueError:
                    return []
                frame = history_manager.get_multi_timeframe_history(symbol, tf, bars=limit)
            return dataframe_to_candles(frame)

        hub = ConnectionHub()
        market_api_registry = MarketApiRegistry()
        market_api_registry.register(
            MarketApiView(
                market="NSE",
                status=lambda: {
                    "status": "HEALTHY",
                    "provider": "DHAN" if not simulated_session else "SIMULATED",
                    "execution": str(settings.execution_mode).upper(),
                    "runtime_id": str(getattr(settings, "runtime_id", "nse")),
                    "data": "SIMULATED" if simulated_session else "LIVE",
                },
                signals=signal_logger.read_recent,
                positions=lambda: [position.to_dict() for position in paper_engine.get_positions()],
            )
        )
        # Serve the peer FOREX domain from its published snapshots so this single
        # dashboard aggregates every implemented market.  The Forex worker
        # (deltaquant-forex) is an independent process writing to the shared
        # snapshot root; reading it here needs no Forex/OANDA state in-process.
        # Until that worker publishes, the view reports STALE/UNKNOWN (never a
        # 404) and lights up automatically once the first snapshot lands.
        market_api_registry.register(market_snapshot_store.api_view("FOREX"))
        web_server = WebUIServer(
            hub=hub,
            get_snapshot=lambda: {"type": "state", "data": stats_to_dict(dashboard.stats)},
            get_signals=signal_logger.read_recent,
            get_health=lambda full: _get_health_dict(full),
            get_candles=_get_candles,
            host=settings.web_ui_host,
            port=settings.web_ui_port,
            cors_origins=settings.web_ui_cors_origins.split(","),
            username=settings.web_ui_username,
            password_hash=settings.web_ui_password_hash,
            session_secret=settings.web_ui_session_secret,
            session_ttl_minutes=settings.web_ui_session_ttl_minutes,
            cookie_secure=settings.web_ui_cookie_secure,
            login_max_attempts=settings.web_ui_login_max_attempts,
            login_lockout_minutes=settings.web_ui_login_lockout_minutes,
            get_universe=lambda: sorted(set(discovery.universe)),
            market_api_registry=market_api_registry,
            get_jobs_status=_get_jobs_status,
            run_job=_run_job,
        )
        web_task = asyncio.create_task(web_server.serve())
        dashboard.stats.log_activity(
            f"Web UI: http://{settings.web_ui_host}:{settings.web_ui_port}", "INFO"
        )

    # Sector-wide top gainers/losers: scans the FULL discovery universe (~70 NIFTY50+
    # midcap symbols), not just the ~15 symbols actively picked for trading. Runs on its
    # own multi-minute timer in a thread executor (the scan is blocking) so it never
    # competes with the fast trading-signal loop for the event loop.
    sector_tracker = None
    sector_task: asyncio.Task | None = None
    if settings.enable_sector_movers:
        from src.markets.nse.universe.sector_movers import SectorMoversTracker

        sector_tracker = SectorMoversTracker(
            symbols=discovery.universe,
            refresh_seconds=settings.sector_movers_refresh_seconds,
            on_status=dashboard.stats.log_activity,
            # Reuse the authoritative live quote state; never issue a second
            # full-universe Dhan quote request just for dashboard aggregation.
            fetch_quotes=market_manager.get_all_quotes,
            data_source=market_manager.data_source,
        )
        sector_task = asyncio.create_task(sector_tracker.run())
        dashboard.stats.log_activity(
            f"Sector movers scan started ({len(discovery.universe)} symbols, "
            f"every {settings.sector_movers_refresh_seconds}s)",
            "INFO",
        )

    # The old informational scalp screener independently downloaded and rescanned the
    # whole universe every 30 minutes. The settled-candle path below now supplies the
    # same dashboard opportunity state, so keep no second timer-driven candle engine.
    scalping_tracker = None
    scalping_task: asyncio.Task | None = None
    if settings.enable_scalping_screener:
        dashboard.stats.log_activity(
            "Scalping screener is settled-candle driven; duplicate 30-minute history "
            "download loop disabled",
            "INFO",
        )

    # Telegram startup notification (best-effort; no-op if unconfigured)
    notifier = get_notifier()
    if notifier.enabled:
        try:
            await notifier.send_startup_message()
            dashboard.stats.log_activity("Telegram notifications active", "INFO")
        except Exception as e:
            logger.warning("Telegram startup notification failed: %s", e)

    async def refresh_dashboard() -> None:
        """Single choke point for every dashboard state refresh.

        Updates sector movers from the background tracker and, when the web
        UI is enabled, broadcasts the current state to any connected
        WebSocket clients. Plain-text progress is printed separately, in
        real time, by TradingStats.log_activity() itself.
        """
        if sector_tracker is not None:
            dashboard.stats.sector_movers = sector_tracker.sector_movers
            dashboard.stats.sector_movers_status = sector_tracker.scan_status
            dashboard.stats.sector_movers_data_source = sector_tracker.data_source
        if scalping_tracker is not None:
            dashboard.stats.scalping_candidates = scalping_tracker.candidates
            dashboard.stats.scalping_screener_status = scalping_tracker.scan_status
            dashboard.stats.scalping_screener_data_source = scalping_tracker.data_source
        # The manager transitions between market_closed, dhan_rest, and temporary
        # unavailability without restarting. Keep the UI label on the active source.
        dashboard.stats.data_source = market_manager.data_source
        # Mirror the exact gate the cycle loop itself uses (below), not an
        # independently-computed guess — the Header badge and this must never disagree
        # about whether the system is actually willing to open new positions right now.
        dashboard.stats.market_open = force_window_active or is_trading_window(
            settings.no_trading_before, settings.no_trading_after
        )
        dashboard.stats.force_trading_window = force_window_active
        today = daily_risk_store.get_today()
        dashboard.stats.daily_entry_cap = settings.paper_daily_entry_cap
        dashboard.stats.daily_entries = int(today["trades_count"])
        # Sync durable, NSE-scoped FinOps at every broadcast so
        # context-only Qwen calls and cycles that end at WAIT do not leave the Session
        # & Cost panel at zero until a rare full order path completes.
        tracker = get_cost_tracker()
        live_cost_summary = tracker.daily_summary("NSE")
        dashboard.stats.llm_calls = int(live_cost_summary["calls"])
        dashboard.stats.llm_tokens = int(live_cost_summary["total_tokens"])
        dashboard.stats.llm_cost_usd = float(live_cost_summary["total_cost_usd"])
        dashboard.stats.llm_input_tokens = int(live_cost_summary["input_tokens"])
        dashboard.stats.llm_output_tokens = int(live_cost_summary["output_tokens"])
        dashboard.stats.llm_finops = live_cost_summary
        dashboard.stats.llm_session_metrics = tracker.session_summary("NSE")
        dashboard.stats.qwen_calls_by_agent = {
            agent: int(values.get("calls", 0))
            for agent, values in live_cost_summary["by_agent"].items()
        }
        if market_manager.data_source == "simulated":
            dashboard.stats.simulation_event_time = (
                market_manager.simulated_data.event_time.isoformat()
            )
        dashboard.sync_paper_account(paper_engine.get_stats())
        dashboard.sync_positions(paper_engine.get_positions(), exit_manager.get_managed_positions())
        current_signal = dashboard.stats.current_signal
        market_snapshot_store.publish(
            "NSE",
            status={
                "status": "HEALTHY",
                "provider": "SIMULATED" if market_manager.data_source == "simulated" else "DHAN",
                "data": market_manager.data_source.upper(),
                "execution": str(settings.execution_mode).upper(),
                "market_open": dashboard.stats.market_open,
                "runtime_id": str(getattr(settings, "runtime_id", "nse")),
                "llm_finops": live_cost_summary,
                "llm_session": dashboard.stats.llm_session_metrics,
            },
            signals=(
                [dict(current_signal)]
                if isinstance(current_signal, dict) and current_signal
                else []
            ),
            positions=[position.to_dict() for position in paper_engine.get_positions()],
            strategies=[
                record.to_dict()
                for record in eligibility_registry.load_all()
                if record.market.upper() == "NSE"
            ],
            models=(
                [record.to_dict() for record in nse_model_registry.list_records(market="NSE")]
                if nse_model_registry is not None
                else []
            ),
            dashboard_state=stats_to_dict(dashboard.stats),
        )
        if hub is not None:
            await hub.broadcast({"type": "state", "data": stats_to_dict(dashboard.stats)})

    async def _log_signals_async(records: list[SignalRecord]) -> None:
        """Write a batch of signal records without blocking the event loop.

        signal_logger.log() is a synchronous Postgres round-trip; a cycle can produce
        dozens of these (one per signal per timeframe/strategy), so writing them one at a
        time directly on the loop would stall the WebSocket broadcast and market polling
        for the sum of all those round-trips. Running the whole batch in one executor call
        keeps the loop free while they write.
        """
        if not records:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: [signal_logger.log(r) for r in records])

    def _publish_chart(
        symbol: str,
        *,
        entry: float,
        current: float,
        target: float,
        stop: float,
        status: str,
    ) -> None:
        """Publish compact multi-timeframe chart data from the canonical feed."""
        chart: dict[str, Any] = {}
        for timeframe in (
            Timeframe.M5,
            Timeframe.M15,
            Timeframe.M30,
            Timeframe.H1,
            Timeframe.H4,
        ):
            frame = history_manager.get_multi_timeframe_history(symbol, timeframe, 90)
            if frame is None or frame.empty:
                continue
            points = []
            for timestamp, row in frame.tail(90).iterrows():
                points.append(
                    {
                        "time": timestamp.isoformat(),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]),
                    }
                )
            chart[timeframe.value] = {
                "points": points,
                "entry": entry,
                "current": current,
                "target": target,
                "stop": stop,
                "status": status,
            }
        dashboard.stats.chart_symbol = symbol
        dashboard.stats.chart_timeframes = chart

    async def _execute_managed_exit(
        pos: Any,
        *,
        quantity: int,
        price: float,
        reason: str,
        explanation: str,
    ) -> bool:
        """Execute and atomically fan out one canonical local-paper exit fill."""
        if quantity <= 0:
            return False
        event_key = (
            market_manager.simulated_data.sequence
            if market_manager.data_source == "simulated"
            else datetime.now().strftime("%Y%m%d%H%M%S")
        )
        result = await execution_service.submit_async(
            symbol=pos.symbol,
            side="SELL",
            quantity=quantity,
            price=price,
            idempotency_key=(
                f"{data_namespace}:{pos.position_id}:exit:{reason}:{event_key}:{quantity}"
            ),
            trade_id=pos.position_id,
            position_id=pos.position_id,
            reason=reason,
            entry_data_source=pos.entry_data_source,
        )
        if not result.filled:
            dashboard.stats.log_activity(
                f"EXIT {result.status}: {pos.symbol} — {result.message}", "WARNING"
            )
            return False

        fully_closed = lifecycle_store.record_exit(
            pos.position_id,
            order_id=result.order_id,
            quantity=quantity,
            exit_price=result.fill_price,
            realized_pnl=result.realized_pnl,
            exit_charges=result.exit_charges,
            reason=reason,
            mae=pos.mae,
            mfe=pos.mfe,
        )
        daily_risk_store.record_exit_pnl(result.realized_pnl)
        exit_manager.apply_exit_fill(pos.position_id, quantity)
        dashboard.stats.log_activity(
            f"EXIT [{reason}]: {pos.symbol} {quantity} @ Rs.{result.fill_price:,.2f} "
            f"P&L: Rs.{result.realized_pnl:+,.2f} — {explanation}",
            "TRADE",
        )
        if not fully_closed:
            return True

        # Aggregate partial fills + this final leg into exactly one lifecycle outcome for
        # performance/learning (cumulative net P&L at the lifecycle level) — the durable
        # ledger, not this in-memory ManagedPosition, is the source of truth for that
        # aggregation. Routed through the same finalize step used by startup crash-recovery
        # replay, so a kill-switch flatten (which also calls this function) produces the
        # same complete outcome records as a normal close.
        trade_finalizer.finalize(pos.position_id, exit_price=result.fill_price, exit_reason=reason)
        return True

    position_metrics = {
        "quote_events_processed": 0,
        "positions_monitored": 0,
        "stop_exits": 0,
        "target_exits": 0,
        "trailing_exits": 0,
        "risk_exits": 0,
    }
    # Throttled durability snapshot of open positions' current price, independent of
    # trade events -- so a restart during a long quiet stretch (or while the market is
    # closed, when no fresh quotes are available at all -- see MarketDataManager.start())
    # shows the last known mark instead of resetting P&L to zero. A plain mutable dict,
    # not `nonlocal`, since only its value is mutated from the nested async function.
    _price_snapshot_state = {"last_at": 0.0}
    PRICE_SNAPSHOT_INTERVAL_SECONDS = 60.0

    async def _run_position_management_once(*, quote_triggered: bool) -> None:
        """Fast deterministic path; never runs entry discovery, ML, or Qwen."""
        managed = list(exit_manager.get_managed_positions())
        if not managed:
            return
        started = perf_counter()
        if quote_triggered:
            position_metrics["quote_events_processed"] += 1
        position_metrics["positions_monitored"] += len(managed)
        symbols = {position.symbol for position in managed}
        quotes_now = market_manager.get_all_quotes()
        market_prices = {
            symbol: quotes_now[symbol].last_price for symbol in symbols if symbol in quotes_now
        }

        # ATR is advisory to the already-deterministic trailing rule and comes from
        # the last settled feature state. The quote path never computes indicators.
        atr_values: dict[str, float] = {}
        for symbol in symbols:
            for timeframe in (Timeframe.M15, Timeframe.M5, Timeframe.H1, Timeframe.D1):
                indicator = market_state.cached_features(symbol, timeframe)
                if indicator is not None and getattr(indicator, "atr", None):
                    atr_values[symbol] = float(indicator.atr)
                    break
        current_regime = getattr(dashboard.stats, "current_regime", "") or ""
        for position, exit_rule in exit_manager.check_exits(
            market_prices, current_regime, atr_values
        ):
            exit_price = market_prices.get(position.symbol, position.entry_price)
            quantity = max(1, int(position.quantity * exit_rule.partial_pct))
            if await _execute_managed_exit(
                position,
                quantity=quantity,
                price=exit_price,
                reason=exit_rule.exit_type,
                explanation=exit_rule.reason,
            ):
                metric = {
                    "stop_loss": "stop_exits",
                    "target": "target_exits",
                    "trailing_stop": "trailing_exits",
                }.get(exit_rule.exit_type, "risk_exits")
                position_metrics[metric] += 1

        paper_engine.update_positions_pnl(market_prices)
        if market_prices:
            now = perf_counter()
            if now - _price_snapshot_state["last_at"] >= PRICE_SNAPSHOT_INTERVAL_SECONDS:
                _price_snapshot_state["last_at"] = now
                await asyncio.to_thread(paper_engine.snapshot_current_prices)
        drawdown_tracker.update(paper_engine.get_total_value())
        daily_risk_store.update_drawdown(drawdown_tracker.max_drawdown)
        daily_stats_now = daily_risk_store.get_today()
        if (
            (market_kill_switch_active(settings, "NSE") or check_kill_switch(
                {
                    "daily_stats": daily_stats_now,
                    "portfolio": {"capital": max(drawdown_tracker.peak_equity, 1.0)},
                }
            ))
            and settings.kill_switch_flatten
        ):
            for position in list(exit_manager.get_managed_positions()):
                await _execute_managed_exit(
                    position,
                    quantity=position.quantity,
                    price=market_prices.get(position.symbol, position.entry_price),
                    reason="kill_switch",
                    explanation="deterministic daily-loss/drawdown kill switch",
                )
                position_metrics["risk_exits"] += 1
        record_runtime_stage(
            "position_management",
            perf_counter() - started,
            {
                **position_metrics,
                "position_loop_duration_ms": (perf_counter() - started) * 1000.0,
            },
        )

    async def _position_management_loop() -> None:
        while True:
            quote_triggered = False
            try:
                await asyncio.wait_for(position_quote_event.wait(), timeout=1.0)
                quote_triggered = True
            except TimeoutError:
                pass
            position_quote_event.clear()
            try:
                await _run_position_management_once(quote_triggered=quote_triggered)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Fast position-management path failed safely")

    last_review_fingerprint: tuple[str, ...] | None = None

    async def _run_cycle(cycle: int) -> None:
        """
        Run a single trading cycle.

        Raises on any unexpected failure; the caller catches it so one bad
        cycle never tears down the whole loop. Early ``return`` is used for
        benign "nothing to do" outcomes (no candidates / signals / history).
        """
        nonlocal last_review_fingerprint
        cycle_started_at = perf_counter()
        cycle_started_iso = now_ist().isoformat()
        shared_market_context: dict[str, Any] | None = None
        cycle_cost_start = get_cost_tracker().daily_summary("NSE")
        cycle_trades_start = dashboard.stats.total_trades
        funnel_audit = FunnelAudit(cycle_id=cycle, trade_horizon="SWING")
        funnel_audit.set("symbols_requested", len(trading_symbols))

        def _publish_cycle_audit() -> None:
            dashboard.stats.signal_funnel = funnel_audit.to_dict()
            dashboard.stats.rejection_reasons = funnel_audit.rejection_rows()
            dashboard.stats.rejection_drilldown = funnel_audit.rejection_drilldown()
            dashboard.stats.strategy_funnel = funnel_audit.strategy_rows()

            tracker = get_cost_tracker()
            current_cost = tracker.daily_summary("NSE")
            cycle_cost = tracker.summary_since(cycle_started_iso, "NSE")
            dashboard.stats.llm_calls = int(current_cost["calls"])
            dashboard.stats.llm_tokens = int(current_cost["total_tokens"])
            dashboard.stats.llm_input_tokens = int(current_cost["input_tokens"])
            dashboard.stats.llm_output_tokens = int(current_cost["output_tokens"])
            dashboard.stats.llm_cost_usd = float(current_cost["total_cost_usd"])
            dashboard.stats.llm_finops = current_cost
            dashboard.stats.llm_session_metrics = tracker.session_summary("NSE")
            dashboard.stats.llm_cycle_metrics = cycle_cost
            dashboard.stats.qwen_calls_per_cycle = int(cycle_cost["candidate_review_calls"])
            dashboard.stats.qwen_calls_by_agent = {
                agent: int(values.get("calls", 0))
                for agent, values in current_cost["by_agent"].items()
            }
            dashboard.stats.qwen_input_tokens_by_agent = {
                agent: int(values.get("input_tokens", 0))
                for agent, values in current_cost["by_agent"].items()
            }
            dashboard.stats.qwen_output_tokens_by_agent = {
                agent: int(values.get("output_tokens", 0))
                for agent, values in current_cost["by_agent"].items()
            }

            recent_calls = [
                record
                for record in tracker.recent_records(500)
                if str(record.get("timestamp", "")) >= cycle_started_iso
                and str(record.get("market", "")) == "NSE"
            ]
            call_groups: dict[tuple[str, str], dict[str, Any]] = {}
            for record in recent_calls:
                agent = str(record.get("agent", "unknown"))
                purpose = str(record.get("purpose", agent))
                reason = str(record.get("call_reason", "UNKNOWN_LEGACY"))
                component = str(record.get("component", agent))
                key = (component, reason)
                bucket = call_groups.setdefault(
                    key,
                    {
                        "agent": agent,
                        "component": component,
                        "call_reason": reason,
                        "market": "NSE",
                        "purpose": purpose,
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "cache_hits": 0,
                        "candidate_related": reason == "CANDIDATE_REVIEW",
                        "symbols": [],
                        "cycle_id": str(cycle),
                    },
                )
                bucket["calls"] += int(record.get("call_count", 1) or 1)
                bucket["input_tokens"] += int(record.get("input_tokens", 0) or 0)
                bucket["output_tokens"] += int(record.get("output_tokens", 0) or 0)
                bucket["total_tokens"] += int(record.get("total_tokens", 0) or 0)
                bucket["cache_hits"] += int(bool(record.get("cache_hit", False)))
                symbols = [symbol for symbol in str(record.get("symbol", "")).split(",") if symbol]
                bucket["symbols"] = sorted(set(bucket["symbols"]).union(symbols))
            dashboard.stats.qwen_call_audit = list(call_groups.values())
            candidate_cache_hits = int(funnel_audit.counts.get("qwen_cache_hits", 0) or 0)
            if candidate_cache_hits:
                dashboard.stats.qwen_call_audit.append(
                    {
                        "agent": "context_review",
                        "purpose": "candidate_context_review_cache",
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "cache_hits": candidate_cache_hits,
                        "candidate_related": True,
                        "symbols": [],
                        "cycle_id": str(cycle),
                    }
                )

            active_quotes = market_manager.get_all_quotes()
            exchange_timestamp = ""
            if active_quotes:
                latest_quote = max(active_quotes.values(), key=lambda quote: quote.timestamp)
                exchange_timestamp = latest_quote.timestamp.isoformat()
            dashboard.stats.quote_source = market_manager.data_source
            dashboard.stats.data_source = market_manager.data_source
            dashboard.stats.data_lineage = {
                **dashboard.stats.data_lineage,
                "market_data_source": market_manager.data_source,
                "quote_source": market_manager.data_source,
                "candle_source": dashboard.stats.candle_source,
                "is_simulated": market_manager.data_source == "simulated",
                "exchange_timestamp": exchange_timestamp,
                "symbols_in_current_quote_batch": len(active_quotes),
                "simulated_data_executable": (
                    runtime_mode is RuntimeExecutionMode.MOCK
                    and market_manager.data_source == "simulated"
                ),
                "broker_orders_enabled": bool(settings.allow_live_orders),
            }

        _publish_cycle_audit()

        async def _ensure_shared_market_context(
            context_indicators: dict[str, Any],
            lessons: list[dict[str, Any]] | None = None,
        ) -> dict[str, Any] | None:
            nonlocal shared_market_context
            if not settings.llm_cost_optimization_enabled:
                return None
            if shared_market_context is None:
                try:
                    context = await get_shared_market_context_service().get_or_create(
                        {symbol: quote.to_dict() for symbol, quote in quotes.items()},
                        context_indicators,
                        lessons,
                        market="NSE",
                        cycle_id=str(cycle),
                    )
                except Exception as exc:
                    logger.warning("Shared market context failed safely: %s", exc)
                    dashboard.stats.agent_fallback_notice = (
                        "Market context: unavailable this cycle; deterministic scanning continues"
                    )
                    return None
                shared_market_context = context.to_dict()
                dashboard.stats.log_activity(
                    f"Shared market context: {'cache hit' if context.cache_hit else 'generated'} "
                    f"version={context.context_version[:10]} regime={context.regime}",
                    "INFO",
                )
            return shared_market_context

        dashboard.set_cycle_stage("exits", "Checking exits on open positions", cycle=cycle)
        await refresh_dashboard()

        # Advance one authoritative 5-minute event before any exit, signal, or chart
        # reads simulated state. Quotes and all aggregate timeframes stay synchronized.
        if not is_live:
            if simulated_session:
                market_manager.refresh()
            else:
                await asyncio.to_thread(market_manager.refresh)
            if market_manager.data_source == "simulated":
                history_manager.sync_simulated_stream()

        # ── Step 0: Check exits on existing positions ───────────
        # Resolve each open position's price from the pipeline it was actually opened
        # on (Position.entry_data_source), not whichever pipeline happens to be active
        # this cycle -- a real position's exits/stop/target/P&L must never be evaluated
        # against simulated (closed-market pipeline-test) prices, or vice versa, just
        # because the market opened/closed mid-session (see manager.py's
        # get_real_quotes()/get_simulated_quotes()). Non-position scan symbols (ATR
        # below, ranking elsewhere) still use the blended active-pipeline feed, which
        # is correct for them -- they're not committed to a lineage.
        quotes = market_manager.get_all_quotes()
        market_prices = {s: q.last_price for s, q in quotes.items()}
        open_positions_now = paper_engine.get_positions()
        real_position_symbols = {
            p.symbol for p in open_positions_now if p.entry_data_source != "simulated"
        }
        sim_position_symbols = {
            p.symbol for p in open_positions_now if p.entry_data_source == "simulated"
        }
        if real_position_symbols:
            real_prices = market_manager.get_real_quotes(real_position_symbols)
            for symbol in real_position_symbols:
                if symbol in real_prices:
                    market_prices[symbol] = real_prices[symbol].last_price
                else:
                    # No real quote fetched yet this session -- never substitute the
                    # blended (possibly simulated) feed; leave it out so the position
                    # simply holds at its last known price instead of going stale on
                    # some other pipeline's number, until this actually resolves.
                    market_prices.pop(symbol, None)
        if sim_position_symbols:
            sim_prices = market_manager.get_simulated_quotes(sim_position_symbols)
            for symbol in sim_position_symbols:
                if symbol in sim_prices:
                    market_prices[symbol] = sim_prices[symbol].last_price

        # The independent quote/timer path above owns stop/target/trailing/risk exits.
        # Entry discovery only keeps this price snapshot for later paper-risk checks.
        exit_signals: list[Any] = []

        for pos, exit_rule in exit_signals:
            exit_price = market_prices.get(pos.symbol, pos.entry_price)
            exit_qty = int(pos.quantity * exit_rule.partial_pct)
            await _execute_managed_exit(
                pos,
                quantity=exit_qty,
                price=exit_price,
                reason=exit_rule.exit_type,
                explanation=exit_rule.reason,
            )

        # Separate capped averaging experiment. It is disabled by default, can only add
        # to a still-valid long above its stop, consumes a daily entry slot, and is
        # bounded by both the position and aggregate exposure limits.
        if averaging_policy.enabled:
            equity = paper_engine.get_total_value()
            for pos in list(exit_manager.get_managed_positions()):
                lifecycle = lifecycle_store.get(pos.position_id)
                current = market_prices.get(pos.symbol)
                if lifecycle is None or current is None:
                    continue
                add = averaging_policy.evaluate(
                    original_quantity=int(lifecycle["original_quantity"]),
                    add_count=int(lifecycle["add_count"]),
                    weighted_entry_price=float(lifecycle["weighted_entry_price"]),
                    current_price=current,
                    stop_loss=float(lifecycle["stop_loss"]),
                    target_price=float(lifecycle["target_price"]),
                )
                if not add.should_add:
                    continue
                daily = daily_risk_store.get_today()
                expected_fill = paper_engine.cost_model.fill_price(current, "BUY")
                current_exposure = sum(
                    position.quantity * (position.current_price or position.entry_price)
                    for position in paper_engine.get_positions()
                )
                projected_total_pct = (
                    (current_exposure + add.quantity * expected_fill) / equity * 100
                    if equity > 0
                    else 100.0
                )
                projected_position_pct = (
                    (pos.quantity * pos.entry_price + add.quantity * expected_fill) / equity
                    if equity > 0
                    else 1.0
                )
                if (
                    int(daily["trades_count"]) >= settings.paper_daily_entry_cap
                    or projected_total_pct > settings.max_total_exposure_pct
                    or projected_position_pct > settings.max_position_pct
                ):
                    dashboard.stats.log_activity(
                        f"AVERAGING BLOCKED: {pos.symbol} failed cap/exposure controls",
                        "WARNING",
                    )
                    continue
                idempotency_key = (
                    f"{data_namespace}:{pos.position_id}:add:{int(lifecycle['add_count']) + 1}"
                )
                result = await execution_service.submit_async(
                    symbol=pos.symbol,
                    side="BUY",
                    quantity=add.quantity,
                    price=current,
                    idempotency_key=idempotency_key,
                    trade_id=pos.position_id,
                    position_id=pos.position_id,
                    stop_loss=float(lifecycle["stop_loss"]),
                    target_price=float(lifecycle["target_price"]),
                    strategy=pos.strategy,
                    reason="capped_average",
                    entry_data_source=pos.entry_data_source,
                )
                if result.filled:
                    lifecycle_store.record_add(
                        pos.position_id,
                        order_id=result.order_id,
                        quantity=add.quantity,
                        fill_price=result.fill_price,
                        entry_charges=result.entry_charges,
                    )
                    exit_manager.apply_add_fill(pos.position_id, add.quantity, result.fill_price)
                    daily_risk_store.record_entry()
                    dashboard.stats.log_activity(
                        f"CAPPED ADD: {pos.symbol} +{add.quantity} @ "
                        f"Rs.{result.fill_price:,.2f} — {add.reason}",
                        "TRADE",
                    )

        await refresh_dashboard()

        # ── Step 1: Refresh market data ────────────────────────
        # Pull fresh quotes for the active source every cycle. (Previously yfinance was
        # fetched once at startup and frozen; only the websocket push updated live.)
        dashboard.set_cycle_stage("market_data", "Refreshing market quotes")
        quotes = market_manager.get_all_quotes()
        dashboard.update_market_data({s: q.to_dict() for s, q in quotes.items()})
        funnel_audit.set("symbols_ready", len(quotes))
        _publish_cycle_audit()

        if not market_manager.entries_have_valid_source:
            funnel_audit.reject(
                "MARKET_DATA",
                RejectionReason.INVALID_QUOTE.value,
                amount=max(1, len(trading_symbols) - len(quotes)),
            )
            _publish_cycle_audit()
            dashboard.set_ai_review_status(
                "paused_market_data",
                f"Deep review paused: {runtime_mode.value} has no valid current quote batch",
            )
            dashboard.stats.log_activity(
                f"ENTRY PAUSE: {runtime_mode.value} has no valid current quote batch",
                "WARNING",
            )
            dashboard.set_cycle_stage("no_market_data", "No executable current quote batch")
            for _ in range(15):
                await asyncio.sleep(1)
                await refresh_dashboard()
            return

        # Append new quotes to history for rolling indicator updates
        if market_manager.data_source != "simulated":
            for symbol, quote in quotes.items():
                history_manager.append_quote(
                    symbol=symbol,
                    open_price=quote.open,
                    high=quote.high,
                    low=quote.low,
                    close=quote.last_price,
                    volume=quote.volume,
                )
                history_manager.append_intraday_quote(
                    symbol=symbol,
                    price=quote.last_price,
                    cumulative_volume=quote.volume,
                    timestamp=quote.timestamp,
                )

        # ── Data freshness gate ────────────────────────────────
        # If the feed has stalled (even the freshest quote is too old), skip NEW
        # entries this cycle — trading on stale prices is unsafe. Exits in Step 0
        # already ran on the last known price.
        max_stale = settings.max_quote_staleness_seconds
        if max_stale > 0 and quotes and market_manager.data_source != "simulated":
            freshest_age = min(q.age_seconds for q in quotes.values())
            if freshest_age > max_stale:
                funnel_audit.reject(
                    "MARKET_DATA",
                    RejectionReason.STALE_QUOTE.value,
                    amount=len(quotes),
                )
                _publish_cycle_audit()
                dashboard.set_ai_review_status(
                    "paused_market_data",
                    f"Deep review paused: freshest quote is {freshest_age:.0f}s old",
                )
                await get_alert_manager().alert(
                    "data_staleness",
                    f"Market data stale: freshest quote is {freshest_age:.0f}s old "
                    f"(limit {max_stale}s). Skipping new entries this cycle.",
                    level="WARNING",
                )
                dashboard.stats.log_activity(
                    f"Stale data ({freshest_age:.0f}s old) — skipping trading this cycle",
                    "WARNING",
                )
                dashboard.set_cycle_stage(
                    "stale_data", f"Skipping cycle — data is {freshest_age:.0f}s stale"
                )
                for _ in range(15):
                    await asyncio.sleep(1)
                    await refresh_dashboard()
                return

        # A timer wake-up only detects new settled-candle events. It must not create
        # market context, run strategies, infer ML, or call Qwen for unchanged data.
        scan_symbols = [symbol for symbol in trading_symbols if symbol in quotes]
        funnel_audit.set("symbols_ready", len(scan_symbols))
        scan_compute_timeframes = list(
            dict.fromkeys(
                signal_timeframes
                + (scalp_confirmation_timeframes if settings.scalp_enabled else [])
            )
        )
        candle_detection_started = perf_counter()
        settled_timestamps: dict[tuple[str, Timeframe], Any] = {}
        detection_semaphore = asyncio.Semaphore(settings.signal_scan_concurrency)

        async def _load_settled_timestamp(symbol: str, timeframe: Timeframe) -> None:
            async with detection_semaphore:
                timestamp = await asyncio.to_thread(
                    history_manager.latest_settled_candle_timestamp, symbol, timeframe
                )
            settled_timestamps[(symbol, timeframe)] = timestamp

        await asyncio.gather(
            *(
                _load_settled_timestamp(symbol, timeframe)
                for symbol in scan_symbols
                for timeframe in scan_compute_timeframes
            )
        )
        changed_keys, wake_metrics = market_state.plan_timestamps(settled_timestamps)
        if not changed_keys:
            funnel_audit.reject(
                "CANDLE_EVENT",
                RejectionReason.NO_SETTLED_CANDLE.value,
                amount=len(scan_symbols),
            )
            _publish_cycle_audit()
            candle_detection_elapsed = perf_counter() - candle_detection_started
            dashboard.set_ai_review_status(
                "skipped_unchanged", "No settled candle changed; ML and Qwen were not invoked"
            )
            dashboard.set_cycle_stage(
                "no_candle_change",
                f"No settled candle change across {len(scan_symbols)} symbols",
            )
            dashboard.stats.log_activity(
                f"No-change wake-up in {candle_detection_elapsed:.3f}s: "
                "0 strategy evaluations, 0 ML inference, 0 Qwen calls",
                "INFO",
            )
            await refresh_dashboard()
            wait_time = settings.trading_cycle_seconds
            await wait_for_next_cycle(wait_time, dashboard, refresh_dashboard)
            return

        settled_frames: dict[tuple[str, Timeframe], Any] = {}

        async def _load_changed_frame(symbol: str, timeframe: Timeframe) -> None:
            async with detection_semaphore:
                frame = await asyncio.to_thread(
                    get_real_indicator_frame, history_manager, symbol, timeframe
                )
            settled_frames[(symbol, timeframe)] = frame

        await asyncio.gather(
            *(_load_changed_frame(symbol, timeframe) for symbol, timeframe in changed_keys)
        )
        candle_events, _frame_metrics = market_state.plan_events(settled_frames)
        candle_event_by_key = {(event.symbol, event.timeframe): event for event in candle_events}
        await asyncio.gather(
            *(
                emit_candle_settled(
                    event.symbol,
                    event.timeframe.value,
                    event.candle_timestamp.isoformat(),
                    correlation_id=f"cycle-{cycle}",
                )
                for event in candle_events
            )
        )
        changed_frames_by_symbol: dict[str, dict[Timeframe, Any]] = {}
        for event in candle_events:
            changed_frames_by_symbol.setdefault(event.symbol, {})[event.timeframe] = event.frame
        candle_detection_elapsed = perf_counter() - candle_detection_started
        record_runtime_stage(
            "candle_change_detection",
            candle_detection_elapsed,
            {
                "symbols_considered": wake_metrics.symbols_considered,
                "timeframe_states_considered": wake_metrics.timeframe_states_considered,
                "changed_events": wake_metrics.changed_events,
                "skipped_unchanged": wake_metrics.skipped_unchanged,
                "skipped_not_ready": wake_metrics.skipped_not_ready,
            },
        )
        if not candle_events:
            dashboard.set_ai_review_status(
                "skipped_unchanged", "No settled candle changed; ML and Qwen were not invoked"
            )
            dashboard.set_cycle_stage(
                "no_candle_change",
                f"No settled candle change across {len(scan_symbols)} symbols",
            )
            dashboard.stats.log_activity(
                f"No-change wake-up in {candle_detection_elapsed:.3f}s: "
                "0 strategy evaluations, 0 ML inference, 0 Qwen calls",
                "INFO",
            )
            await refresh_dashboard()
            return

        # Publish market-wide agent context before the potentially expensive cold
        # full-universe history bootstrap. Daily frames are already persisted and
        # available here, so five representative indicator snapshots add no broker
        # history calls. The context service safely falls back and caches for ten
        # minutes; it has no execution authority.
        dashboard.set_cycle_stage("market_context", "Refreshing AI market context")
        representative_symbols = sorted(
            quotes,
            key=lambda symbol: abs(float(quotes[symbol].change_percent)),
            reverse=True,
        )[:5]
        context_indicators: dict[str, Any] = {}
        for symbol in representative_symbols:
            indicator, _frame = calculate_real_indicator_snapshot(
                history_manager, symbol, Timeframe.D1
            )
            if indicator is not None:
                context_indicators[symbol] = indicator
        try:
            cycle_context = await _ensure_shared_market_context(context_indicators)
        except Exception as exc:
            logger.warning("Shared market context failed safely: %s", exc)
            cycle_context = None
            dashboard.stats.agent_fallback_notice = (
                "Market context: unavailable this cycle; deterministic scanning continues"
            )
        if cycle_context:
            dashboard.update_shared_market_context(cycle_context)
        await refresh_dashboard()

        # ── Step 2: Find trading candidates ────────────────────
        # Run every configured strategy/timeframe across every quoted universe symbol.
        # This is local computation plus broker history; no LLM tokens are spent here.
        dashboard.set_cycle_stage("scanning", f"Scanning {len(scan_symbols)} symbols for signals")
        indicators_by_symbol: dict[str, dict[Timeframe, Any]] = {}
        scan_indicators_by_symbol: dict[str, dict[Timeframe, Any]] = {
            symbol: market_state.cached_context(symbol) for symbol in scan_symbols
        }
        history_frames_by_symbol: dict[str, dict[Timeframe, Any]] = {
            symbol: {
                timeframe: cached
                for timeframe in scan_compute_timeframes
                if (cached := market_state.cached_frame(symbol, timeframe)) is not None
            }
            for symbol in scan_symbols
        }
        changed_indicators_by_symbol: dict[str, dict[Timeframe, IndicatorResult]] = {}
        raw_signals = []
        registered_signals = []
        feature_snapshots: list[FeatureSnapshot] = []
        strategy_evaluations = 0
        scan_started_at = perf_counter()
        indicator_cache = get_indicator_cache()
        cache_hits_before = indicator_cache.hits
        cache_misses_before = indicator_cache.misses

        def _load_symbol_frames(symbol: str) -> tuple[str, dict[Timeframe, Any]]:
            return symbol, changed_frames_by_symbol.get(symbol, {})

        scan_semaphore = asyncio.Semaphore(settings.signal_scan_concurrency)

        async def _load_symbol_frames_bounded(symbol: str) -> tuple[str, dict[Timeframe, Any]]:
            async with scan_semaphore:
                # Broker history is synchronous. Bounded worker threads let independent
                # symbols overlap network latency while the shared Dhan limiter still
                # enforces the provider-wide request ceiling.
                return await asyncio.to_thread(_load_symbol_frames, symbol)

        # Process in small batches so the dashboard reports progress during a large
        # universe scan. asyncio.gather preserves input order, keeping signal IDs and
        # tie-breaking deterministic even though workers finish out of order.
        processed = 0
        event_scan_symbols = list(changed_frames_by_symbol)
        for batch_start in range(0, len(event_scan_symbols), 25):
            batch = event_scan_symbols[batch_start : batch_start + 25]
            batch_results = await asyncio.gather(
                *(_load_symbol_frames_bounded(symbol) for symbol in batch)
            )
            for symbol, frames_by_tf in batch_results:
                processed += 1
                if frames_by_tf:
                    history_frames_by_symbol.setdefault(symbol, {}).update(frames_by_tf)

                # Indicator calculations are CPU-bound Python/NumPy work. Running them
                # in the history-fetch threads adds GIL contention and was slower than
                # one ordered pass; keep only broker I/O concurrent.
                indicators_by_tf: dict[Timeframe, Any] = {}
                for tf, frame in frames_by_tf.items():
                    try:
                        indicator = indicator_cache.get_or_compute(frame, symbol, timeframe=tf)
                        indicators_by_tf[tf] = indicator
                        market_state.mark_processed(
                            candle_event_by_key[(symbol, tf)],
                            features=indicator,
                            feature_version=FEATURE_SET_VERSION,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Indicator calc failed for %s [%s]: %s", symbol, tf.value, exc
                        )

                if not indicators_by_tf:
                    continue
                scan_indicators_by_symbol.setdefault(symbol, {}).update(indicators_by_tf)
                swing_indicators_by_tf = {
                    tf: indicators_by_tf[tf] for tf in signal_timeframes if tf in indicators_by_tf
                }
                if not swing_indicators_by_tf:
                    continue
                changed_indicators_by_symbol[symbol] = swing_indicators_by_tf

            dashboard.stats.log_activity(
                f"Strategy scan: {processed}/{len(event_scan_symbols)} changed symbols, "
                f"{len(raw_signals)} raw signals",
                "INFO",
            )
            dashboard.set_cycle_stage(
                "scanning",
                f"Scanning changed symbols ({processed}/{len(event_scan_symbols)}) — "
                f"{len(raw_signals)} raw signals so far",
            )
            await refresh_dashboard()

        # Compute benchmark/cross-sectional features once per timeframe. The builder
        # admits only exactly aligned settled timestamps and labels a median fallback
        # explicitly when a configured benchmark instrument is unavailable.
        relative_context_by_timeframe: dict[Timeframe, dict[str, Any]] = {}
        for timeframe in signal_timeframes:
            timeframe_indicators = {
                symbol: contexts[timeframe]
                for symbol, contexts in scan_indicators_by_symbol.items()
                if timeframe in contexts
            }
            relative_context_by_timeframe[timeframe] = build_market_relative_context(
                timeframe_indicators,
                benchmark_symbol=production_strategy_config.benchmark_symbol,
                fallback=production_strategy_config.benchmark_fallback,
            )

        # Signal generation is a deterministic universe-ordered pass and performs no
        # history loading or indicator calculation.
        event_regime = (
            str(cycle_context.get("regime", "unknown"))
            if isinstance(cycle_context, dict)
            else "unknown"
        )
        for symbol in event_scan_symbols:
            swing_indicators_by_tf = changed_indicators_by_symbol.get(symbol, {})
            if not swing_indicators_by_tf:
                continue
            symbol_registered_signals = []
            for timeframe, ind in swing_indicators_by_tf.items():
                event = candle_event_by_key[(symbol, timeframe)]
                snapshot = FeatureSnapshot.create(
                    ind,
                    settled_candle_timestamp=event.candle_timestamp,
                    feature_version=FEATURE_SET_VERSION,
                    market_relative=relative_context_by_timeframe.get(timeframe, {}).get(symbol),
                )
                feature_snapshots.append(snapshot)
                strategy_evaluations += len(eligible_strategy_types(signal_engine, timeframe))
                symbol_registered_signals.extend(
                    evaluate_registered_strategies(
                        signal_engine,
                        snapshot,
                        eligibility_registry,
                        trade_horizon="SWING",
                        regime=event_regime,
                        execution_mode=runtime_mode.value,
                        eligibility_environment=eligibility_environment,
                    )
                )
            symbol_signals = [item.signal for item in symbol_registered_signals]
            if symbol_signals:
                indicators_by_symbol[symbol] = swing_indicators_by_tf
                raw_signals.extend(symbol_signals)
                registered_signals.extend(symbol_registered_signals)

        await asyncio.to_thread(
            persist_feature_snapshots,
            feature_repository,
            feature_snapshots,
        )

        scan_elapsed = perf_counter() - scan_started_at
        funnel_audit.set("symbols_scanned", len(event_scan_symbols))
        funnel_audit.set("strategy_evaluations", strategy_evaluations)
        funnel_audit.set("raw_signals", len(raw_signals))
        for registered in registered_signals:
            signal = registered.signal
            funnel_audit.strategy_increment(
                signal.strategy.value,
                signal.timeframe.value,
                signal.trade_horizon.value,
                "raw",
            )
        _publish_cycle_audit()
        record_runtime_stage(
            "indicator_signal_scan",
            scan_elapsed,
            {
                "symbols_considered": len(scan_symbols),
                "symbols_scanned": len(event_scan_symbols),
                "features_updated": wake_metrics.changed_events,
                "raw_signals": len(raw_signals),
            },
        )
        dashboard.stats.log_activity(
            f"Full-universe strategy scan finished in {scan_elapsed:.1f}s "
            f"(indicator cache: {indicator_cache.hits - cache_hits_before} hits, "
            f"{indicator_cache.misses - cache_misses_before} misses)",
            "SUCCESS",
        )

        # ── Step 2b: Scalp horizon scan (VISIBILITY ONLY) ──────
        # Gated entirely by settings.scalp_enabled (default False). Produces
        # dashboard.stats.scalp_opportunities/scalp_funnel for observability; the
        # actual scan/rank logic lives in src/markets/nse/strategies/scalp_scan.py (kept out of this
        # closure so it's unit-testable without the whole live-session object
        # graph). Never invokes the agent graph and never calls execution_service --
        # structurally cannot place a trade. Strategy eligibility and execution are wired
        # in later stages (see CLAUDE.md "Scalp horizon"). The swing scan above
        # (signal_engine, raw_signals, indicators_by_symbol) is completely
        # untouched by this block.
        if settings.scalp_enabled:
            # A scalp entry is reconsidered only when one of its origin candles
            # settled. Confirmation timeframes are read from MarketStateStore.
            scalp_scan_symbols = [
                symbol
                for symbol in scan_symbols
                if any(
                    timeframe in scalp_origin_timeframes
                    for timeframe in changed_frames_by_symbol.get(symbol, {})
                )
            ]
            dashboard.set_cycle_stage(
                "scalp_scanning", f"Scalp scan: {len(scalp_scan_symbols)} symbols"
            )
            # Last cycle's LLM-classified regime (this cycle's own classification
            # doesn't exist yet -- the agent graph hasn't run). Used only for the
            # assessment matrix's divergence note. The active eligibility registry
            # later evaluates the runtime regime and applies its versioned policy.
            scalp_cycle_regime = getattr(dashboard.stats, "current_regime", "") or "unknown"

            async def _fetch_scalp_indicators(symbol: str, tf: Timeframe) -> IndicatorResult | None:
                cached = scan_indicators_by_symbol.get(symbol, {}).get(tf)
                if cached is not None:
                    return cached
                # A scalp-only timeframe may not be present in SIGNAL_TIMEFRAMES.
                # Fetch only that missing cell; common cells reuse the swing snapshot.
                ind, frame = await asyncio.to_thread(
                    calculate_real_indicator_snapshot, history_manager, symbol, tf
                )
                if ind is not None:
                    scan_indicators_by_symbol.setdefault(symbol, {})[tf] = ind
                if frame is not None:
                    history_frames_by_symbol.setdefault(symbol, {})[tf] = frame
                return ind

            async def _fetch_scalp_5m_frame(symbol: str) -> Any:
                cached = history_frames_by_symbol.get(symbol, {}).get(Timeframe.M5)
                if cached is not None:
                    return cached.iloc[-240:] if len(cached) > 240 else cached
                return await asyncio.to_thread(
                    history_manager.get_multi_timeframe_history, symbol, Timeframe.M5, 240
                )

            scalp_ml_semaphore = asyncio.Semaphore(settings.signal_scan_concurrency)

            async def _fetch_scalp_prediction(
                symbol: str, tf: Timeframe, strategy_name: str
            ) -> Any:
                if not settings.ml_predictions_enabled:
                    return None
                try:
                    frame = await asyncio.to_thread(
                        history_manager.get_settled_history,
                        symbol,
                        tf,
                        settings.ml_history_bars,
                    )
                    if frame is None or frame.empty:
                        return None
                    active_model = (
                        nse_model_registry.latest_active(
                            ModelGrain(
                                market="NSE",
                                strategy_name=strategy_name,
                                timeframe=tf.value,
                                strategy_version=production_strategy_config.version,
                            )
                        )
                        if nse_model_registry is not None
                        else None
                    )
                    if nse_model_registry is not None and active_model is None:
                        return None
                    ml_frame = frame.rename(columns=lambda column: str(column).title())
                    async with scalp_ml_semaphore:
                        return await asyncio.to_thread(
                            prediction_agent.predict_cached,
                            ml_frame,
                            symbol,
                            timeframe=tf.value,
                            trade_horizon="SCALP",
                            strategy_version=production_strategy_config.version,
                            model_version=(
                                active_model.model_version if active_model is not None else MODEL_VERSION
                            ),
                        )
                except Exception:
                    logger.warning(
                        "Stored-history scalp ML failed for %s [%s]",
                        symbol,
                        tf.value,
                        exc_info=True,
                    )
                    return None

            scalp_scan_result = await run_scalp_scan_detailed(
                scalp_scan_symbols,
                settings=settings,
                scalp_signal_engine=scalp_signal_engine,
                scalp_confirmation_timeframes=scalp_confirmation_timeframes,
                scalp_origin_timeframes=scalp_origin_timeframes,
                scalp_cycle_regime=scalp_cycle_regime,
                performance_tracker=perf_tracker,
                fetch_indicators=_fetch_scalp_indicators,
                fetch_5m_frame=_fetch_scalp_5m_frame,
                fetch_prediction=_fetch_scalp_prediction,
            )
            scalp_opportunities = scalp_scan_result.opportunities
            scalp_funnel = scalp_scan_result.funnel
            dashboard.stats.scalp_symbol_statuses = [
                status.to_dict() for status in scalp_scan_result.symbol_statuses
            ]
            dashboard.stats.scalp_scan_completed_at = now_ist().isoformat()
            if settings.llm_cost_optimization_enabled:
                before_registry_opportunities = list(scalp_opportunities)
                scalp_opportunities = []
                scalp_registry_rejections: Counter[str] = Counter()
                confidence_reduced = 0
                for opportunity in before_registry_opportunities:
                    payload = opportunity.to_signal_dict()
                    decision = evaluate_signal_eligibility(
                        payload,
                        regime=scalp_cycle_regime,
                        runtime_mode=runtime_mode.value,
                        registry=eligibility_registry,
                    )
                    if decision.pipeline_allowed:
                        opportunity = dataclasses.replace(
                            opportunity, score=decision.adjusted_confidence
                        )
                        if decision.confidence_multiplier < 1.0:
                            confidence_reduced += 1
                        scalp_opportunities.append(opportunity)
                        continue
                    scalp_registry_rejections[decision.reason_code] += 1
                before_registry = len(before_registry_opportunities)
                scalp_funnel["registry_eligible"] = len(scalp_opportunities)
                scalp_funnel["registry_pass"] = len(scalp_opportunities)
                scalp_funnel["confidence_reduced"] = confidence_reduced
                if before_registry > len(scalp_opportunities):
                    dashboard.stats.log_activity(
                        "Strategy eligibility blocked "
                        f"{before_registry - len(scalp_opportunities)} SCALP candidate(s) "
                        "before Qwen",
                        "INFO",
                    )
            dashboard.stats.scalp_funnel = scalp_funnel
            scalp_rejections: Counter[str] = Counter()
            for status in scalp_scan_result.symbol_statuses:
                reason = scalp_status_reason(status)
                if reason:
                    scalp_rejections[reason] += 1
            if settings.llm_cost_optimization_enabled:
                scalp_rejections.update(scalp_registry_rejections)
            scalp_rejection_total = sum(scalp_rejections.values())
            dashboard.stats.scalp_rejection_reasons = [
                {
                    "stage": "SCALP",
                    "reason": reason,
                    "count": count,
                    "percentage": (
                        100.0 * count / scalp_rejection_total if scalp_rejection_total else 0.0
                    ),
                }
                for reason, count in scalp_rejections.most_common()
            ]
            dashboard.stats.scalp_opportunities = [o.to_dict() for o in scalp_opportunities]
            scalp_buy_count = sum(
                1 for status in scalp_scan_result.symbol_statuses if status.decision == "BUY"
            )
            scalp_no_data_count = sum(
                1 for status in scalp_scan_result.symbol_statuses if status.decision == "NO_DATA"
            )
            dashboard.stats.log_activity(
                f"Full scalp universe: {len(scalp_scan_result.symbol_statuses)} symbols -> "
                f"{scalp_buy_count} BUY, "
                f"{len(scalp_scan_result.symbol_statuses) - scalp_buy_count - scalp_no_data_count} "
                f"NO BUY, {scalp_no_data_count} NO DATA. Funnel: "
                f"{scalp_funnel['raw_triggers']} raw triggers -> "
                f"{scalp_funnel['consolidated']} consolidated -> "
                f"{scalp_funnel['mtf_candidates']} mtf-confirmed -> "
                f"{scalp_funnel['entry_quality_passed']} entry-quality-passed -> "
                f"{scalp_funnel['ml_candidates']} ML candidates -> "
                f"{scalp_funnel['ml_qualified']} ML-qualified -> "
                f"{scalp_funnel['ml_scored']} ML-scored cells -> "
                f"{scalp_funnel['regime_compatible']} regime-compatible "
                f"({len(scalp_opportunities)} ranked; no Top-K admission cap)",
                "SUCCESS",
            )
            await refresh_dashboard()

        # ── Step 2c: Scalp horizon agent review ────────────────
        # First point a scalp trade could actually be approved -- still NOT
        # executed here (see Stage 12/CLAUDE.md "Scalp horizon"). Reuses the exact
        # same LangGraph pipeline as swing (strategy_selection_node/
        # signal_validation_node/risk_compliance_node), invoked a SECOND time this
        # cycle with trade_horizon="SCALP" threaded through state so risk limits use
        # SCALP semantics. Eligibility identity remains strategy + timeframe + model
        # version. Shares the same FinOps daily budget pool as swing, by design
        # (see FinOps spend gate below) -- a busy scalp cycle can exhaust today's
        # LLM budget faster than swing alone; that's a product tradeoff, not a
        # missing gate.
        if settings.scalp_enabled and scalp_opportunities:
            scalp_cost_tracker = get_cost_tracker()
            if scalp_cost_tracker.is_over_hard_budget("SCALP"):
                dashboard.stats.log_activity(
                    "FinOps HARD budget reached — skipping scalp agent review this cycle",
                    "WARNING",
                )
            else:
                scalp_signal_payloads = [o.to_signal_dict() for o in scalp_opportunities]
                for payload in scalp_signal_payloads:
                    payload["execution_mode"] = runtime_mode.value
                dashboard.stats.scalp_funnel["sent_to_ai"] = len(scalp_signal_payloads)

                scalp_symbols = {o.symbol for o in scalp_opportunities}
                scalp_market_data = {s: quotes[s].to_dict() for s in scalp_symbols if s in quotes}
                scalp_workflow_id = f"LIVE-{datetime.now().strftime('%Y%m%d%H%M%S')}-{cycle}-scalp"
                scalp_portfolio = {
                    "capital": paper_engine.get_balance(),
                    "positions": [p.to_dict() for p in paper_engine.get_positions()],
                }
                scalp_daily_stats = daily_risk_store.get_today()
                scalp_daily_stats["simulation_mode"] = simulated_session

                dashboard.set_cycle_stage(
                    "scalp_ai_review",
                    f"Scalp AI review: {len(scalp_signal_payloads)} candidate(s)",
                )
                await refresh_dashboard()

                cycle_context = await _ensure_shared_market_context(indicators_by_symbol)

                scalp_final_state = await run_trading_cycle(
                    graph=graph,
                    market_data=scalp_market_data,
                    indicators={},
                    signals=scalp_signal_payloads,
                    memory_lessons=[],
                    portfolio=scalp_portfolio,
                    daily_stats=scalp_daily_stats,
                    thread_id=scalp_workflow_id,
                    precomputed_predictions={
                        f"{symbol}|{timeframe}": prediction.to_dict()
                        for (symbol, timeframe), prediction in scalp_scan_result.predictions.items()
                    },
                    data_namespace=data_namespace,
                    trade_horizon="SCALP",
                    shared_market_context=cycle_context,
                    execution_mode=runtime_mode.value,
                )

                scalp_validated = scalp_final_state.get("validated_signals", [])
                scalp_approved = scalp_final_state.get("approved_trades", [])
                scalp_risk_rejected = scalp_final_state.get("risk_rejected", [])
                dashboard.stats.scalp_funnel["ai_approved"] = len(scalp_approved)
                scalp_metrics = scalp_final_state.get("llm_optimization_metrics", {})
                for metric in (
                    "pre_llm_risk_pass",
                    "qwen_candidates",
                    "qwen_calls",
                    "qwen_cache_hits",
                    "qwen_cache_misses",
                    "qwen_approved",
                ):
                    if metric in scalp_metrics:
                        dashboard.stats.scalp_funnel[metric] = int(
                            scalp_metrics.get(metric, 0) or 0
                        )
                dashboard.stats.scalp_funnel["registry_eligible"] = int(
                    scalp_metrics.get("pre_llm_risk_pass", 0) or 0
                )
                dashboard.stats.scalp_funnel["registry_pass"] = dashboard.stats.scalp_funnel[
                    "registry_eligible"
                ]

                scalp_fallbacks = [
                    error
                    for error in scalp_final_state.get("errors", [])
                    if "fallback" in str(error).lower()
                ]
                if scalp_fallbacks:
                    scalp_approved = []
                for rejected_scalp_signal in scalp_risk_rejected:
                    failures = rejected_scalp_signal.get("risk_result", {}).get("failures", [])
                    reason = failures[0].get("message") if failures else "risk rules"
                    dashboard.stats.log_activity(
                        f"SCALP RISK BLOCKED: {rejected_scalp_signal.get('symbol')} "
                        f"[{rejected_scalp_signal.get('timeframe', 'N/A')}] — {reason}",
                        "WARNING",
                    )
                dashboard.stats.log_activity(
                    f"Scalp AI review: {len(scalp_validated)} validated, "
                    f"{len(scalp_approved)} approved, {len(scalp_risk_rejected)} risk-rejected",
                    "SUCCESS" if scalp_approved else "INFO",
                )
                await refresh_dashboard()

                # ── Step 2d: Execute AI-approved scalp trades ──────
                # Reuses the exact same execution_service/lifecycle_store/
                # exit_manager/journal call sites swing's Step 7 uses below,
                # unmodified -- only the idempotency key, position sizing
                # (scalp_risk_per_trade/scalp_max_position_pct), and a
                # `trade_horizon` tag differ. Portfolio-wide aggregate limits
                # (max_total_exposure_pct, max_concurrent_positions, the daily
                # entry cap) are the SAME shared settings swing uses -- only the
                # per-trade risk sizing is horizon-specific; scalp trades count
                # against the same daily cap and exposure budget as swing, they
                # don't get a separate, additive risk allowance.
                if runtime_mode is RuntimeExecutionMode.MARKET_PAPER and not is_live:
                    await asyncio.to_thread(market_manager.refresh)
                    quotes = market_manager.get_all_quotes()
                    market_prices.update({s: q.last_price for s, q in quotes.items()})
                scalp_reservations = PaperRiskReservations()
                for trade in scalp_approved:
                    symbol = trade.get("symbol", "N/A")
                    side = trade.get("signal_type", "BUY").upper()

                    if settings.long_only and side == "SELL":
                        dashboard.stats.log_activity(
                            f"LONG-ONLY: ignoring new scalp SELL signal for {symbol}", "INFO"
                        )
                        continue

                    if settings.circuit_guard_enabled:
                        q = quotes.get(symbol)
                        if q is not None and is_circuit_locked(
                            q.change_percent, settings.default_circuit_band_pct
                        ):
                            dashboard.stats.log_activity(
                                f"CIRCUIT LOCKED: skipping scalp {symbol} "
                                f"({q.change_percent:+.1f}%)",
                                "WARNING",
                            )
                            continue

                    quoted_price = float(market_prices.get(symbol, 0.0) or 0.0)
                    reviewed_entry = float(trade.get("entry_price", 0.0) or 0.0)
                    stop_loss = float(trade.get("stop_loss", 0.0) or 0.0)
                    target_price = float(trade.get("target_price", 0.0) or 0.0)
                    strategy = str(trade.get("strategy", "unknown"))
                    if runtime_mode is RuntimeExecutionMode.MARKET_PAPER:
                        quote_freshness = executable_market_quote_is_fresh(
                            quotes.get(symbol),
                            present_in_latest_batch=market_manager.is_current_batch_symbol(symbol),
                            max_age_seconds=settings.max_quote_staleness_seconds,
                            session_date=now_ist().date(),
                        )
                        if not quote_freshness.approved:
                            dashboard.stats.log_activity(
                                f"WAIT: scalp {symbol} quote rejected — {quote_freshness.reason}",
                                "WARNING",
                            )
                            continue
                    if reviewed_entry <= 0 or quoted_price <= 0:
                        dashboard.stats.log_activity(
                            f"WAIT: scalp {symbol} has no exact reviewed/current price",
                            "WARNING",
                        )
                        continue
                    if not reviewed_price_is_fresh(reviewed_entry, quoted_price):
                        dashboard.stats.log_activity(
                            f"WAIT: scalp {symbol} moved after review; exact approval is stale",
                            "WARNING",
                        )
                        continue
                    requested_price = reviewed_entry
                    expected_fill = paper_engine.cost_model.fill_price(requested_price, side)

                    if expected_fill > 0 and 0 < stop_loss < expected_fill < target_price:
                        win_rate = perf_tracker.get_strategy_performance(
                            scalp_performance_key(strategy), scalp_cycle_regime
                        ).win_rate
                        sizing = calculate_position_size(
                            capital=paper_engine.get_total_value(),
                            entry_price=expected_fill,
                            stop_loss=stop_loss,
                            target_price=target_price,
                            risk_per_trade=settings.scalp_risk_per_trade,
                            max_position_pct=settings.scalp_max_position_pct,
                            win_rate=win_rate,
                        )
                        quantity = sizing.shares
                    else:
                        quantity = 0

                    scalp_final_order = FinalPaperOrder(
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        entry_price=expected_fill,
                        stop_loss=stop_loss,
                        target_price=target_price,
                        strategy=strategy,
                    )
                    current_daily = daily_risk_store.get_today()
                    scalp_final_risk = scalp_reservations.evaluate(
                        scalp_final_order,
                        positions=paper_engine.get_positions(),
                        equity=paper_engine.get_total_value(),
                        entries_today=int(current_daily["trades_count"]),
                        daily_entry_cap=min(
                            settings.max_daily_trades, settings.paper_daily_entry_cap
                        ),
                        max_positions=settings.max_concurrent_positions,
                        max_position_pct=settings.scalp_max_position_pct,
                        max_total_exposure_pct=settings.max_total_exposure_pct,
                        risk_per_trade=settings.scalp_risk_per_trade,
                        max_total_risk=settings.max_total_risk,
                        realized_daily_pnl=float(current_daily["profit_loss"]),
                        daily_loss_limit=settings.daily_loss_limit,
                        daily_loss_buffer=settings.open_risk_daily_loss_buffer,
                    )
                    if not scalp_final_risk.approved:
                        dashboard.stats.log_activity(
                            f"FINAL RISK BLOCKED: scalp {symbol} — {scalp_final_risk.reasons[0]}",
                            "WARNING",
                        )
                        continue
                    scalp_reservations.reserve(scalp_final_order)

                    scalp_signal_id = str(trade.get("signal_id", ""))
                    # "scalp" in the key (plus the already-scalp-suffixed
                    # scalp_workflow_id) guarantees this can never collide with a
                    # same-cycle, same-symbol SWING entry's idempotency key --
                    # the two horizons are always distinguishable, never silently
                    # deduplicated against each other.
                    scalp_idempotency_key = (
                        f"{data_namespace}:{scalp_workflow_id}:{scalp_signal_id}:"
                        f"{symbol}:scalp:entry"
                    )
                    scalp_trade_id = lifecycle_store.new_trade_id()
                    lifecycle_store.create_intent(
                        namespace=data_namespace,
                        run_id=run_id,
                        workflow_id=scalp_workflow_id,
                        idempotency_key=scalp_idempotency_key,
                        signal_id=scalp_signal_id,
                        symbol=symbol,
                        side=side,
                        strategy=strategy,
                        timeframe=str(trade.get("timeframe", "")),
                        quantity=quantity,
                        entry_price=expected_fill,
                        stop_loss=stop_loss,
                        target_price=target_price,
                        rationale=list(trade.get("reasons", [])),
                        context={
                            "trade_horizon": "SCALP",
                            "regime": scalp_final_state.get("regime", "unknown"),
                            "regime_confidence": scalp_final_state.get("regime_confidence", 0),
                            "validation": trade.get("validation", {}),
                            "risk_result": trade.get("risk_result", {}),
                        },
                        active_lessons=[],
                        trade_id=scalp_trade_id,
                    )

                    result = await execution_service.submit_async(
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        price=requested_price,
                        idempotency_key=scalp_idempotency_key,
                        trade_id=scalp_trade_id,
                        position_id=scalp_trade_id,
                        stop_loss=stop_loss,
                        target_price=target_price,
                        strategy=strategy,
                        reason="entry",
                        entry_data_source=runtime_mode.expected_quote_source,
                    )
                    scalp_reservations.release(scalp_final_order)
                    scalp_validation = trade.get("validation", {})
                    record_candidate_execution(
                        {
                            "symbol": symbol,
                            "strategy": strategy,
                            "trade_horizon": "SCALP",
                            "timeframe": trade.get("timeframe"),
                            "model_version": trade.get("model_version", ""),
                            "validation_status": trade.get("eligibility_status", ""),
                            "current_regime": trade.get("current_regime", ""),
                            "regime_policy": trade.get("regime_policy", ""),
                            "original_confidence": trade.get("original_confidence"),
                            "regime_adjusted_confidence": trade.get("regime_adjusted_confidence"),
                            "ml_confidence": trade.get("ml_confidence"),
                            "registry_decision": trade.get("registry_decision", ""),
                            "risk_decision": trade.get("risk_decision", "APPROVED"),
                            "final_outcome": result.status,
                            "rejection_reason": trade.get("rejection_reason", ""),
                            "candidate_fingerprint": trade.get("candidate_fingerprint"),
                            "cache_hit": scalp_validation.get("cache_hit", False),
                            "model": scalp_validation.get("model_used", ""),
                            "context_version": (cycle_context or {}).get("context_version", ""),
                            "decision": scalp_validation.get("decision", "approve"),
                            "execution_result": result.status,
                        }
                    )

                    if result.is_duplicate:
                        dashboard.stats.log_activity(
                            f"DUPLICATE suppressed: scalp {side} {symbol}", "INFO"
                        )
                        continue

                    if result.filled:
                        dashboard.stats.scalp_funnel["execution_accepted"] += 1
                        dashboard.stats.log_activity(
                            f"SCALP PAPER FILL: {side} {quantity} {symbol} @ "
                            f"Rs.{result.fill_price:,.2f} [{trade.get('timeframe', 'N/A')}]",
                            "TRADE",
                        )
                        dashboard.stats.current_balance = paper_engine.get_balance()
                        lifecycle_store.mark_open(
                            scalp_trade_id,
                            order_id=result.order_id,
                            position_id=scalp_trade_id,
                            quantity=quantity,
                            fill_price=result.fill_price,
                            entry_charges=result.entry_charges,
                        )
                        daily_risk_store.record_entry()

                        exit_manager.register_position(
                            position_id=scalp_trade_id,
                            symbol=symbol,
                            side=side,
                            quantity=quantity,
                            entry_price=result.fill_price,
                            stop_loss=stop_loss,
                            target_price=target_price,
                            strategy=strategy,
                            regime=scalp_final_state.get("regime", "unknown"),
                            timeframe=str(trade.get("timeframe", "")),
                        )
                        try:
                            scalp_trade_record = {
                                **trade,
                                "quantity": quantity,
                                "entry_price": result.fill_price,
                                "stop_loss": stop_loss,
                                "target_price": target_price,
                                "candidate_decision": {},
                            }
                            journal.record_trade(
                                scalp_trade_record,
                                scalp_workflow_id,
                                scalp_final_state,
                                trade_id=scalp_trade_id,
                                run_id=run_id,
                            )
                        except Exception as e:
                            logger.warning("Journal record_trade failed (scalp): %s", e)
                        _publish_chart(
                            symbol,
                            entry=result.fill_price,
                            current=result.fill_price,
                            target=target_price,
                            stop=stop_loss,
                            status="OPEN",
                        )
                    else:
                        lifecycle_store.mark_rejected(
                            scalp_trade_id, result.message or result.status
                        )
                        dashboard.stats.log_activity(
                            f"SCALP ORDER {result.status}: {symbol} — {result.message}",
                            "WARNING",
                        )

                await refresh_dashboard()

        if discovery_scheduler is not None and discovery_scheduler.maybe_start(
            history_manager, trading_symbols
        ):
            dashboard.stats.log_activity(
                "Automatic signal-discovery research queued in the background",
                "INFO",
            )
        if validation_scheduler is not None and validation_scheduler.maybe_start():
            dashboard.stats.log_activity(
                "Automatic strategy validation queued in the background", "INFO"
            )
        if scalp_training_scheduler is not None and scalp_training_scheduler.maybe_start():
            dashboard.stats.log_activity(
                "Automatic scalp ML training queued in the background", "INFO"
            )

        if not raw_signals:
            _publish_cycle_audit()
            cycle_llm = get_cost_tracker().summary_since(cycle_started_iso, "NSE")
            record_trading_cycle(
                {
                    "market": "NSE",
                    "provider": "DHAN",
                    "cycle_id": cycle,
                    "execution_mode": runtime_mode.value,
                    "trade_horizon": "SCALP+SWING" if settings.scalp_enabled else "SWING",
                    "changed_timeframes": sorted(
                        {event.timeframe.value for event in candle_events}
                    ),
                    "symbols_considered": len(scan_symbols),
                    "symbols_scanned": len(event_scan_symbols),
                    "technical_candidates": 0,
                    "ml_evaluated": 0,
                    "ml_cache_hits": 0,
                    "ml_cache_misses": 0,
                    "qwen_reviewed": 0,
                    "qwen_cache_hits": 0,
                    "all_llm_calls": cycle_llm["calls"],
                    "candidate_qwen_calls": cycle_llm["candidate_review_calls"],
                    "market_context_calls": cycle_llm["context_support_calls"],
                    "signals_validated": 0,
                    "risk_approved": 0,
                    "orders_created": 0,
                    "signal_funnel": funnel_audit.to_dict(),
                    "top_rejection_reasons": funnel_audit.rejection_rows()[:10],
                    "cycle_duration_ms": (perf_counter() - cycle_started_at) * 1000.0,
                }
            )
            dashboard.set_ai_review_status(
                "skipped_precheck",
                "Deep review skipped: the technical scan produced no strategy signals",
            )
            dashboard.stats.log_activity(
                f"Full-universe scan: {len(scan_symbols)} symbols, no strategy signals",
                "INFO",
            )
            dashboard.set_cycle_stage("no_signals", "No strategy signals this cycle")
            for _ in range(15):
                await asyncio.sleep(1)
                await refresh_dashboard()
            return

        dashboard.set_cycle_stage(
            "ranking", f"Stage 1 ranking {len(raw_signals)} raw signals (technical evidence)"
        )

        # One candidate per symbol/timeframe/candle retains both directional support
        # and opposition. Ineligible strategies stay visible as research evidence but
        # cannot become executable. Long-only filtering happens after aggregation so
        # a SELL hypothesis remains visible as conflict instead of disappearing.
        aggregated_candidates = aggregate_strategy_signals(registered_signals)
        registry_candidates = [item for item in aggregated_candidates if item.pipeline_eligible]
        registry_blocked_candidates = [
            item for item in aggregated_candidates if not item.pipeline_eligible
        ]
        shadow_candidates = [item for item in registry_candidates if item.shadow_only]
        long_only_candidates = [
            item
            for item in registry_candidates
            if settings.long_only and item.direction.value != "BUY"
        ]
        funnel_audit.set("technical_setups", len(aggregated_candidates))
        funnel_audit.set("registry_eligible", len(registry_candidates))
        funnel_audit.set("registry_blocked", len(registry_blocked_candidates))
        funnel_audit.set("registry_shadow_only", len(shadow_candidates))
        funnel_audit.set(
            "confidence_reduced",
            sum(
                any(
                    evidence.eligibility_decision.confidence_multiplier < 1.0
                    for evidence in (*candidate.supporting_signals, *candidate.opposing_signals)
                )
                for candidate in aggregated_candidates
            ),
        )
        for candidate in aggregated_candidates:
            representative = candidate.representative_signal
            funnel_audit.strategy_increment(
                representative.strategy.value,
                representative.timeframe.value,
                representative.trade_horizon.value,
                "technical_setups",
            )
        duplicate_evidence = max(0, len(raw_signals) - len(aggregated_candidates))
        if duplicate_evidence:
            funnel_audit.reject(
                "CONSOLIDATION",
                RejectionReason.CONSOLIDATED_DUPLICATE.value,
                amount=duplicate_evidence,
            )
        for registered in registered_signals:
            signal = registered.signal
            metric = "registry_eligible" if registered.pipeline_eligible else "registry_blocked"
            funnel_audit.strategy_increment(
                signal.strategy.value,
                signal.timeframe.value,
                signal.trade_horizon.value,
                metric,
            )
            if registered.shadow_only:
                funnel_audit.strategy_increment(
                    signal.strategy.value,
                    signal.timeframe.value,
                    signal.trade_horizon.value,
                    "shadow_only",
                )
        for candidate in registry_blocked_candidates:
            evidence = (*candidate.supporting_signals, *candidate.opposing_signals)
            primary = evidence[0] if evidence else None
            reason = (
                primary.admission_reason
                if primary is not None and primary.admission_reason
                else RejectionReason.ELIGIBILITY_REGISTRY_MISSING.value
            )
            if primary is not None and primary.validation_status == "UNVALIDATED":
                funnel_audit.increment("registry_unvalidated")
            if primary is not None and primary.eligibility_decision.regime_policy.value == "BLOCK":
                funnel_audit.increment("regime_blocked")
            representative = candidate.representative_signal
            funnel_audit.reject(
                "STRATEGY_ELIGIBILITY",
                reason,
                strategy=representative.strategy.value,
                timeframe=representative.timeframe.value,
                trade_horizon=representative.trade_horizon.value,
                symbol=representative.symbol,
            )
        for candidate in long_only_candidates:
            representative = candidate.representative_signal
            funnel_audit.increment("final_reject")
            funnel_audit.reject(
                "DETERMINISTIC_QUALIFICATION",
                RejectionReason.LONG_ONLY.value,
                strategy=representative.strategy.value,
                timeframe=representative.timeframe.value,
                trade_horizon=representative.trade_horizon.value,
                symbol=representative.symbol,
            )
        _publish_cycle_audit()
        signal_logger.log_many(
            [
                SignalRecord.from_signal(
                    item.to_dict(),
                    "shadow_only",
                    reason=(
                        f"Strategy eligibility {item.validation_status}; research evidence "
                        "only, excluded from executable order creation"
                    ),
                    source="shadow",
                )
                for item in registered_signals
                if item.shadow_only
            ]
        )
        pipeline_candidates = [
            item
            for item in aggregated_candidates
            if item.pipeline_eligible and (not settings.long_only or item.direction.value == "BUY")
        ]
        signals_for_stage1 = [item.apply_lineage() for item in pipeline_candidates]
        dashboard.stats.log_activity(
            f"Multi-strategy aggregation: {len(raw_signals)} raw signals -> "
            f"{len(aggregated_candidates)} candidates; "
            f"{len(signals_for_stage1)} registry-eligible, "
            f"{len(shadow_candidates)} shadow-only",
            "INFO",
        )

        stage1_started_at = perf_counter()
        stage1_ranked = technical_pre_rank(signals_for_stage1, perf_tracker)
        stage1_candidates = select_stage1_candidates(
            stage1_ranked,
            max_candidates=settings.ranking_stage1_top_k,
            max_per_sector=settings.universe_max_per_sector,
        )
        stage1_elapsed = perf_counter() - stage1_started_at
        record_runtime_stage(
            "stage1_ranking",
            stage1_elapsed,
            {
                "input_signals": len(signals_for_stage1),
                "selected_candidates": len(stage1_candidates),
            },
        )
        dashboard.stats.log_activity(
            f"Stage 1: {len(signals_for_stage1)} opportunities -> "
            f"{len(stage1_candidates)} unique technical setups "
            "(not yet ML candidates; no Top-K admission cap)",
            "INFO",
        )
        # Deterministic qualification precedes ML.  A strategy trigger is only a
        # technical setup; it becomes an ML candidate after 5m confirmation, volume,
        # higher-timeframe alignment, level geometry, and history sufficiency pass.
        candidate_histories_by_symbol: dict[str, dict[Timeframe, Any]] = {}
        pre_ml_decisions = {}
        ml_candidates = []
        pre_ml_started_at = perf_counter()
        for item in stage1_candidates:
            signal = item.signal
            symbol = signal.symbol
            histories = candidate_histories_by_symbol.get(symbol)
            if histories is None:
                histories = {}
                missing_timeframes = []
                for timeframe in (
                    Timeframe.M5,
                    Timeframe.M15,
                    Timeframe.M30,
                    Timeframe.H1,
                    Timeframe.H4,
                ):
                    cached = history_frames_by_symbol.get(symbol, {}).get(timeframe)
                    if cached is None:
                        missing_timeframes.append(timeframe)
                    else:
                        histories[timeframe] = cached.iloc[-240:] if len(cached) > 240 else cached
                if missing_timeframes:

                    def _fetch_missing_histories() -> dict[Timeframe, Any]:
                        return {
                            timeframe: history_manager.get_multi_timeframe_history(
                                symbol, timeframe, 240
                            )
                            for timeframe in missing_timeframes
                        }

                    histories.update(await asyncio.to_thread(_fetch_missing_histories))
                candidate_histories_by_symbol[symbol] = histories

            decision = evaluate_long_candidate(
                signal.to_dict(),
                histories,
                None,
                target_pct=settings.paper_target_pct,
                min_ml_confidence=settings.paper_min_ml_confidence,
                min_volume_ratio=settings.paper_min_volume_ratio,
                required_higher_timeframes=settings.paper_required_higher_timeframes,
                require_ml=False,
            )
            pre_ml_decisions[signal.signal_id] = decision
            if decision.action == CandidateAction.BUY:
                ml_candidates.append(item)

        pre_ml_elapsed = perf_counter() - pre_ml_started_at
        funnel_audit.set("deterministic_qualified", len(ml_candidates))
        funnel_audit.set(
            "deterministic_rejected",
            max(0, len(registry_candidates) - len(ml_candidates)),
        )
        funnel_audit.set("ml_candidates", len(ml_candidates))
        for item in stage1_candidates:
            signal = item.signal
            decision = pre_ml_decisions[signal.signal_id]
            if decision.action == CandidateAction.BUY:
                funnel_audit.strategy_increment(
                    signal.strategy.value,
                    signal.timeframe.value,
                    signal.trade_horizon.value,
                    "qualified",
                )
                continue
            if decision.action == CandidateAction.WAIT:
                funnel_audit.increment("final_wait")
            else:
                funnel_audit.increment("final_reject")
            reasons = decision.reason_codes or (RejectionReason.LOW_ENTRY_QUALITY.value,)
            funnel_audit.reject(
                "DETERMINISTIC_QUALIFICATION",
                reasons[0],
                strategy=signal.strategy.value,
                timeframe=signal.timeframe.value,
                trade_horizon=signal.trade_horizon.value,
                symbol=signal.symbol,
            )
        _publish_cycle_audit()
        record_runtime_stage(
            "deterministic_qualification",
            pre_ml_elapsed,
            {
                "technical_setups": len(stage1_candidates),
                "qualified_technical_candidates": len(ml_candidates),
            },
        )
        dashboard.stats.log_activity(
            f"Deterministic qualification: {len(stage1_candidates)} technical setups -> "
            f"{len(ml_candidates)} qualified technical candidates eligible for an ML overlay",
            "INFO",
        )
        stage1_signals = [item.signal for item in ml_candidates]

        # Stage 2 performs inference only for deterministically qualified unique pairs.
        # The cache key includes settled candle and exact artifact semantics.
        predictions = {}
        prediction_semaphore = asyncio.Semaphore(settings.signal_scan_concurrency)
        prediction_metrics_before = prediction_agent.cache_metrics()
        ml_started_at = perf_counter()

        async def _predict_stage2(item: Any) -> tuple[tuple[str, str], Any] | None:
            symbol = item.signal.symbol
            timeframe = item.signal.timeframe
            cycle_snapshot = history_frames_by_symbol.get(symbol, {}).get(timeframe)
            frame = await asyncio.to_thread(
                history_manager.get_settled_history,
                symbol,
                timeframe,
                settings.ml_history_bars,
            )
            if frame is None:
                frame = cycle_snapshot
            if frame is None or frame.empty:
                return None
            active_model = (
                nse_model_registry.latest_active(
                    ModelGrain(
                        market="NSE",
                        strategy_name=item.signal.strategy.value,
                        timeframe=timeframe.value,
                        strategy_version=item.signal.strategy_version,
                    )
                )
                if nse_model_registry is not None
                else None
            )
            if nse_model_registry is not None and active_model is None:
                return None
            if len(frame) > settings.ml_history_bars:
                frame = frame.iloc[-settings.ml_history_bars :]
            ml_frame = frame.rename(columns=lambda column: str(column).title())
            async with prediction_semaphore:
                prediction = await asyncio.to_thread(
                    prediction_agent.predict_cached,
                    ml_frame,
                    symbol,
                    timeframe=timeframe.value,
                    trade_horizon="SWING",
                    strategy_version=item.signal.strategy_version,
                    model_version=(
                        active_model.model_version if active_model is not None else MODEL_VERSION
                    ),
                )
            return (symbol, timeframe.value), prediction

        if settings.ml_predictions_enabled:
            prediction_results = await asyncio.gather(
                *(_predict_stage2(item) for item in ml_candidates)
            )
            for prediction_result in prediction_results:
                if prediction_result is not None:
                    key, prediction = prediction_result
                    predictions[key] = prediction
            for item in ml_candidates:
                prediction = predictions.get((item.signal.symbol, item.signal.timeframe.value))
                if prediction is None:
                    item.signal.ml_status = "NOT_AVAILABLE"
                    continue
                item.signal.ml_status = "ABSTAIN" if prediction.abstained else "VALIDATED"
                item.signal.ml_probability = (
                    None if prediction.abstained else float(prediction.confidence)
                )
                item.signal.ml_model_version = str(prediction.model_version)
                item.signal.ml_feature_version = str(prediction.feature_version)
        else:
            # Master switch off: no sklearn inference calls at all. `predictions` stays
            # empty, so rank_signals and prediction_node fall back to their existing
            # no-ML-available path -- the same one already exercised on every abstention.
            for item in ml_candidates:
                item.signal.ml_status = "DISABLED"
        prediction_metrics_after = prediction_agent.cache_metrics()
        ml_evaluated_count = sum(
            1 for prediction in predictions.values() if not prediction.abstained
        )
        ml_abstention_count = sum(
            1 for prediction in predictions.values() if prediction.abstained
        ) + max(0, len(ml_candidates) - len(predictions))
        funnel_audit.set(
            "ml_inference",
            prediction_metrics_after.inference_count - prediction_metrics_before.inference_count,
        )
        funnel_audit.set("ml_abstain", ml_abstention_count)
        funnel_audit.set("ml_pass", ml_evaluated_count)
        for item in ml_candidates:
            signal = item.signal
            prediction = predictions.get((signal.symbol, signal.timeframe.value))
            if prediction is None:
                reason = (
                    RejectionReason.ML_DISABLED.value
                    if not settings.ml_predictions_enabled
                    else RejectionReason.ML_ARTIFACT_MISSING.value
                )
                metric = "ml_abstained"
            elif prediction.abstained:
                reason = RejectionReason.ML_ABSTAIN.value
                metric = "ml_abstained"
            else:
                reason = ""
                metric = "ml_evaluated"
            funnel_audit.strategy_increment(
                signal.strategy.value,
                signal.timeframe.value,
                signal.trade_horizon.value,
                metric,
            )
            if reason:
                funnel_audit.reject(
                    "ML_OVERLAY",
                    reason,
                    strategy=signal.strategy.value,
                    timeframe=signal.timeframe.value,
                    trade_horizon=signal.trade_horizon.value,
                    symbol=signal.symbol,
                )
        _publish_cycle_audit()
        ml_elapsed = perf_counter() - ml_started_at
        record_runtime_stage(
            "stage2_ml",
            ml_elapsed,
            {
                "candidates": len(ml_candidates),
                "ml_evaluated": ml_evaluated_count,
                "ml_abstentions": ml_abstention_count,
                "cache_hits": prediction_metrics_after.hits - prediction_metrics_before.hits,
                "cache_misses": prediction_metrics_after.misses - prediction_metrics_before.misses,
            },
        )
        dashboard.stats.log_activity(
            f"Stage 2 ML overlay: {ml_evaluated_count} exact-artifact inferences, "
            f"{ml_abstention_count} abstentions; "
            f"cache hits +{prediction_metrics_after.hits - prediction_metrics_before.hits}, "
            f"misses +{prediction_metrics_after.misses - prediction_metrics_before.misses}, "
            f"inferences +{prediction_metrics_after.inference_count - prediction_metrics_before.inference_count}, "
            "live training +0, live walk-forward +0",
            "INFO",
        )

        discovered_tilts: dict[tuple[str, str], float] = {}
        for discovery_timeframe, scorer in discovered_signal_scorers.items():
            if not scorer.store.load_accepted():
                continue
            discovery_histories = {}
            for symbol in event_scan_symbols:
                if discovery_timeframe not in changed_frames_by_symbol.get(symbol, {}):
                    continue
                frame = history_frames_by_symbol.get(symbol, {}).get(discovery_timeframe)
                frame_is_cycle_snapshot = frame is not None
                if frame is None:
                    frame = await asyncio.to_thread(
                        history_manager.get_multi_timeframe_history,
                        symbol,
                        discovery_timeframe,
                        500,
                    )
                if frame is None or frame.empty:
                    continue
                if len(frame) > 500:
                    frame = frame.iloc[-500:]
                if (
                    not frame_is_cycle_snapshot
                    and settings.signals_exclude_forming_bar
                    and len(frame) > 1
                ):
                    frame = frame.iloc[:-1]
                discovery_histories[symbol] = frame
            if not discovery_histories:
                continue
            timeframe_tilts = await asyncio.to_thread(
                scorer.score_panel,
                build_ohlcv_panel(discovery_histories),
            )
            discovered_tilts.update(
                {
                    (symbol, discovery_timeframe.value): tilt
                    for symbol, tilt in timeframe_tilts.items()
                }
            )
            if timeframe_tilts:
                dashboard.stats.log_activity(
                    f"Discovered {discovery_timeframe.value} alpha tilted "
                    f"{len(timeframe_tilts)} symbols",
                    "INFO",
                )

        # Shared with the graph's prediction_node (support_agents) so it doesn't
        # re-fetch history and re-run the ensemble for symbols already scored here.
        precomputed_predictions_map = {
            f"{symbol}|{timeframe}": pred.to_dict()
            for (symbol, timeframe), pred in predictions.items()
        }

        # Stage 2 is a real local ML agent even when every candidate later resolves
        # to WAIT. Publish a bounded unique-symbol sample immediately instead of
        # waiting for prediction_node in a graph invocation that may never occur.
        visible_predictions = []
        visible_prediction_symbols: set[str] = set()
        for item in ml_candidates:
            key = (item.signal.symbol, item.signal.timeframe.value)
            prediction = predictions.get(key)
            if prediction is None or item.signal.symbol in visible_prediction_symbols:
                continue
            visible_prediction_symbols.add(item.signal.symbol)
            visible_predictions.append(prediction.to_dict())
            if len(visible_predictions) >= 5:
                break
        dashboard.stats.prediction_signals = visible_predictions

        # This service is content-versioned and cached (ten minutes by default).
        # Generate/reuse market news, mood, and regime even when no trade candidate
        # passes the local BUY gate. It cannot validate or create an order.
        cycle_context = await _ensure_shared_market_context(indicators_by_symbol)
        if cycle_context:
            dashboard.update_shared_market_context(cycle_context)
        await refresh_dashboard()

        ranked = rank_signals(
            stage1_signals,
            predictions,
            perf_tracker,
            discovered_signal_tilts=discovered_tilts,
        )

        # Ranking orders every deterministically qualified candidate.  Portfolio
        # capacity is applied only after validation; it must not pre-truncate otherwise
        # valid opportunities before ML abstention/context assessment is known.
        # Histories were loaded once during deterministic prequalification and are
        # reused here for the post-ML decision.
        candidate_pool = list(ranked)

        candidate_decisions = dict(pre_ml_decisions)
        qualifying_ranked = []
        candidate_evaluation_started_at = perf_counter()
        for item in candidate_pool:
            signal = item.signal
            symbol = signal.symbol

            # This should always hit the prequalification cache.  The defensive
            # fallback remains fail-safe for callers that inject a ranked item.
            histories = candidate_histories_by_symbol.get(symbol)
            if histories is None:
                histories = {}
                missing_timeframes = []
                for timeframe in (
                    Timeframe.M5,
                    Timeframe.M15,
                    Timeframe.M30,
                    Timeframe.H1,
                    Timeframe.H4,
                ):
                    cached = history_frames_by_symbol.get(symbol, {}).get(timeframe)
                    if cached is None:
                        missing_timeframes.append(timeframe)
                    else:
                        histories[timeframe] = cached.iloc[-240:] if len(cached) > 240 else cached

                if missing_timeframes:

                    def _fetch_missing_histories() -> dict[Timeframe, Any]:
                        return {
                            timeframe: history_manager.get_multi_timeframe_history(
                                symbol, timeframe, 240
                            )
                            for timeframe in missing_timeframes
                        }

                    histories.update(await asyncio.to_thread(_fetch_missing_histories))
                candidate_histories_by_symbol[symbol] = histories
            eligibility = eligibility_registry.get(
                signal.strategy.value,
                signal.timeframe.value,
                signal.model_version or signal.strategy_version,
            )
            strategy_ml_minimum = (
                eligibility.minimum_model_confidence if eligibility is not None else 0.0
            )
            decision = evaluate_long_candidate(
                signal.to_dict(),
                histories,
                predictions.get((signal.symbol, signal.timeframe.value)),
                target_pct=settings.paper_target_pct,
                min_ml_confidence=max(
                    settings.paper_min_ml_confidence,
                    strategy_ml_minimum,
                ),
                min_volume_ratio=settings.paper_min_volume_ratio,
                required_higher_timeframes=settings.paper_required_higher_timeframes,
                require_ml=signal.ml_required,
            )
            candidate_decisions[signal.signal_id] = decision
            # The framework target is the exact target subsequently reviewed, risked,
            # submitted, managed, journaled, and displayed.
            signal.target_price = decision.target_price
            signal.risk_reward_ratio = (
                (decision.target_price - decision.entry_price)
                / (decision.entry_price - decision.stop_loss)
                if decision.entry_price > decision.stop_loss > 0
                else 0.0
            )
            if decision.action == CandidateAction.BUY:
                qualifying_ranked.append(item)

        candidate_evaluation_elapsed = perf_counter() - candidate_evaluation_started_at
        record_runtime_stage(
            "candidate_evaluation",
            candidate_evaluation_elapsed,
            {"evaluated": len(candidate_pool), "qualified": len(qualifying_ranked)},
        )
        logger.info(
            "cycle_metrics cycle=%d symbols=%d raw_signals=%d technical_setups=%d "
            "ml_candidates=%d ml=%d "
            "scan_seconds=%.3f stage1_seconds=%.3f ml_seconds=%.3f candidate_seconds=%.3f",
            cycle,
            len(scan_symbols),
            len(raw_signals),
            len(stage1_candidates),
            len(ml_candidates),
            ml_evaluated_count,
            scan_elapsed,
            stage1_elapsed,
            ml_elapsed,
            candidate_evaluation_elapsed,
        )

        dashboard.stats.candidate_decisions = [
            candidate_decisions[item.signal.signal_id].to_dict() for item in candidate_pool[:25]
        ]
        survivors = select_diversified_signals(
            qualifying_ranked,
            max_symbols=settings.max_active_stocks,
            max_per_sector=settings.universe_max_per_sector,
            max_signals_per_symbol=settings.max_signals_per_symbol,
        )
        survivor_symbols = list(dict.fromkeys(item.signal.symbol for item in survivors))
        # Review limits determine ordering/batching, never admission. Qwen's material
        # fingerprint cache prevents unchanged candidates from being sent repeatedly.
        review_symbols = survivor_symbols
        reviewed_symbol_set = set(review_symbols)
        reviewed_ranked = [item for item in survivors if item.signal.symbol in reviewed_symbol_set]
        if settings.llm_cost_optimization_enabled:
            before_registry_review = len(reviewed_ranked)
            reviewed_ranked = [
                item for item in reviewed_ranked if item.signal.registry_qwen_allowed
            ]
            if before_registry_review > len(reviewed_ranked):
                dashboard.stats.log_activity(
                    "Strategy eligibility kept "
                    f"{before_registry_review - len(reviewed_ranked)} SWING candidate(s) "
                    "in shadow without a Qwen call",
                    "INFO",
                )
        signals = [item.signal for item in reviewed_ranked]

        # Falls back within candidate_pool (every item here has a computed decision),
        # never the raw `ranked` list -- candidate_decisions only covers candidate_pool.
        display_item = (
            reviewed_ranked[0]
            if reviewed_ranked
            else (candidate_pool[0] if candidate_pool else None)
        )
        if display_item is not None:
            display_signal = display_item.signal
            display_decision = candidate_decisions[display_signal.signal_id]
            quote = quotes.get(display_signal.symbol)
            current = quote.last_price if quote is not None else display_decision.entry_price
            dashboard.set_current_signal(
                display_signal.signal_type.value,
                display_signal.symbol,
                display_signal.strategy.value,
                display_signal.confidence,
                timeframe=display_signal.timeframe.value,
                action=display_decision.action.value,
                rationale=display_decision.rationale,
                target_realistic=display_decision.target_realistic,
            )
            dashboard.set_decision_reason(" ".join(display_decision.rationale))
            _publish_chart(
                display_signal.symbol,
                entry=display_decision.entry_price,
                current=current,
                target=display_decision.target_price,
                stop=display_decision.stop_loss,
                status=display_decision.action.value,
            )

        dashboard.stats.log_activity(
            f"Ranked {len(raw_signals)} signals from {len(scan_symbols)} symbols; "
            f"{len(signals)} signals across {len(review_symbols)} symbols sent to AI",
            "SUCCESS",
        )
        best_by_symbol = {}
        for item in survivors:
            best_by_symbol.setdefault(item.signal.symbol, item)
        for position, symbol in enumerate(survivor_symbols, start=1):
            item = best_by_symbol[symbol]
            dashboard.stats.log_activity(
                f"Rank {position}: {symbol} "
                f"[{item.signal.strategy.value}/{item.signal.timeframe.value}] "
                f"p={item.estimated_win_probability:.1%} "
                f"ER={item.expected_r_multiple:+.2f} "
                f"history={item.historical_sample_size} ({item.accuracy_status})",
                "INFO",
            )

        indicators_dict: dict[str, dict[str, Any]] = {}
        for symbol in review_symbols:
            indicators_by_tf = indicators_by_symbol[symbol]
            primary_indicators = next(
                (indicators_by_tf[tf] for tf in signal_timeframes if tf in indicators_by_tf),
                next(iter(indicators_by_tf.values())),
            )
            indicators_dict[symbol] = primary_indicators.to_dict()

        if signals:
            sig = signals[0]
            decision = candidate_decisions[sig.signal_id]
            dashboard.set_current_signal(
                sig.signal_type.value,
                sig.symbol,
                sig.strategy.value,
                sig.confidence,
                timeframe=sig.timeframe.value,
                action=decision.action.value,
                rationale=decision.rationale,
                target_realistic=decision.target_realistic,
            )
            primary_quote = quotes[sig.symbol]
            primary_indicators = indicators_by_symbol[sig.symbol][sig.timeframe]
            direction = "bullish" if primary_quote.is_bullish else "bearish"
            # RSI/ADX can be None during the indicator warm-up window (a signal may come
            # from a strategy that doesn't need them), so format defensively — a bare
            # f"{None:.1f}" raises TypeError and kills the cycle (#17).
            reason = (
                f"{direction.title()} momentum ({primary_quote.change_percent:+.2f}%) "
                f"with {sig.strategy.value} strategy on {sig.timeframe.value} "
                f"(RSI: {fmt_optional(primary_indicators.rsi, '.1f')}, "
                f"ADX: {fmt_optional(primary_indicators.adx, '.1f')})"
            )
            dashboard.set_decision_reason(reason + " " + " ".join(decision.rationale))

        dashboard.stats.signals_generated += len(raw_signals)
        for signal in signals:
            dashboard.stats.log_activity(
                f"Signal: {signal.signal_type.value} {signal.symbol} "
                f"[{signal.strategy.value}/{signal.timeframe.value}] "
                f"conf={signal.confidence:.0%}",
                "INFO",
            )
        await refresh_dashboard()

        if not signals:
            _publish_cycle_audit()
            cycle_llm = get_cost_tracker().summary_since(cycle_started_iso, "NSE")
            record_trading_cycle(
                {
                    "market": "NSE",
                    "provider": "DHAN",
                    "cycle_id": cycle,
                    "execution_mode": runtime_mode.value,
                    "trade_horizon": "SCALP+SWING" if settings.scalp_enabled else "SWING",
                    "changed_timeframes": sorted(
                        {event.timeframe.value for event in candle_events}
                    ),
                    "symbols_considered": len(scan_symbols),
                    "symbols_scanned": len(event_scan_symbols),
                    "raw_strategy_signals": len(raw_signals),
                    "technical_setups": len(stage1_candidates),
                    "technical_candidates": len(ml_candidates),
                    "ml_candidates": ml_evaluated_count,
                    "ml_evaluated": ml_evaluated_count,
                    "ml_abstentions": ml_abstention_count,
                    "ml_cache_hits": (
                        prediction_metrics_after.hits - prediction_metrics_before.hits
                    ),
                    "ml_cache_misses": (
                        prediction_metrics_after.misses - prediction_metrics_before.misses
                    ),
                    "qwen_reviewed": 0,
                    "qwen_cache_hits": 0,
                    "all_llm_calls": cycle_llm["calls"],
                    "candidate_qwen_calls": cycle_llm["candidate_review_calls"],
                    "market_context_calls": cycle_llm["context_support_calls"],
                    "signals_validated": 0,
                    "risk_approved": 0,
                    "orders_created": 0,
                    "signal_funnel": funnel_audit.to_dict(),
                    "top_rejection_reasons": funnel_audit.rejection_rows()[:10],
                    "cycle_duration_ms": (perf_counter() - cycle_started_at) * 1000.0,
                }
            )
            waits = sum(
                decision.action == CandidateAction.WAIT for decision in candidate_decisions.values()
            )
            rejects = sum(
                decision.action == CandidateAction.REJECT
                for decision in candidate_decisions.values()
            )
            dashboard.stats.log_activity(
                f"No qualifying BUY candidates (WAIT {waits}, REJECT {rejects}); "
                "no trade was forced",
                "INFO",
            )
            dashboard.stats.strategy_reasoning = (
                "Strategy selection and Qwen candidate review were not invoked because "
                f"the deterministic pre-check held all {waits + rejects} shortlisted "
                "candidates at WAIT/REJECT. Market context and local ML still ran."
            )
            dashboard.set_ai_review_status(
                "skipped_precheck",
                f"Deep Qwen review skipped safely: {waits} WAIT, {rejects} REJECT",
            )
            dashboard.set_cycle_stage("no_candidates", "No qualifying candidates after ranking")
            for _ in range(15):
                await asyncio.sleep(1)
                await refresh_dashboard()
            return

        review_fingerprint = tuple(
            f"{item.signal.symbol}|{item.signal.strategy.value}|"
            f"{item.signal.timeframe.value}|{item.signal.entry_price:.4f}|"
            f"{item.estimated_win_probability:.4f}"
            for item in reviewed_ranked
        )
        if (
            not settings.llm_cost_optimization_enabled
            and review_fingerprint == last_review_fingerprint
        ):
            dashboard.set_ai_review_status(
                "skipped_duplicate", "Deep review skipped: candidate set is unchanged"
            )
            dashboard.stats.log_activity(
                "Ranked signal set unchanged; skipping duplicate Qwen/news review", "INFO"
            )
            # Keep the normal no-op cycle cadence. Without this cooldown, the outer
            # loop immediately rescans the same unchanged set and busy-loops.
            for _ in range(15):
                await asyncio.sleep(1)
                await refresh_dashboard()
            return

        # ── FinOps spend gate ──────────────────────────────────
        # The agent pipeline below is the only LLM spend. If the daily token/cost
        # budget is exhausted, skip it (and any new entries) to conserve spend —
        # exits in Step 0 already ran, so risk is still managed.
        cost_tracker = get_cost_tracker()
        if cost_tracker.is_over_hard_budget("SWING"):
            status = cost_tracker.budget_status("SWING")
            await get_alert_manager().alert(
                "finops_hard_budget",
                f"Daily LLM budget exhausted ({status['tokens_used']:,} tokens / "
                f"${status['cost_used_usd']:.4f} used) — pausing new agent cycles.",
                level="CRITICAL",
            )
            dashboard.stats.log_activity(
                "FinOps HARD budget reached — skipping agent pipeline this cycle", "WARNING"
            )
            dashboard.set_ai_review_status(
                "skipped_budget", "Deep Qwen review skipped: daily LLM budget exhausted"
            )
            dashboard.set_cycle_stage("budget_paused", "Daily LLM budget exhausted — paused")
            for _ in range(15):
                await asyncio.sleep(1)
                await refresh_dashboard()
            return

        # ── Step 5: Run agent pipeline ─────────────────────────
        # The full universe is already covered by the deterministic screen. Keep
        # the LLM context to the reviewed names instead of dumping hundreds of
        # raw quotes into a prompt.
        reviewed_symbols = set(indicators_dict)
        market_data = {s: quotes[s].to_dict() for s in reviewed_symbols}

        market_breadth = sum(1 for quote in quotes.values() if quote.is_bullish) / max(
            len(quotes), 1
        )
        memory_lessons = memory_db.get_top_lessons_for_context(
            regime="trending_up" if market_breadth >= 0.5 else "trending_down",
            strategies=[
                "momentum",
                "trend_following",
                "ema_heiken_ashi_rsi",
                "ema_psar",
                "ema_cci",
            ],
            n=5,
        )

        workflow_id = f"LIVE-{datetime.now().strftime('%Y%m%d%H%M%S')}-{cycle}"

        # Mark positions to market and update intraday drawdown (incl. unrealized) BEFORE
        # the kill-switch check, so a deep unrealized drawdown actually halts trading.
        paper_engine.update_positions_pnl(market_prices)
        if market_prices:
            now = perf_counter()
            if now - _price_snapshot_state["last_at"] >= PRICE_SNAPSHOT_INTERVAL_SECONDS:
                _price_snapshot_state["last_at"] = now
                await asyncio.to_thread(paper_engine.snapshot_current_prices)
        drawdown_tracker.update(paper_engine.get_total_value())
        daily_risk_store.update_drawdown(drawdown_tracker.max_drawdown)

        # trades_count/profit_loss come from Postgres (today's IST-scoped row), not the
        # in-memory dashboard counters — those reset on every restart, which would silently
        # reset the daily trade limit and daily loss limit along with them.
        daily_stats = daily_risk_store.get_today()
        # `simulated_session` is a startup-only, all-or-nothing flag from before
        # MarketDataManager/HistoryManager learned to switch real vs simulated
        # dynamically per cycle (is_market_hours(), re-checked every call) -- with
        # real Dhan data enabled at all, simulated_session is permanently False, so
        # risk_compliance's FORCE_TRADING_WINDOW bypass (which requires this exact
        # marker -- see force_window_requires_simulation_marker in
        # RiskLimits.from_settings()) never actually applied. Confirmed live: every
        # weekend entry attempt was blocked at "Outside trading hours" regardless of
        # FORCE_TRADING_WINDOW, even candidates that cleared every earlier gate.
        # OR in the live per-cycle pipeline state so the bypass tracks which data
        # actually drove THIS cycle's decision, not a stale startup snapshot.
        daily_stats["simulation_mode"] = simulated_session
        portfolio = {
            "capital": paper_engine.get_balance(),
            "positions": [p.to_dict() for p in paper_engine.get_positions()],
        }

        # Real return-correlation, not just a sector-membership proxy: two names in
        # different sectors can still move together. Only worth computing over the
        # symbols that could actually interact with an open-position risk check —
        # existing holdings plus this cycle's reviewed candidates.
        correlation_symbols = {p["symbol"] for p in portfolio["positions"]} | {
            s.symbol for s in signals
        }
        if len(correlation_symbols) > 1:
            price_histories = {
                symbol: frame["close"]
                for symbol in correlation_symbols
                if (
                    frame := history_manager.get_history(
                        symbol,
                        bars=settings.pairwise_correlation_lookback_days,
                        include_forming=False,
                    )
                )
                is not None
            }
            portfolio["return_correlations"] = compute_return_correlations(price_histories)

        signal_payloads = []
        ranked_by_id = {item.signal.signal_id: item for item in reviewed_ranked}
        for signal in signals:
            payload = signal.to_dict()
            decision = candidate_decisions[signal.signal_id]
            payload["candidate_action"] = decision.action.value
            payload["candidate_rationale"] = decision.rationale
            payload["target_realistic"] = decision.target_realistic
            payload["volume_ratio"] = decision.volume_ratio
            payload["vwap"] = decision.vwap
            payload["higher_timeframes_aligned"] = decision.higher_timeframes_aligned
            payload["mtf_confirmation_passed"] = (
                decision.higher_timeframes_aligned >= settings.paper_required_higher_timeframes
            )
            payload["entry_state"] = decision.action.value
            ranked_item = ranked_by_id[signal.signal_id]
            payload["ml_probability"] = ranked_item.estimated_win_probability
            payload["expected_r"] = ranked_item.expected_r_multiple
            primary_frame = await asyncio.to_thread(
                history_manager.get_multi_timeframe_history,
                signal.symbol,
                signal.timeframe,
                2,
            )
            if primary_frame is not None and not primary_frame.empty:
                candle_index = primary_frame.index[-1]
                payload["signal_candle_timestamp"] = (
                    candle_index.isoformat()
                    if hasattr(candle_index, "isoformat")
                    else str(candle_index)
                )
            signal_payloads.append(payload)

        dashboard.set_cycle_stage(
            "ai_review",
            f"AI review: {len(signal_payloads)} candidate(s) "
            "(News/Sentiment/ML → Regime → Strategy → Validation → Risk)",
        )
        dashboard.set_ai_review_status(
            "reviewing", f"Qwen/LangGraph reviewing {len(signal_payloads)} candidate(s)"
        )
        await refresh_dashboard()

        cycle_context = await _ensure_shared_market_context(indicators_dict, memory_lessons)
        final_state = await run_trading_cycle(
            graph=graph,
            market_data=market_data,
            indicators=indicators_dict,
            signals=signal_payloads,
            memory_lessons=memory_lessons,
            portfolio=portfolio,
            daily_stats=daily_stats,
            thread_id=workflow_id,
            precomputed_predictions=precomputed_predictions_map,
            data_namespace=data_namespace,
            trade_horizon="SWING",
            shared_market_context=cycle_context,
            execution_mode=runtime_mode.value,
        )
        last_review_fingerprint = review_fingerprint

        dashboard.set_cycle_stage("processing", "Processing AI review results")

        # ── Step 6: Process results ────────────────────────────
        regime = final_state.get("regime", "unknown")
        confidence = final_state.get("regime_confidence", 0)
        strategies = final_state.get("active_strategies", [])
        dashboard.update_regime(regime, confidence, strategies)
        if hasattr(dashboard.stats, "current_regime"):
            dashboard.stats.current_regime = regime

        # What each agent actually decided this cycle -- populated every cycle
        # (independent of whether a trade results), so "nothing happened" is never a
        # mystery on the dashboard.
        dashboard.stats.regime_reasoning = final_state.get("regime_reasoning", "")
        dashboard.stats.strategy_reasoning = final_state.get("strategy_reasoning", "")
        dashboard.stats.news_headlines = final_state.get("news_headlines", [])
        dashboard.stats.news_sentiment = float(
            (final_state.get("news_sentiment") or {}).get("avg_sentiment", 0.0)
        )
        dashboard.stats.market_mood = final_state.get("market_mood", {})
        dashboard.stats.prediction_signals = final_state.get("prediction_signals", [])
        dashboard.set_ai_review_status(
            "completed", f"Qwen/LangGraph reviewed {len(signal_payloads)} candidate(s)"
        )

        cycle_fallbacks = [
            str(e) for e in final_state.get("errors", []) if "fallback" in str(e).lower()
        ]
        if cycle_fallbacks:
            agent_label, _, cause_msg = cycle_fallbacks[0].partition(":")
            agent_label = agent_label.strip().removesuffix(" fallback")
            dashboard.stats.agent_fallback_notice = (
                f"{agent_label}: {plain_english_fallback_cause(cause_msg)}"
            )
        else:
            dashboard.stats.agent_fallback_notice = ""

        await refresh_dashboard()

        validated = final_state.get("validated_signals", [])
        rejected = final_state.get("rejected_signals", [])
        cycle_llm_metrics = final_state.get("llm_optimization_metrics", {})
        funnel_audit.set(
            "qwen_required",
            int(cycle_llm_metrics.get("qwen_reviews_requested", 0) or 0),
        )
        funnel_audit.set(
            "qwen_skipped_clear",
            int(cycle_llm_metrics.get("qwen_skipped_clear", 0) or 0),
        )
        funnel_audit.set(
            "qwen_cache_hits",
            int(cycle_llm_metrics.get("qwen_cache_hits", 0) or 0),
        )
        funnel_audit.set(
            "qwen_external_calls",
            int(cycle_llm_metrics.get("qwen_calls", 0) or 0),
        )
        funnel_audit.set(
            "qwen_deferred",
            int(cycle_llm_metrics.get("qwen_reviews_deferred", 0) or 0),
        )
        funnel_audit.set("signal_validation_pass", len(validated))
        funnel_audit.set("signal_validation_reject", len(rejected))
        dashboard.stats.signals_validated += len(validated)
        dashboard.stats.signals_rejected += len(rejected)

        for sig in validated:
            strategy = str(sig.get("strategy", "unknown"))
            timeframe = str(sig.get("timeframe", "unknown"))
            horizon = str(sig.get("trade_horizon", "SWING"))
            funnel_audit.strategy_increment(strategy, timeframe, horizon, "validation_passed")
            dashboard.stats.log_activity(
                f"VALIDATED: {sig.get('signal_type')} {sig.get('symbol')} "
                f"[{sig.get('timeframe', 'N/A')}]",
                "SUCCESS",
            )
        rejected_records = []
        for sig in rejected:
            dashboard.stats.log_activity(
                f"REJECTED: {sig.get('signal_type')} {sig.get('symbol')} "
                f"[{sig.get('timeframe', 'N/A')}]",
                "WARNING",
            )
            rejected_records.append(SignalRecord.from_signal(sig, "rejected_validation"))
            validation = sig.get("validation", {})
            handling = str(validation.get("handling", validation.get("decision", ""))).upper()
            reason = (
                RejectionReason.QWEN_CONTEXT_REJECT.value
                if handling in {"CONTEXT_REJECT", "REJECT", "MATERIAL_CONFLICT"}
                else RejectionReason.SIGNAL_VALIDATION_REJECT.value
            )
            if reason == RejectionReason.QWEN_CONTEXT_REJECT.value:
                funnel_audit.increment("qwen_rejected")
            funnel_audit.reject(
                "SIGNAL_VALIDATION",
                reason,
                strategy=str(sig.get("strategy", "")),
                timeframe=str(sig.get("timeframe", "")),
                trade_horizon=str(sig.get("trade_horizon", "SWING")),
                symbol=str(sig.get("symbol", "")),
            )
            funnel_audit.strategy_increment(
                str(sig.get("strategy", "unknown")),
                str(sig.get("timeframe", "unknown")),
                str(sig.get("trade_horizon", "SWING")),
                "validation_rejected",
            )
        await _log_signals_async(rejected_records)
        _publish_cycle_audit()
        await refresh_dashboard()

        # ── Step 7: Execute approved trades via paper engine ───
        approved = final_state.get("approved_trades", [])
        risk_rejected = final_state.get("risk_rejected", [])
        agent_fallbacks = [
            error for error in final_state.get("errors", []) if "fallback" in str(error).lower()
        ]
        degraded_blocked = []
        if agent_fallbacks:
            # The deterministic screen can shortlist candidates, but it must not
            # silently replace the requested LLM review. A degraded agent cycle
            # is analysis-only and can never create a new position.
            degraded_blocked = list(approved)
            approved = []
            if degraded_blocked:
                dashboard.stats.log_activity(
                    f"AGENT DEGRADED - blocking {len(degraded_blocked)} new entry "
                    f"{'signal' if len(degraded_blocked) == 1 else 'signals'}: "
                    f"{agent_fallbacks[0]}",
                    "WARNING",
                )
        long_only_blocked = []
        if settings.long_only:
            long_only_blocked = [
                sig for sig in approved if sig.get("signal_type", "").upper() == "SELL"
            ]
            approved = [sig for sig in approved if sig not in long_only_blocked]
        dashboard.stats.trades_approved += len(approved)
        dashboard.stats.trades_risk_rejected += len(risk_rejected)
        funnel_audit.set("risk_approved", len(approved))
        funnel_audit.set(
            "risk_blocked",
            len(risk_rejected) + len(long_only_blocked) + len(degraded_blocked),
        )
        for sig in approved:
            funnel_audit.strategy_increment(
                str(sig.get("strategy", "unknown")),
                str(sig.get("timeframe", "unknown")),
                str(sig.get("trade_horizon", "SWING")),
                "risk_approved",
            )
        for sig in risk_rejected:
            failures = sig.get("risk_result", {}).get("failures", [])
            first_rule = str(failures[0].get("rule", "")) if failures else ""
            reason = (
                RejectionReason.DUPLICATE_POSITION.value
                if "duplicate" in first_rule
                else RejectionReason.CAPITAL_CONSTRAINT.value
                if "capital" in first_rule or "position" in first_rule
                else RejectionReason.RISK_LIMIT.value
            )
            funnel_audit.reject(
                "RISK",
                reason,
                strategy=str(sig.get("strategy", "")),
                timeframe=str(sig.get("timeframe", "")),
                trade_horizon=str(sig.get("trade_horizon", "SWING")),
                symbol=str(sig.get("symbol", "")),
            )
            funnel_audit.strategy_increment(
                str(sig.get("strategy", "unknown")),
                str(sig.get("timeframe", "unknown")),
                str(sig.get("trade_horizon", "SWING")),
                "risk_rejected",
            )
        _publish_cycle_audit()

        # Surface WHY entries were blocked so a run with signals-but-no-trades is not a
        # mystery (#19). The most common off-hours cause is the deterministic
        # trading-hours guard (09:15–15:15 IST) — every entry is blocked when the NSE is
        # closed, which is intentional, not a bug.
        outcome_records = []
        for sig in risk_rejected:
            failures = sig.get("risk_result", {}).get("failures", [])
            # Strategy eligibility is the most useful root cause when present; it is
            # more actionable than whichever independent risk check happened to run first.
            admission_failure = next(
                (f for f in failures if f.get("rule") == "strategy_eligibility"), None
            )
            chosen_failure = admission_failure or (failures[0] if failures else None)
            reason = chosen_failure.get("message") if chosen_failure else "risk rules"
            dashboard.stats.log_activity(
                f"RISK BLOCKED: {sig.get('signal_type')} {sig.get('symbol')} "
                f"[{sig.get('timeframe', 'N/A')}] — {reason}",
                "WARNING",
            )
            outcome_records.append(SignalRecord.from_signal(sig, "rejected_risk"))
        for sig in long_only_blocked:
            dashboard.stats.log_activity(
                f"LONG-ONLY BLOCKED: SELL {sig.get('symbol')} [{sig.get('timeframe', 'N/A')}]",
                "INFO",
            )
            outcome_records.append(
                SignalRecord.from_signal(
                    sig,
                    "rejected_risk",
                    reason="Long-only investing mode does not open short positions",
                )
            )
        for sig in degraded_blocked:
            outcome_records.append(
                SignalRecord.from_signal(
                    sig,
                    "rejected_risk",
                    reason="LLM agent review degraded; new entries fail closed",
                )
            )
        for sig in approved:
            outcome_records.append(SignalRecord.from_signal(sig, "approved"))
        await _log_signals_async(outcome_records)

        # Kill-switch gate: a daily-loss / drawdown breach must halt NEW entries
        # at the point of execution (exits in Step 0 still run, to flatten risk).
        # Previously the kill switch only ended the agent graph at the regime edge
        # and never re-checked here, so a mid-cycle breach still placed orders.
        # Use peak equity as the capital base so the drawdown % is true (the graph's
        # `portfolio.capital` is cash-only and would distort it).
        kill_state = {
            "daily_stats": daily_stats,
            "portfolio": {"capital": max(drawdown_tracker.peak_equity, 1.0)},
        }
        if market_kill_switch_active(settings, "NSE") or check_kill_switch(kill_state):
            if approved:
                dashboard.stats.log_activity(
                    f"KILL SWITCH ACTIVE — blocking {len(approved)} approved "
                    f"{'entry' if len(approved) == 1 else 'entries'} "
                    f"(loss/drawdown {drawdown_tracker.drawdown_pct:.1f}%)",
                    "WARNING",
                )
                approved = []

            # Stop the bleed: flatten all open positions (not just block new entries).
            # place_order() closes by (symbol, side) FIFO, not by position id, so a single
            # snapshot pass may not fully flatten multiple/odd-sized same-symbol positions.
            # Loop until the engine reports flat (capped to avoid spinning), and clear the
            # exit-manager / open-trade tracking ONLY once it actually is — otherwise we'd
            # lose sight of still-open risk.
            if settings.kill_switch_flatten and paper_engine.get_positions():
                for pos in list(exit_manager.get_managed_positions()):
                    px = market_prices.get(pos.symbol, pos.entry_price)
                    await _execute_managed_exit(
                        pos,
                        quantity=pos.quantity,
                        price=px,
                        reason="kill_switch",
                        explanation="deterministic daily-loss/drawdown kill switch",
                    )
                dashboard.stats.current_balance = paper_engine.get_balance()
                if paper_engine.get_positions():
                    # Could not fully flatten — keep risk tracking intact and escalate
                    # rather than clearing (which would hide the residual exposure).
                    remaining = len(paper_engine.get_positions())
                    dashboard.stats.log_activity(
                        f"FLATTEN INCOMPLETE — {remaining} position(s) still open; "
                        "keeping risk tracking.",
                        "ERROR",
                    )
                    await get_alert_manager().alert(
                        "kill_switch_flatten_incomplete",
                        f"Kill switch fired but {remaining} position(s) could not be "
                        "flattened — risk tracking retained.",
                        level="CRITICAL",
                    )
                else:
                    await get_alert_manager().alert(
                        "kill_switch_flatten",
                        f"Kill switch fired (drawdown {drawdown_tracker.drawdown_pct:.1f}%) — "
                        "flattened all open positions.",
                        level="CRITICAL",
                    )

        # AI review can take long enough for the original REST quote batch to become
        # stale. Refresh once, then enforce exchange-time freshness per approved symbol.
        if runtime_mode is RuntimeExecutionMode.MARKET_PAPER and not is_live:
            await asyncio.to_thread(market_manager.refresh)
            quotes = market_manager.get_all_quotes()
            market_prices.update({s: q.last_price for s, q in quotes.items()})

        reservations = PaperRiskReservations()
        final_risk_approved_trades: list[dict[str, Any]] = []
        filled_trades: list[dict[str, Any]] = []
        for trade in approved:
            symbol = trade.get("symbol", "N/A")
            side = trade.get("signal_type", "BUY").upper()

            # Cash-investing mode does not treat a bearish signal as permission to
            # short. Existing long holdings are exited only by ExitManager rules.
            if settings.long_only and side == "SELL":
                funnel_audit.increment("risk_blocked")
                funnel_audit.reject(
                    "FINAL_RISK",
                    RejectionReason.LONG_ONLY.value,
                    strategy=str(trade.get("strategy", "")),
                    timeframe=str(trade.get("timeframe", "")),
                    trade_horizon=str(trade.get("trade_horizon", "SWING")),
                    symbol=str(symbol),
                )
                dashboard.stats.log_activity(
                    f"LONG-ONLY: ignoring new SELL signal for {symbol}", "INFO"
                )
                continue

            # Circuit guard: don't open into a scrip at/through its NSE price band — near a
            # circuit you can't get a sane fill (limit-up: no sellers; limit-down: no buyers).
            if settings.circuit_guard_enabled:
                q = quotes.get(symbol)
                if q is not None and is_circuit_locked(
                    q.change_percent, settings.default_circuit_band_pct
                ):
                    funnel_audit.increment("risk_blocked")
                    funnel_audit.reject(
                        "FINAL_RISK",
                        RejectionReason.RISK_LIMIT.value,
                        strategy=str(trade.get("strategy", "")),
                        timeframe=str(trade.get("timeframe", "")),
                        trade_horizon=str(trade.get("trade_horizon", "SWING")),
                        symbol=str(symbol),
                    )
                    dashboard.stats.log_activity(
                        f"CIRCUIT LOCKED: skipping {symbol} ({q.change_percent:+.1f}%)",
                        "WARNING",
                    )
                    continue

            quoted_price = float(market_prices.get(symbol, 0.0) or 0.0)
            reviewed_entry = float(trade.get("entry_price", 0.0) or 0.0)
            stop_loss = float(trade.get("stop_loss", 0.0) or 0.0)
            target_price = float(trade.get("target_price", 0.0) or 0.0)
            strategy = str(trade.get("strategy", "unknown"))
            if runtime_mode is RuntimeExecutionMode.MARKET_PAPER:
                quote_freshness = executable_market_quote_is_fresh(
                    quotes.get(symbol),
                    present_in_latest_batch=market_manager.is_current_batch_symbol(symbol),
                    max_age_seconds=settings.max_quote_staleness_seconds,
                    session_date=now_ist().date(),
                )
                if not quote_freshness.approved:
                    funnel_audit.increment("risk_blocked")
                    funnel_audit.reject(
                        "FINAL_RISK",
                        RejectionReason.STALE_QUOTE.value,
                        strategy=str(trade.get("strategy", "")),
                        timeframe=str(trade.get("timeframe", "")),
                        trade_horizon=str(trade.get("trade_horizon", "SWING")),
                        symbol=str(symbol),
                    )
                    dashboard.stats.log_activity(
                        f"WAIT: {symbol} quote rejected — {quote_freshness.reason}",
                        "WARNING",
                    )
                    continue
            if reviewed_entry <= 0 or quoted_price <= 0:
                funnel_audit.increment("risk_blocked")
                funnel_audit.reject(
                    "FINAL_RISK",
                    RejectionReason.INVALID_QUOTE.value,
                    strategy=str(trade.get("strategy", "")),
                    timeframe=str(trade.get("timeframe", "")),
                    trade_horizon=str(trade.get("trade_horizon", "SWING")),
                    symbol=str(symbol),
                )
                dashboard.stats.log_activity(
                    f"WAIT: {symbol} has no exact reviewed/current price", "WARNING"
                )
                continue
            if not reviewed_price_is_fresh(reviewed_entry, quoted_price):
                funnel_audit.increment("risk_blocked")
                funnel_audit.reject(
                    "FINAL_RISK",
                    RejectionReason.STALE_QUOTE.value,
                    strategy=str(trade.get("strategy", "")),
                    timeframe=str(trade.get("timeframe", "")),
                    trade_horizon=str(trade.get("trade_horizon", "SWING")),
                    symbol=str(symbol),
                )
                dashboard.stats.log_activity(
                    f"WAIT: {symbol} moved after review; exact approval is stale", "WARNING"
                )
                continue
            requested_price = reviewed_entry
            expected_fill = paper_engine.cost_model.fill_price(requested_price, side)

            # Risk-based position sizing: size from the risk budget (risk-per-trade and the
            # stop distance) using the real PositionSizer — and the strategy's actual
            # win-rate (Kelly) when enough history exists — instead of a flat 5% of cash.
            if expected_fill > 0 and 0 < stop_loss < expected_fill < target_price:
                win_rate = perf_tracker.get_strategy_performance(strategy, regime).win_rate
                sizing = calculate_position_size(
                    capital=paper_engine.get_total_value(),
                    entry_price=expected_fill,
                    stop_loss=stop_loss,
                    target_price=target_price,
                    risk_per_trade=settings.risk_per_trade,
                    max_position_pct=settings.max_position_pct,
                    win_rate=win_rate,
                )
                quantity = sizing.shares
            else:
                quantity = 0

            final_order = FinalPaperOrder(
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=expected_fill,
                stop_loss=stop_loss,
                target_price=target_price,
                strategy=strategy,
            )
            current_daily = daily_risk_store.get_today()
            final_risk = reservations.evaluate(
                final_order,
                positions=paper_engine.get_positions(),
                equity=paper_engine.get_total_value(),
                entries_today=int(current_daily["trades_count"]),
                daily_entry_cap=min(settings.max_daily_trades, settings.paper_daily_entry_cap),
                max_positions=settings.max_concurrent_positions,
                max_position_pct=settings.max_position_pct,
                max_total_exposure_pct=settings.max_total_exposure_pct,
                risk_per_trade=settings.risk_per_trade,
                max_total_risk=settings.max_total_risk,
                realized_daily_pnl=float(current_daily["profit_loss"]),
                daily_loss_limit=settings.daily_loss_limit,
                daily_loss_buffer=settings.open_risk_daily_loss_buffer,
            )
            if not final_risk.approved:
                funnel_audit.increment("risk_blocked")
                funnel_audit.reject(
                    "FINAL_RISK",
                    RejectionReason.RISK_LIMIT.value,
                    strategy=str(trade.get("strategy", "")),
                    timeframe=str(trade.get("timeframe", "")),
                    trade_horizon=str(trade.get("trade_horizon", "SWING")),
                    symbol=str(symbol),
                )
                dashboard.stats.log_activity(
                    f"FINAL RISK BLOCKED: {symbol} — {final_risk.reasons[0]}", "WARNING"
                )
                continue
            reservations.reserve(final_order)
            final_risk_approved_trades.append(trade)

            signal_id = str(trade.get("signal_id", ""))
            decision = candidate_decisions.get(signal_id)
            rationale = (
                decision.rationale
                if decision is not None
                else list(trade.get("candidate_rationale", []))
            )
            idempotency_key = f"{data_namespace}:{workflow_id}:{signal_id}:{symbol}:entry"
            trade_id = lifecycle_store.new_trade_id()
            lifecycle_store.create_intent(
                namespace=data_namespace,
                run_id=run_id,
                workflow_id=workflow_id,
                idempotency_key=idempotency_key,
                signal_id=signal_id,
                symbol=symbol,
                side=side,
                strategy=strategy,
                timeframe=str(trade.get("timeframe", "")),
                quantity=quantity,
                entry_price=expected_fill,
                stop_loss=stop_loss,
                target_price=target_price,
                rationale=rationale,
                context={
                    "regime": regime,
                    "regime_confidence": confidence,
                    "validation": trade.get("validation", {}),
                    "risk_result": trade.get("risk_result", {}),
                    "news_sentiment": final_state.get("news_sentiment", {}),
                    "market_mood": final_state.get("market_mood", {}),
                    "prediction_signals": final_state.get("prediction_signals", []),
                    "target_realistic": (
                        decision.target_realistic if decision is not None else True
                    ),
                },
                active_lessons=(
                    feedback.lesson_ids(memory_lessons) if settings.enable_learning else []
                ),
                trade_id=trade_id,
            )

            # Execute via the unified execution service (idempotent; shadow-aware;
            # awaits the broker for live modes).
            result = await execution_service.submit_async(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=requested_price,
                idempotency_key=idempotency_key,
                trade_id=trade_id,
                position_id=trade_id,
                stop_loss=stop_loss,
                target_price=target_price,
                strategy=strategy,
                reason="entry",
                entry_data_source=runtime_mode.expected_quote_source,
            )
            reservations.release(final_order)
            validation = trade.get("validation", {})
            record_candidate_execution(
                {
                    "symbol": symbol,
                    "strategy": strategy,
                    "trade_horizon": "SWING",
                    "timeframe": trade.get("timeframe"),
                    "model_version": trade.get("model_version", ""),
                    "validation_status": trade.get("eligibility_status", ""),
                    "current_regime": trade.get("current_regime", ""),
                    "regime_policy": trade.get("regime_policy", ""),
                    "original_confidence": trade.get("original_confidence"),
                    "regime_adjusted_confidence": trade.get("regime_adjusted_confidence"),
                    "ml_confidence": trade.get("ml_confidence"),
                    "registry_decision": trade.get("registry_decision", ""),
                    "risk_decision": trade.get("risk_decision", "APPROVED"),
                    "final_outcome": result.status,
                    "rejection_reason": trade.get("rejection_reason", ""),
                    "candidate_fingerprint": trade.get("candidate_fingerprint"),
                    "cache_hit": validation.get("cache_hit", False),
                    "model": validation.get("model_used", ""),
                    "context_version": (cycle_context or {}).get("context_version", ""),
                    "decision": validation.get("decision", "approve"),
                    "execution_result": result.status,
                }
            )

            if result.is_duplicate:
                funnel_audit.reject(
                    "EXECUTION",
                    RejectionReason.DUPLICATE_POSITION.value,
                    strategy=strategy,
                    timeframe=str(trade.get("timeframe", "")),
                    trade_horizon=str(trade.get("trade_horizon", "SWING")),
                    symbol=str(symbol),
                )
                dashboard.stats.log_activity(f"DUPLICATE suppressed: {side} {symbol}", "INFO")
                continue

            if result.filled:
                filled_trades.append(trade)
                funnel_audit.strategy_increment(
                    strategy,
                    str(trade.get("timeframe", "unknown")),
                    str(trade.get("trade_horizon", "SWING")),
                    "final_orders",
                )
                tf = trade.get("timeframe", "N/A")
                dashboard.stats.log_activity(
                    f"PAPER FILL: {side} {quantity} {symbol} @ Rs.{result.fill_price:,.2f} [{tf}]",
                    "TRADE",
                )
                dashboard.stats.current_balance = paper_engine.get_balance()
                lifecycle_store.mark_open(
                    trade_id,
                    order_id=result.order_id,
                    position_id=trade_id,
                    quantity=quantity,
                    fill_price=result.fill_price,
                    entry_charges=result.entry_charges,
                )
                daily_risk_store.record_entry()

                # Register with exit manager for tracking
                exit_manager.register_position(
                    position_id=trade_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    entry_price=result.fill_price,
                    stop_loss=stop_loss,
                    target_price=target_price,
                    strategy=strategy,
                    regime=regime,
                    timeframe=str(trade.get("timeframe", "")),
                )
                try:
                    trade_record = {
                        **trade,
                        "quantity": quantity,
                        "entry_price": result.fill_price,
                        "stop_loss": stop_loss,
                        "target_price": target_price,
                        "candidate_decision": (decision.to_dict() if decision is not None else {}),
                    }
                    journal.record_trade(
                        trade_record,
                        workflow_id,
                        final_state,
                        trade_id=trade_id,
                        run_id=run_id,
                    )
                except Exception as e:
                    logger.warning("Journal record_trade failed: %s", e)
                _publish_chart(
                    symbol,
                    entry=result.fill_price,
                    current=result.fill_price,
                    target=target_price,
                    stop=stop_loss,
                    status="OPEN",
                )
            else:
                lifecycle_store.mark_rejected(trade_id, result.message or result.status)
                dashboard.stats.log_activity(
                    f"ORDER {result.status}: {symbol} — {result.message}", "WARNING"
                )

        # Materialize the final Decision view after deterministic risk. This journal
        # observes the existing runtime; it cannot approve or execute anything.
        decision_records_by_candidate = {}

        def _capture_decision(
            payload: dict[str, Any],
            status: DecisionStatus,
            reasons: tuple[str, ...] = (),
        ) -> None:
            try:
                record = decision_record_from_payload(
                    market="NSE",
                    provider="DHAN",
                    payload=payload,
                    status=status,
                    rejection_reasons=reasons,
                )
                decision_records_by_candidate[record.candidate_id] = record
            except (TypeError, ValueError):
                logger.warning("Skipping malformed NSE Decision payload", exc_info=True)

        for trade in rejected:
            _capture_decision(trade, DecisionStatus.REJECTED, ("SIGNAL_VALIDATION_REJECTED",))
        for trade in risk_rejected:
            failures = trade.get("risk_result", {}).get("failures", [])
            reason = (
                str(failures[0].get("rule", "RISK_REJECTED"))
                if failures
                else "RISK_REJECTED"
            )
            _capture_decision(trade, DecisionStatus.REJECTED, (reason,))
        for trade in long_only_blocked:
            _capture_decision(trade, DecisionStatus.REJECTED, ("LONG_ONLY_POLICY",))
        for trade in degraded_blocked:
            _capture_decision(trade, DecisionStatus.REJECTED, ("AGENT_REVIEW_DEGRADED",))

        final_approved_ids = {
            stable_candidate_id("NSE", trade) for trade in final_risk_approved_trades
        }
        filled_ids = {stable_candidate_id("NSE", trade) for trade in filled_trades}
        for trade in approved:
            candidate_id = stable_candidate_id("NSE", trade)
            if candidate_id in filled_ids:
                _capture_decision(trade, DecisionStatus.PAPER_FILLED)
            elif candidate_id in final_approved_ids:
                _capture_decision(trade, DecisionStatus.PAPER_APPROVED)
            else:
                _capture_decision(trade, DecisionStatus.REJECTED, ("FINAL_RISK_BLOCKED",))
        await asyncio.to_thread(
            persist_decision_records,
            decision_repository,
            decision_records_by_candidate.values(),
        )

        await refresh_dashboard()

        # Update paper engine P&L with current prices
        paper_engine.update_positions_pnl(market_prices)
        if market_prices:
            now = perf_counter()
            if now - _price_snapshot_state["last_at"] >= PRICE_SNAPSHOT_INTERVAL_SECONDS:
                _price_snapshot_state["last_at"] = now
                await asyncio.to_thread(paper_engine.snapshot_current_prices)

        # Update dashboard balance from paper engine
        dashboard.stats.current_balance = paper_engine.get_balance()

        # ── FinOps: surface today's LLM spend + soft-budget alert ──
        cost_summary = cost_tracker.daily_summary("NSE")
        dashboard.stats.llm_calls = cost_summary["calls"]
        dashboard.stats.llm_tokens = cost_summary["total_tokens"]
        dashboard.stats.llm_cost_usd = cost_summary["total_cost_usd"]
        dashboard.stats.llm_input_tokens = cost_summary["input_tokens"]
        dashboard.stats.llm_output_tokens = cost_summary["output_tokens"]
        dashboard.stats.qwen_calls_by_agent = {
            agent: int(values.get("calls", 0)) for agent, values in cost_summary["by_agent"].items()
        }
        dashboard.stats.qwen_input_tokens_by_agent = {
            agent: int(values.get("input_tokens", 0))
            for agent, values in cost_summary["by_agent"].items()
        }
        dashboard.stats.qwen_output_tokens_by_agent = {
            agent: int(values.get("output_tokens", 0))
            for agent, values in cost_summary["by_agent"].items()
        }
        dashboard.stats.qwen_cost_by_agent = {
            agent: float(values.get("cost_usd", 0.0))
            for agent, values in cost_summary["by_agent"].items()
        }

        cycle_summary = cost_tracker.summary_since(cycle_started_iso, "NSE")
        candidate_bucket = cycle_summary["by_reason"].get("CANDIDATE_REVIEW", {})
        cycle_calls = int(candidate_bucket.get("calls", 0) or 0)
        cycle_input = int(candidate_bucket.get("input_tokens", 0) or 0)
        cycle_output = int(candidate_bucket.get("output_tokens", 0) or 0)
        cycle_cost = float(candidate_bucket.get("cost_usd", 0.0) or 0.0)
        dashboard.stats.qwen_calls_per_cycle = cycle_calls
        dashboard.stats.llm_finops = cost_summary
        dashboard.stats.llm_session_metrics = cost_tracker.session_summary("NSE")
        dashboard.stats.llm_cycle_metrics = cycle_summary
        for horizon, attribute in (
            ("SCALP", "scalp_qwen_cost"),
            ("SWING", "swing_qwen_cost"),
        ):
            now_bucket = cost_summary["by_horizon"].get(horizon, {})
            start_bucket = cycle_cost_start["by_horizon"].get(horizon, {})
            setattr(
                dashboard.stats,
                attribute,
                float(now_bucket.get("cost_usd", 0.0)) - float(start_bucket.get("cost_usd", 0.0)),
            )

        swing_metrics = final_state.get("llm_optimization_metrics", {})
        scalp_metrics = dashboard.stats.scalp_funnel or {}
        cache_hits = int(swing_metrics.get("qwen_cache_hits", 0) or 0) + int(
            scalp_metrics.get("qwen_cache_hits", 0) or 0
        )
        cache_misses = int(swing_metrics.get("qwen_cache_misses", 0) or 0) + int(
            scalp_metrics.get("qwen_cache_misses", 0) or 0
        )
        raw_candidates = len(raw_signals) + int(scalp_metrics.get("raw_candidates", 0) or 0)
        deterministic_skips = max(0, raw_candidates - cache_hits - cache_misses)
        zero_candidates = max(0, raw_candidates - cache_misses)
        zero_pct = 100.0 * zero_candidates / raw_candidates if raw_candidates else 100.0
        approved_count = int(swing_metrics.get("qwen_approved", 0) or 0) + int(
            scalp_metrics.get("qwen_approved", 0) or 0
        )
        reviewed_count = cache_hits + cache_misses
        executed_count = max(0, dashboard.stats.total_trades - cycle_trades_start)
        dashboard.stats.qwen_cache_hits = cache_hits
        dashboard.stats.qwen_cache_misses = cache_misses
        dashboard.stats.qwen_cache_hit_rate = (
            100.0 * cache_hits / reviewed_count if reviewed_count else 0.0
        )
        dashboard.stats.dedup_skip_count = cache_hits
        dashboard.stats.deterministic_rejection_count = deterministic_skips
        dashboard.stats.zero_llm_candidate_percentage = zero_pct
        dashboard.stats.cost_per_candidate_reviewed = (
            cycle_cost / cache_misses if cache_misses else 0.0
        )
        dashboard.stats.cost_per_ai_approved_candidate = (
            cycle_cost / approved_count if approved_count else 0.0
        )
        dashboard.stats.cost_per_executed_trade = (
            cycle_cost / executed_count if executed_count else 0.0
        )
        dashboard.stats.log_activity(
            f"LLM this cycle: total={cycle_summary['calls']} "
            f"candidate_Qwen={cycle_calls} context/support="
            f"{cycle_summary['context_support_calls']}; cache_hits={cache_hits} "
            f"deterministic_skips={deterministic_skips}; "
            f"tokens={cycle_input}in/{cycle_output}out cost=${cycle_cost:.6f}; "
            f"zero-Qwen candidates={zero_pct:.1f}%",
            "INFO",
        )
        _publish_cycle_audit()
        budget = cost_tracker.budget_status()
        if budget["soft_breached"] and not budget["hard_breached"]:
            await get_alert_manager().alert(
                "finops_soft_budget",
                f"Approaching daily LLM budget: {budget['tokens_used']:,} tokens / "
                f"${budget['cost_used_usd']:.4f} used "
                f"(soft limit {budget['soft_pct']:.0%}).",
                level="WARNING",
            )

        # ── Profit goal: pace tracking (advisory only — never changes sizing) ──
        goal_report = goal_engine.evaluate(
            capital=paper_engine.get_balance(),
            realized_pnl=dashboard.stats.realized_pnl,
        )
        if goal_report.get("enabled"):
            dashboard.stats.goal_mtd_pnl = goal_report["month_to_date_pnl"]
            dashboard.stats.goal_expected_to_date = goal_report["expected_to_date"]
            dashboard.stats.goal_on_pace = goal_report["on_pace"]
            dashboard.stats.goal_status = goal_report["status"]
            if not goal_report["feasible"]:
                await get_alert_manager().alert(
                    "goal_infeasible",
                    "Profit target not feasible within risk budget. "
                    f"{goal_report['plan']['recommended_action']}",
                    level="WARNING",
                )
            elif not goal_report["on_pace"]:
                await get_alert_manager().alert(
                    "goal_off_pace",
                    f"Behind profit pace: month-to-date Rs.{goal_report['month_to_date_pnl']:,.0f} "
                    f"vs pace Rs.{goal_report['expected_to_date']:,.0f}. "
                    "Risk limits are fixed — do NOT increase position size to catch up.",
                    level="WARNING",
                )

        strategy_frequency: dict[str, dict[str, int]] = {}

        def _frequency_bucket(signal: Any) -> dict[str, int]:
            payload = signal.to_dict() if hasattr(signal, "to_dict") else signal
            key = (
                f"{payload.get('strategy', 'unknown')}|"
                f"{payload.get('timeframe', 'unknown')}|"
                f"{payload.get('trade_horizon', 'SWING')}"
            )
            return strategy_frequency.setdefault(
                key,
                {
                    "raw": 0,
                    "qualified": 0,
                    "ml_evaluated": 0,
                    "ml_abstained": 0,
                    "qwen_required": 0,
                    "qwen_skipped_clear": 0,
                    "qwen_cache_hit": 0,
                    "qwen_deferred": 0,
                    "risk_approved": 0,
                    "risk_rejected": 0,
                },
            )

        for signal in raw_signals:
            _frequency_bucket(signal)["raw"] += 1
        for item in ml_candidates:
            bucket = _frequency_bucket(item.signal)
            bucket["qualified"] += 1
            if item.signal.ml_status == "VALIDATED":
                bucket["ml_evaluated"] += 1
            else:
                bucket["ml_abstained"] += 1
        for signal in [
            *final_state.get("validated_signals", []),
            *final_state.get("rejected_signals", []),
        ]:
            bucket = _frequency_bucket(signal)
            if signal.get("qwen_review_required"):
                bucket["qwen_required"] += 1
            if signal.get("validation", {}).get("qwen_skipped_clear"):
                bucket["qwen_skipped_clear"] += 1
            if signal.get("validation", {}).get("cache_hit"):
                bucket["qwen_cache_hit"] += 1
            if signal.get("validation", {}).get("decision") == "deferred":
                bucket["qwen_deferred"] += 1
        for signal in approved:
            _frequency_bucket(signal)["risk_approved"] += 1
        for signal in final_state.get("risk_rejected", []):
            _frequency_bucket(signal)["risk_rejected"] += 1

        record_trading_cycle(
            {
                "market": "NSE",
                "provider": "DHAN",
                "cycle_id": cycle,
                "execution_mode": runtime_mode.value,
                "trade_horizon": "SCALP+SWING" if settings.scalp_enabled else "SWING",
                "changed_timeframes": sorted({event.timeframe.value for event in candle_events}),
                "symbols_considered": len(scan_symbols),
                "symbols_scanned": len(event_scan_symbols),
                "raw_strategy_signals": len(raw_signals),
                "technical_setups": len(stage1_candidates),
                "technical_candidates": len(ml_candidates),
                "ml_candidates": ml_evaluated_count,
                "ml_evaluated": ml_evaluated_count,
                "ml_abstentions": ml_abstention_count,
                "ml_cache_hits": (prediction_metrics_after.hits - prediction_metrics_before.hits),
                "ml_cache_misses": (
                    prediction_metrics_after.misses - prediction_metrics_before.misses
                ),
                "qwen_reviewed": reviewed_count,
                "qwen_calls": cycle_calls,
                "qwen_cache_hits": cache_hits,
                "all_llm_calls": cycle_summary["calls"],
                "candidate_qwen_calls": cycle_summary["candidate_review_calls"],
                "market_context_calls": cycle_summary["context_support_calls"],
                "signals_validated": len(validated),
                "risk_approved": len(approved),
                "orders_created": executed_count,
                "signal_funnel": funnel_audit.to_dict(),
                "top_rejection_reasons": funnel_audit.rejection_rows()[:10],
                "strategy_frequency": strategy_frequency,
                "qwen_input_tokens": int(swing_metrics.get("qwen_input_tokens", 0) or 0),
                "qwen_output_tokens": int(swing_metrics.get("qwen_output_tokens", 0) or 0),
                "qwen_reviews_deferred": int(swing_metrics.get("qwen_reviews_deferred", 0) or 0),
                "cycle_duration_ms": (perf_counter() - cycle_started_at) * 1000.0,
            }
        )

        # Wait before next cycle
        wait_time = settings.trading_cycle_seconds
        dashboard.stats.log_activity(f"Next cycle in {wait_time}s...", "INFO")
        funnel_audit.set("risk_approved", len(final_risk_approved_trades))
        funnel_audit.set("final_buy", len(filled_trades))
        funnel_audit.set(
            "final_reject",
            len(rejected)
            + int(funnel_audit.counts.get("risk_blocked", 0))
            + max(0, len(final_risk_approved_trades) - len(filled_trades)),
        )
        _publish_cycle_audit()
        await refresh_dashboard()

        await wait_for_next_cycle(wait_time, dashboard, refresh_dashboard)

    position_management_task = asyncio.create_task(_position_management_loop())
    dashboard.stats.log_activity(
        "Fast quote-driven position-management path started (1s safety timer)", "INFO"
    )
    consecutive_errors = 0
    market_pause_announced = False

    try:
        cycle = 0

        while True:
            if not force_window_active and not is_trading_window(
                settings.no_trading_before, settings.no_trading_after
            ):
                if not market_pause_announced:
                    dashboard.stats.log_activity(
                        "Market closed: trading cycles paused until the next entry window.",
                        "INFO",
                    )
                    market_pause_announced = True
                dashboard.set_cycle_stage("market_closed", "Market closed — cycles paused")
                await refresh_dashboard()
                await asyncio.sleep(60)
                continue

            market_pause_announced = False
            cycle += 1
            dashboard.begin_cycle(cycle)
            dashboard.stats.log_activity(f"=== Trading Cycle #{cycle} ===", "INFO")
            dashboard.set_cycle_stage("starting", f"Cycle #{cycle}: starting", cycle=cycle)
            await refresh_dashboard()

            try:
                await _run_cycle(cycle)
                consecutive_errors = 0
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # Per-cycle isolation: log, back off, and keep the loop alive.
                consecutive_errors += 1
                backoff = min(60, 5 * consecutive_errors)
                dashboard.set_ai_review_status(
                    "failed", f"Cycle failed before review completed: {type(e).__name__}"
                )
                logger.exception("Trading cycle #%d failed", cycle)
                dashboard.stats.log_activity(
                    f"Cycle #{cycle} ERROR (failure #{consecutive_errors}): {e} "
                    f"— recovering in {backoff}s",
                    "ERROR",
                )
                dashboard.set_cycle_stage(
                    "error", f"Cycle #{cycle} failed — recovering in {backoff}s"
                )
                if consecutive_errors >= 5:
                    dashboard.stats.log_activity(
                        "5+ consecutive cycle failures — check data source and logs",
                        "WARNING",
                    )
                for _ in range(backoff):
                    await asyncio.sleep(1)
                    await refresh_dashboard()

    except KeyboardInterrupt:
        dashboard.stats.log_activity("Shutdown requested", "WARNING")
        await refresh_dashboard()

    finally:
        position_management_task.cancel()
        try:
            await position_management_task
        except asyncio.CancelledError:
            pass
        if history_ingestion_scheduler is not None:
            await history_ingestion_scheduler.stop()
        if discovery_scheduler is not None:
            await discovery_scheduler.stop()
        if validation_scheduler is not None:
            await validation_scheduler.stop()
        if scalp_training_scheduler is not None:
            await scalp_training_scheduler.stop()
        if listen_task is not None:
            listen_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass
        if web_task is not None:
            await web_server.shutdown()
            web_task.cancel()
            try:
                await web_task
            except asyncio.CancelledError:
                pass
        if sector_task is not None:
            sector_task.cancel()
            try:
                await sector_task
            except asyncio.CancelledError:
                pass
        if scalping_task is not None:
            scalping_task.cancel()
            try:
                await scalping_task
            except asyncio.CancelledError:
                pass
        await market_manager.stop()
        # Show final stats
        stats = paper_engine.get_stats()
        cost_summary = get_cost_tracker().daily_summary("NSE")
        console.print("\n[yellow]Trading stopped[/]")
        console.print(
            f"[dim]Final balance: Rs.{stats['balance']:,.2f} | "
            f"P&L: Rs.{stats['total_pnl']:+,.2f} | "
            f"Win rate: {stats['win_rate']:.1f}%[/]"
        )
        console.print(
            f"[dim]LLM spend today: {cost_summary['calls']} calls | "
            f"{cost_summary['total_tokens']:,} tokens | "
            f"${cost_summary['total_cost_usd']:.4f} (paid-tier equiv)[/]"
        )

        # Telegram shutdown + P&L summary (best-effort)
        if notifier.enabled:
            try:
                await notifier.send_pnl_summary(
                    balance=stats["balance"],
                    realized_pnl=stats["total_pnl"],
                    unrealized_pnl=0.0,
                    total_trades=stats.get("total_trades", 0),
                    win_rate=stats["win_rate"],
                )
                await notifier.send_shutdown_message(reason="Session ended")
            except Exception as e:
                logger.warning("Telegram shutdown notification failed: %s", e)


def _acquire_single_instance_lock() -> Any:
    """Refuse to start a second instance.

    A duplicate process doesn't just race for the web UI port (the failure mode this
    used to be caught by) — now that paper wallet/signal history/daily risk state all
    live in Postgres, two independent processes would each open their own connection
    pool against the same database and silently split writes between them. This has
    happened for real multiple times this session, so it's no longer just a cosmetic
    "port already in use" log line — it's a data-integrity risk.

    Uses an OS-level file lock (not a PID file): the OS releases it automatically even
    if the process is killed or crashes, so there's no stale-lock file to clean up by
    hand — unlike a PID file, which can wrongly claim a slot forever if the process that
    wrote it never got the chance to delete it on exit.
    """
    lock_path = RUN_ROOT / "nse" / "run_live_trading.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            "ERROR: another instance of run_live_trading.py is already running "
            f"(lock held on {lock_path}). Stop it first — a second instance would race "
            "for the web UI port and split Postgres writes between two processes.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    return lock_file  # keep this referenced for the life of the process — closing releases the lock


def main():
    """Main entry point."""
    settings = get_settings()
    runtime_id = str(getattr(settings, "runtime_id", "nse"))
    log_path = configure_runtime_logging(
        None,
        market="NSE",
        provider="DHAN",
        runtime_id=runtime_id,
    )
    logger.info("Runtime logging initialized: %s", log_path)

    lock_file = _acquire_single_instance_lock()  # noqa: F841 — held for process lifetime

    try:
        asyncio.run(run_live_trading())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception("Fatal runtime error")
        console.print(f"[red]Error: {e}[/]")
        raise
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
