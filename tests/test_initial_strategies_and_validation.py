from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from nanodelta.contracts import Market
from nanodelta.strategies import (
    ClosedBar,
    EmaRsiMomentumStrategy,
    EmaRsiParameters,
    StrategyContext,
    StrategyRegistry,
    StrategySpec,
    SuperTrendAdxStrategy,
    ValidationPolicy,
    VwapPullbackParameters,
    VwapPullbackStrategy,
    validate_strategy,
)
from nanodelta.strategies.artifacts import (
    PromotionStage,
    build_artifact,
    promote_to_paper,
    write_artifact,
)
from nanodelta.strategies.backtest import BacktestPolicy, replay_strategy

START = datetime(2025, 1, 1, tzinfo=UTC)


def bars(closes: list[float], *, volumes: list[float] | None = None) -> tuple[ClosedBar, ...]:
    result = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        opening = previous
        result.append(
            ClosedBar(
                START + timedelta(minutes=15 * index),
                opening,
                max(opening, close) + 0.4,
                min(opening, close) - 0.4,
                close,
                volumes[index] if volumes else 1000 + index,
            )
        )
    return tuple(result)


def context(plugin: object, history: tuple[ClosedBar, ...]) -> StrategyContext:
    definition = plugin.definition  # type: ignore[attr-defined]
    identity = definition.identity
    return StrategyContext(
        identity.market,
        "TEST",
        None,
        identity.timeframe,
        identity.trade_horizon,
        identity.feature_set_version,
        history[-1].open_time + timedelta(minutes=15),
        ("fixture",),
        {"close": history[-1].close},
        closed_bars=history,
    )


def test_closed_bar_context_rejects_future_and_nonchronological_history() -> None:
    history = bars([100, 101, 102])
    with pytest.raises(ValueError, match="decision or future"):
        StrategyContext(
            Market.NSE,
            "TEST",
            None,
            "15m",
            "intraday",
            1,
            history[-1].open_time,
            ("fixture",),
            {"close": 102},
            closed_bars=history,
        )
    with pytest.raises(ValueError, match="chronological"):
        StrategyContext(
            Market.NSE,
            "TEST",
            None,
            "15m",
            "intraday",
            1,
            history[-1].open_time + timedelta(hours=1),
            ("fixture",),
            {"close": 102},
            closed_bars=(history[1], history[0]),
        )


def test_market_compatibility_does_not_force_vwap_onto_forex() -> None:
    with pytest.raises(ValueError, match="Forex is not supported"):
        VwapPullbackStrategy(StrategySpec(Market.FOREX, "15m"))
    # Price-only momentum/trend strategies explicitly support separate market identities.
    assert (
        EmaRsiMomentumStrategy(StrategySpec(Market.FOREX, "15m")).definition.identity.market
        is Market.FOREX
    )
    assert (
        SuperTrendAdxStrategy(StrategySpec(Market.CRYPTO, "5m")).definition.identity.market
        is Market.CRYPTO
    )


def test_ema_rsi_signal_uses_only_closed_history_and_is_deterministic() -> None:
    plugin = EmaRsiMomentumStrategy(
        StrategySpec(Market.NSE, "15m"),
        EmaRsiParameters(
            fast_ema=3, slow_ema=6, rsi_period=3, buy_rsi=50, sell_rsi=50, atr_period=3
        ),
    )
    history = bars([100, 99, 98, 97, 96, 95, 94, 101])
    first = plugin.generate(context(plugin, history))
    second = plugin.generate(context(plugin, history))
    assert first == second
    assert first is not None
    assert first.action.value == "BUY"


def test_vwap_pullback_requires_real_volume_and_can_generate_reclaim() -> None:
    plugin = VwapPullbackStrategy(
        StrategySpec(Market.NSE, "15m"), VwapPullbackParameters(ema_period=3, atr_period=3)
    )
    history = bars([100, 101, 102, 99, 104], volumes=[100, 100, 100, 500, 1000])
    signal = plugin.generate(context(plugin, history))
    assert signal is not None and signal.action.value == "BUY"
    zero_volume = bars([100, 101, 102, 99, 104], volumes=[0, 0, 0, 0, 0])
    assert plugin.generate(context(plugin, zero_volume)) is None


def fixture_series(length: int = 280) -> tuple[ClosedBar, ...]:
    # Deterministic oscillating data is intentionally not constructed to guarantee profit.
    closes = [100 + 0.015 * index + 2.5 * math.sin(index / 5) for index in range(length)]
    return bars(closes)


def test_offline_replay_is_next_bar_cost_aware_and_does_not_auto_approve(tmp_path) -> None:
    plugin = EmaRsiMomentumStrategy(
        StrategySpec(Market.NSE, "15m"),
        EmaRsiParameters(
            fast_ema=3, slow_ema=7, rsi_period=4, buy_rsi=50, sell_rsi=50, atr_period=4
        ),
    )
    backtest = replay_strategy(
        plugin,
        fixture_series(),
        BacktestPolicy(warmup_bars=20, maximum_holding_bars=6, walk_forward_windows=5),
        tested_hypotheses=3,
    )
    assert all(trade.entry_index == trade.signal_index + 1 for trade in backtest.trades)
    assert all(trade.cost_r > 0 and trade.net_r < trade.gross_r for trade in backtest.trades)
    validation = validate_strategy(
        plugin.definition.identity,
        backtest.metrics,
        ValidationPolicy(),
        evaluated_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    registry = StrategyRegistry()
    registry.register(plugin.definition)
    registry.record_validation(validation)
    artifact = build_artifact(
        validation,
        backtest,
        source_data_id="synthetic-oscillation-v1",
        code_revision="test-revision",
    )
    assert artifact.promotion_stage is PromotionStage.RESEARCH
    path = write_artifact(artifact, tmp_path)
    assert json.loads(path.read_text())["content_sha256"] == artifact.content_sha256
    assert write_artifact(artifact, tmp_path) == path
    with pytest.raises(PermissionError):
        promote_to_paper(
            registry,
            identity=plugin.definition.identity,
            validation_run_id=validation.validation_run_id,
            approved_at=datetime(2026, 8, 15, tzinfo=UTC),
            expires_at=datetime(2026, 9, 15, tzinfo=UTC),
            approved_by="committee",
            reason="manual review",
        )
    assert not validation.passed
