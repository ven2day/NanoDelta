from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from nanodelta.contracts import AdvisoryAction, FeatureRecord, Market, Provider
from nanodelta.decisions import InMemoryDecisionLedger
from nanodelta.orchestration import AllocationPolicy
from nanodelta.paper import ExecutionPolicy, PaperExecutionEngine, PositionState
from nanodelta.paper.lifecycle import MemoryLifecycleStore, PaperPositionLifecycle
from nanodelta.risk import PortfolioPosition, PortfolioSnapshot, RiskEngine, RiskLimits
from nanodelta.runtime.paper_decision import PaperDecisionResult, PaperDecisionService
from nanodelta.runtime.paper_session import (
    ContinuousNsePaperSession,
    MemoryPaperSessionStore,
    PaperSessionClaimState,
)
from nanodelta.runtime.realtime import QuoteEvent, RealtimeMarketCycle, SettledCandle
from nanodelta.strategies import (
    StrategyApproval,
    StrategyContext,
    StrategyDefinition,
    StrategyIdentity,
    StrategyRegistry,
    StrategyRuntimeCatalog,
    StrategySignal,
    SymbolRegimeLimits,
    TradeabilityLimits,
    ValidationMetrics,
    ValidationPolicy,
    validate_strategy,
)

ENTRY_AT = datetime(2026, 8, 17, 4, tzinfo=UTC)
EXIT_AT = datetime(2026, 8, 17, 10, 30, tzinfo=UTC)
ACCOUNT = "paper-nse"


def test_continuous_session_migration_has_leases_health_counts_and_bounded_nse_scope() -> None:
    sql = (
        Path(__file__).parents[1] / "migrations" / "0017_nse_continuous_paper_session.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS control.paper_session_cycles" in sql
    assert "CHECK (market = 'nse')" in sql
    assert "locked_until timestamptz" in sql
    assert "attempt_count integer" in sql
    assert "decision_cycle_id text" in sql
    assert "order_count integer" in sql
    assert "exit_count integer" in sql


def test_decision_failure_is_measured_but_does_not_masquerade_as_provider_failure() -> None:
    feature = _feature("decision-error", ENTRY_AT, 100)

    class Pipeline:
        def ingest(self, **kwargs: object) -> object:
            del kwargs
            return SimpleNamespace(
                canonical=SimpleNamespace(symbol="RELIANCE", timeframe="1m"), silver_created=True
            )

        def build_gold(self, candles: list[object]) -> list[FeatureRecord]:
            del candles
            return [feature]

    class Metrics:
        def __init__(self) -> None:
            self.results: list[str] = []

        def observe_decision(self, market: Market, result: str, seconds: float) -> None:
            del market, seconds
            self.results.append(result)

    cycle = object.__new__(RealtimeMarketCycle)
    cycle.pipeline = Pipeline()  # type: ignore[assignment]
    cycle.on_features = lambda _features: (_ for _ in ()).throw(RuntimeError("database down"))
    metrics = Metrics()
    cycle.metrics = metrics  # type: ignore[assignment]
    cycle.market = Market.NSE
    cycle.clock = lambda: ENTRY_AT
    cycle._previous_candles = {  # type: ignore[assignment]
        ("RELIANCE", "1m"): SimpleNamespace(symbol="RELIANCE", timeframe="1m")
    }
    quote = QuoteEvent(Market.NSE, Provider.TRUEDATA, "RELIANCE", ENTRY_AT, 100)
    candle = SettledCandle("RELIANCE", "1m", ENTRY_AT, 100, 101, 99, 100, 10)

    cycle._persist_settled(quote, candle)

    assert metrics.results == ["error"]


def test_first_settled_bar_after_restart_restores_prior_silver_bar_for_gold() -> None:
    previous = SimpleNamespace(symbol="RELIANCE", timeframe="1m", marker="previous")
    current = SimpleNamespace(symbol="RELIANCE", timeframe="1m", marker="current")

    class Pipeline:
        def __init__(self) -> None:
            self.gold_inputs: list[list[object]] = []

        def ingest(self, **kwargs: object) -> object:
            del kwargs
            return SimpleNamespace(canonical=current, silver_created=True)

        def build_gold(self, candles: list[object]) -> list[FeatureRecord]:
            self.gold_inputs.append(candles)
            return []

    pipeline = Pipeline()
    cycle = object.__new__(RealtimeMarketCycle)
    cycle.pipeline = pipeline  # type: ignore[assignment]
    cycle.on_features = None
    cycle.metrics = None
    cycle.market = Market.NSE
    cycle.clock = lambda: ENTRY_AT
    cycle._previous_candles = {}
    cycle.previous_candle_loader = lambda _current: previous
    quote = QuoteEvent(Market.NSE, Provider.TRUEDATA, "RELIANCE", ENTRY_AT, 100)
    candle = SettledCandle("RELIANCE", "1m", ENTRY_AT, 100, 101, 99, 100, 10)

    cycle._persist_settled(quote, candle)

    assert pipeline.gold_inputs == [[previous, current]]


@dataclass
class FixedBuyStrategy:
    definition: StrategyDefinition

    def compatibility(self, context: StrategyContext) -> tuple[bool, str]:
        return ("close" in context.features, "COMPATIBLE")

    def generate(self, context: StrategyContext) -> StrategySignal:
        price = context.features["close"]
        return StrategySignal(AdvisoryAction.BUY, 0.9, price, price - 2, price + 3)


class InMemoryDecisionService(PaperDecisionService):
    def __init__(self, *, execution: PaperExecutionEngine, **kwargs: object) -> None:
        super().__init__(execution=execution, **kwargs)  # type: ignore[arg-type]
        self.execution = execution

    def _latest_marks(self, market: Market) -> dict[str, float]:
        del market
        return {}

    def _technical_context(self, feature: FeatureRecord, now: datetime) -> StrategyContext | None:
        del feature, now
        return None

    def _portfolio(
        self, market: Market, marks: dict[str, float], now: datetime
    ) -> PortfolioSnapshot:
        position = self.execution.position(market, ACCOUNT, "RELIANCE")
        positions = ()
        if position is not None and position.state is PositionState.OPEN:
            positions = (
                PortfolioPosition(
                    market,
                    ACCOUNT,
                    "RELIANCE",
                    position.signed_quantity,
                    marks["RELIANCE"],
                ),
            )
        return PortfolioSnapshot("snapshot", ACCOUNT, 100_000, 0, positions, now)


def _feature(record_id: str, at: datetime, close: float) -> FeatureRecord:
    return FeatureRecord(
        record_id,
        f"candle-{record_id}",
        Market.NSE,
        "RELIANCE",
        "1m",
        at,
        close,
        0.02,
        0.02,
        0.01,
        0.2,
    )


def _approved_strategy() -> tuple[StrategyRegistry, StrategyRuntimeCatalog]:
    identity = StrategyIdentity(Market.NSE, "fixed_buy", "1", "1m", "intraday", 1)
    strategy = FixedBuyStrategy(StrategyDefinition(identity, "test", (), "tests:FixedBuyStrategy"))
    registry = StrategyRegistry()
    registry.register(strategy.definition)
    validation = validate_strategy(
        identity,
        ValidationMetrics(100, 5, 4, 0.02, 0.002, 0.1, 0.001, 5),
        ValidationPolicy(),
        evaluated_at=ENTRY_AT - timedelta(days=2),
    )
    registry.record_validation(validation)
    registry.record_approval(
        StrategyApproval.create(
            identity=identity,
            validation_run_id=validation.validation_run_id,
            approved_at=ENTRY_AT - timedelta(days=1),
            expires_at=ENTRY_AT + timedelta(days=30),
            approved_by="test-committee",
            reason="test validation passed",
        )
    )
    catalog = StrategyRuntimeCatalog()
    catalog.register(strategy)
    return registry, catalog


def test_continuous_session_completes_buy_risk_fill_position_sell_exit_and_outcome() -> None:
    registry, catalog = _approved_strategy()
    ledger = InMemoryDecisionLedger()
    execution = PaperExecutionEngine(ExecutionPolicy(0, 0))
    risk = RiskEngine(RiskLimits(100_000, 100_000, 200_000, 200_000, 50_000, 10))
    lifecycle_store = MemoryLifecycleStore()
    lifecycle = PaperPositionLifecycle(
        store=lifecycle_store, execution=execution, risk=risk, ledger=ledger
    )
    service = InMemoryDecisionService(
        connect=lambda: None,
        registry=registry,
        catalog=catalog,
        ledger=ledger,
        risk=risk,
        execution=execution,
        allocation=AllocationPolicy(100_000, 0.01, 50_000, 50_000, 10, 10),
        account_id=ACCOUNT,
        equity=100_000,
        tradeability=TradeabilityLimits(1, 1, 1, 0.0001, 10.0, 10.0),
        symbol_regime=SymbolRegimeLimits(20.0, 35.0, 0.4, 1.2),
        lifecycle=lifecycle,
        entry_session_open=lambda _market, at: at < EXIT_AT,
    )
    now = [ENTRY_AT]
    durable_store = MemoryPaperSessionStore()
    session = ContinuousNsePaperSession(
        processor=service, store=durable_store, account_id=ACCOUNT, clock=lambda: now[0]
    )

    entered = session.process((_feature("gold-entry", ENTRY_AT, 100),))

    assert entered is not None and entered.decision is not None
    assert entered.decision.candidate_count == 1
    assert entered.decision.risk_decision_count == 1
    assert entered.decision.order_count == 1
    position = execution.position(Market.NSE, ACCOUNT, "RELIANCE")
    assert position is not None and position.state is PositionState.OPEN
    entry_order = next(iter(execution._receipts.values())).order
    assert entry_order.action is AdvisoryAction.BUY

    # A new process with the same durable store replays the settled Gold ID without
    # entering a second order. This is the restart boundary that used to be absent.
    restarted = ContinuousNsePaperSession(
        processor=service, store=durable_store, account_id=ACCOUNT, clock=lambda: now[0]
    )
    replay = restarted.process((_feature("gold-entry", ENTRY_AT, 100),))
    assert replay is not None and replay.state is PaperSessionClaimState.COMPLETED
    assert len(execution._receipts) == 1

    # Protective exits remain active after the entry session closes.
    now[0] = EXIT_AT
    exited = restarted.process((_feature("gold-exit", EXIT_AT, 104),))

    assert exited is not None and exited.decision is not None
    assert exited.decision.exit_count == 1
    closed = execution.position(Market.NSE, ACCOUNT, "RELIANCE")
    assert closed is not None and closed.state is PositionState.CLOSED
    assert {receipt.order.action for receipt in execution._receipts.values()} == {
        AdvisoryAction.BUY,
        AdvisoryAction.SELL,
    }
    assert len(lifecycle_store.outcomes) == 1
    assert next(iter(lifecycle_store.outcomes.values())).net_pnl > 0


class FailOnceProcessor:
    def __init__(self) -> None:
        self.evaluated: list[datetime] = []

    def process(
        self, features: tuple[FeatureRecord, ...], *, evaluated_at: datetime | None = None
    ) -> PaperDecisionResult:
        del features
        assert evaluated_at is not None
        self.evaluated.append(evaluated_at)
        if len(self.evaluated) == 1:
            raise RuntimeError("simulated crash")
        from nanodelta.orchestration.decision_pipeline import CycleMode

        return PaperDecisionResult(Market.NSE, "decision-cycle", CycleMode.NORMAL, 1, 1, 1, 1, 0)


def test_failed_cycle_reuses_original_evaluation_time_and_then_becomes_replay() -> None:
    processor = FailOnceProcessor()
    store = MemoryPaperSessionStore()
    now = [ENTRY_AT]
    session = ContinuousNsePaperSession(
        processor=processor, store=store, account_id=ACCOUNT, clock=lambda: now[0]
    )
    feature = (_feature("retry", ENTRY_AT, 100),)

    try:
        session.process(feature)
    except RuntimeError as exc:
        assert str(exc) == "simulated crash"
    else:
        raise AssertionError("first attempt must fail")

    now[0] += timedelta(minutes=10)
    completed = session.process(feature)
    replayed = session.process(feature)

    assert processor.evaluated == [ENTRY_AT, ENTRY_AT]
    assert completed is not None and completed.state is PaperSessionClaimState.CLAIMED
    assert replayed is not None and replayed.state is PaperSessionClaimState.COMPLETED
    assert session.health.failed == 1
    assert session.health.processed == 1
    assert session.health.replayed == 1


class RetryMemoryStore(MemoryPaperSessionStore):
    def __init__(self) -> None:
        super().__init__()
        self.pending: tuple[tuple[FeatureRecord, ...], ...] = ()

    def retryable_features(
        self, *, as_of: datetime, limit: int = 10
    ) -> tuple[tuple[FeatureRecord, ...], ...]:
        del as_of, limit
        result, self.pending = self.pending, ()
        return result


def test_next_settled_cycle_retries_prior_failed_gold_without_blocking_newer_gold() -> None:
    processor = FailOnceProcessor()
    store = RetryMemoryStore()
    now = [ENTRY_AT]
    session = ContinuousNsePaperSession(
        processor=processor, store=store, account_id=ACCOUNT, clock=lambda: now[0]
    )
    failed = (_feature("failed-gold", ENTRY_AT, 100),)

    try:
        session.process(failed)
    except RuntimeError:
        pass
    else:
        raise AssertionError("first attempt must fail")

    store.pending = (failed,)
    now[0] += timedelta(minutes=1)
    newer = (_feature("newer-gold", now[0], 101),)
    session.process(newer)

    assert processor.evaluated == [ENTRY_AT, now[0], ENTRY_AT]
    assert all(cycle.state == "COMPLETED" for cycle in store.cycles.values())
    assert session.health.processed == 2
