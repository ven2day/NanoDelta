"""Deterministic NSE strategy research over settled, provider-backed candles.

The fixed technical plugins have no fitted parameters.  Their walk-forward test is
therefore a sequence of chronological, disjoint test folds after indicator warm-up;
signals use a snapshot at ``t`` and only the next settled close for the outcome.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum

from nanodelta.contracts import AdvisoryAction, Provider, stable_id, utc
from nanodelta.strategies import (
    StrategyContext,
    StrategyPlugin,
    TechnicalCandle,
    ValidationMetrics,
    ValidationPolicy,
    ValidationResult,
    materialize_technical_features,
    validate_strategy,
)

REQUIRED_NSE_TIMEFRAMES = ("5m", "15m", "30m", "1h")
_MINIMUM_BARS_PER_SESSION = {"5m": 75, "15m": 25, "30m": 12, "1h": 6}


class ResearchState(StrEnum):
    RESEARCH = "RESEARCH"
    FAILED = "FAILED"


@dataclass(frozen=True)
class NseCostModel:
    """Explicit round-trip assumptions, expressed in basis points."""

    brokerage_bps: float
    taxes_and_fees_bps: float
    slippage_bps: float

    def __post_init__(self) -> None:
        values = (self.brokerage_bps, self.taxes_and_fees_bps, self.slippage_bps)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("cost assumptions must be finite and non-negative")

    @property
    def round_trip_fraction(self) -> float:
        return (self.brokerage_bps + self.taxes_and_fees_bps + self.slippage_bps) / 10_000


@dataclass(frozen=True)
class NseValidationConfig:
    minimum_history_days: int = 730
    required_timeframes: tuple[str, ...] = REQUIRED_NSE_TIMEFRAMES
    walk_forward_windows: int = 5
    minimum_session_coverage: float = 0.8
    maximum_last_bar_age: timedelta = timedelta(days=4)
    tested_hypotheses: int = 3
    cost_model: NseCostModel = NseCostModel(3.0, 7.0, 5.0)
    policy: ValidationPolicy = field(
        default_factory=lambda: ValidationPolicy(minimum_walk_forward_windows=5)
    )

    def __post_init__(self) -> None:
        if self.minimum_history_days < 730:
            raise ValueError("NSE credentialed validation requires at least 730 history days")
        if self.walk_forward_windows < 2 or self.tested_hypotheses < 1:
            raise ValueError("walk-forward windows and hypotheses are invalid")
        if not 0.8 <= self.minimum_session_coverage <= 1:
            raise ValueError("minimum_session_coverage must be in [0.8, 1]")
        if (
            len(self.required_timeframes) != len(REQUIRED_NSE_TIMEFRAMES)
            or set(self.required_timeframes) != set(REQUIRED_NSE_TIMEFRAMES)
        ):
            raise ValueError("NSE readiness must cover 5m, 15m, 30m, and 1h")
        if self.maximum_last_bar_age <= timedelta(0):
            raise ValueError("maximum_last_bar_age must be positive")


@dataclass(frozen=True)
class SettledCandle:
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    provider: Provider = Provider.DHAN

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.timeframe.strip():
            raise ValueError("candle symbol and timeframe are required")
        utc(self.open_time, "open_time")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("candle values must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise ValueError("candle prices and volume are invalid")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("candle OHLC is inconsistent")


@dataclass(frozen=True)
class NseReadinessEvidence:
    readiness_id: str
    campaign_id: str
    symbol: str
    timeframe: str
    first_open: datetime | None
    last_open: datetime | None
    settled_count: int
    minimum_settled_count: int
    history_days: int
    ready: bool
    reasons: tuple[str, ...]
    source_fingerprint: str


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    started_at: datetime | None
    ended_at: datetime | None
    trade_count: int
    gross_expectancy: float
    net_expectancy: float
    profitable: bool


@dataclass(frozen=True)
class NseStrategyEvidence:
    evidence_id: str
    campaign_id: str
    validation: ValidationResult
    state: ResearchState
    data_fingerprint: str
    windows: tuple[WalkForwardWindow, ...]
    cost_model: NseCostModel
    stressed_net_expectancy: float


@dataclass(frozen=True)
class NseValidationCampaign:
    campaign_id: str
    evaluated_at: datetime
    requested_start: datetime
    requested_end: datetime
    symbols: tuple[str, ...]
    config: NseValidationConfig
    source_provider: Provider = Provider.DHAN

    @classmethod
    def create(
        cls,
        *,
        evaluated_at: datetime,
        symbols: Sequence[str],
        config: NseValidationConfig,
    ) -> NseValidationCampaign:
        evaluated_at = utc(evaluated_at, "evaluated_at")
        normalized = tuple(sorted({value.strip().upper() for value in symbols if value.strip()}))
        if not normalized:
            raise ValueError("validation campaign requires at least one NSE symbol")
        requested_start = evaluated_at - timedelta(days=config.minimum_history_days)
        campaign_id = stable_id(
            "nse-validation",
            evaluated_at.isoformat(),
            normalized,
            config,
            Provider.DHAN.value,
        )
        return cls(
            campaign_id,
            evaluated_at,
            requested_start,
            evaluated_at,
            normalized,
            config,
        )


def _fingerprint(candles: Sequence[SettledCandle]) -> str:
    digest = hashlib.sha256()
    for candle in sorted(candles, key=lambda row: (row.symbol, row.timeframe, row.open_time)):
        payload = (
            candle.symbol,
            candle.timeframe,
            utc(candle.open_time, "open_time").isoformat(),
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            candle.provider.value,
        )
        digest.update(json.dumps(payload, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def evaluate_nse_readiness(
    campaign: NseValidationCampaign,
    candles: Sequence[SettledCandle],
) -> tuple[NseReadinessEvidence, ...]:
    grouped: dict[tuple[str, str], list[SettledCandle]] = {}
    for candle in candles:
        if candle.symbol in campaign.symbols and candle.open_time <= campaign.requested_end:
            grouped.setdefault((candle.symbol, candle.timeframe), []).append(candle)
    result: list[NseReadinessEvidence] = []
    expected_sessions = _weekday_count(
        campaign.requested_start.date(), campaign.requested_end.date()
    )
    for symbol in campaign.symbols:
        for timeframe in campaign.config.required_timeframes:
            rows = sorted(grouped.get((symbol, timeframe), ()), key=lambda row: row.open_time)
            coverage_rows = [
                row for row in rows if row.open_time >= campaign.requested_start
            ]
            first = rows[0].open_time if rows else None
            last = rows[-1].open_time if rows else None
            reasons: list[str] = []
            if not rows:
                reasons.append("NO_SETTLED_DHAN_CANDLES")
            if any(row.provider is not Provider.DHAN for row in rows):
                reasons.append("NON_DHAN_SOURCE")
            if first is None or first > campaign.requested_start:
                reasons.append("LESS_THAN_TWO_YEARS_HISTORY")
            if last is None or campaign.requested_end - last > campaign.config.maximum_last_bar_age:
                reasons.append("LATEST_CANDLE_STALE")
            minimum_count = math.floor(
                expected_sessions
                * _MINIMUM_BARS_PER_SESSION[timeframe]
                * campaign.config.minimum_session_coverage
            )
            if len(coverage_rows) < minimum_count:
                reasons.append("INSUFFICIENT_SETTLED_CANDLE_COVERAGE")
            history_days = 0 if first is None or last is None else (last - first).days
            fingerprint = _fingerprint(rows)
            result.append(
                NseReadinessEvidence(
                    stable_id(campaign.campaign_id, symbol, timeframe, fingerprint),
                    campaign.campaign_id,
                    symbol,
                    timeframe,
                    first,
                    last,
                    len(coverage_rows),
                    minimum_count,
                    history_days,
                    not reasons,
                    tuple(reasons),
                    fingerprint,
                )
            )
    return tuple(result)


def _weekday_count(start: date, end: date) -> int:
    current = start
    count = 0
    while current <= end:
        count += current.weekday() < 5
        current += timedelta(days=1)
    return count


def _one_sided_sign_test(wins: int, sample: int) -> float:
    if sample == 0:
        return 1.0
    tail = sum(math.comb(sample, index) for index in range(wins, sample + 1))
    return min(1.0, float(tail) / float(2**sample))


def _maximum_drawdown(returns: Sequence[float]) -> float:
    equity = peak = 1.0
    maximum = 0.0
    for value in returns:
        equity *= max(0.0, 1 + value)
        peak = max(peak, equity)
        maximum = max(maximum, 0.0 if peak == 0 else (peak - equity) / peak)
    return maximum


def _window(
    index: int,
    rows: Sequence[tuple[datetime, str, float]],
    cost: float,
) -> WalkForwardWindow:
    returns = [value for _, _, value in rows]
    count = len(returns)
    gross = sum(returns) / count if count else 0.0
    net = gross - cost if count else 0.0
    return WalkForwardWindow(
        index,
        rows[0][0] if rows else None,
        rows[-1][0] if rows else None,
        count,
        gross,
        net,
        count > 0 and net > 0,
    )


def evaluate_nse_strategy(
    campaign: NseValidationCampaign,
    plugin: StrategyPlugin,
    candles: Sequence[SettledCandle],
    readiness: Sequence[NseReadinessEvidence],
) -> NseStrategyEvidence:
    """Evaluate one exact plugin identity without fitting or future feature access."""
    identity = plugin.definition.identity
    if identity.market.value != "nse":
        raise ValueError("NSE validation accepts only NSE strategy identities")
    series: dict[str, list[SettledCandle]] = {}
    for candle in candles:
        if (
            candle.symbol in campaign.symbols
            and candle.timeframe == identity.timeframe
            and candle.provider is Provider.DHAN
            and candle.open_time <= campaign.requested_end
        ):
            series.setdefault(candle.symbol, []).append(candle)

    trades: list[tuple[datetime, str, float]] = []
    used_candles: list[SettledCandle] = []
    for symbol, raw_rows in sorted(series.items()):
        rows = sorted(raw_rows, key=lambda row: row.open_time)
        used_candles.extend(rows)
        snapshots = materialize_technical_features(
            [
                TechnicalCandle(
                    row.open_time,
                    row.open,
                    row.high,
                    row.low,
                    row.close,
                    row.volume,
                )
                for row in rows
            ]
        )
        closes = {row.open_time: row.close for row in rows}
        ordered_times = [row.open_time for row in rows]
        next_close = {
            opened: closes[ordered_times[index + 1]]
            for index, opened in enumerate(ordered_times[:-1])
        }
        for snapshot in snapshots:
            if not campaign.requested_start <= snapshot.event_time <= campaign.requested_end:
                continue
            following = next_close.get(snapshot.event_time)
            if following is None:
                continue
            context = StrategyContext(
                market=identity.market,
                symbol=symbol,
                sector=None,
                timeframe=identity.timeframe,
                trade_horizon=identity.trade_horizon,
                feature_set_version=identity.feature_set_version,
                event_time=snapshot.event_time,
                gold_snapshot_ids=(stable_id(symbol, identity.timeframe, snapshot.event_time),),
                features=snapshot.values,
            )
            compatible, _ = plugin.compatibility(context)
            signal = plugin.generate(context) if compatible else None
            if signal is None:
                continue
            direction = 1.0 if signal.action is AdvisoryAction.BUY else -1.0
            gross = direction * (following - signal.reference_price) / signal.reference_price
            trades.append((snapshot.event_time, symbol, gross))

    ordered = sorted(trades, key=lambda row: (row[0], row[1]))
    window_rows: list[list[tuple[datetime, str, float]]] = [
        [] for _ in range(campaign.config.walk_forward_windows)
    ]
    for index, row in enumerate(ordered):
        slot = min(
            len(window_rows) - 1,
            index * len(window_rows) // max(1, len(ordered)),
        )
        window_rows[slot].append(row)
    cost = campaign.config.cost_model.round_trip_fraction
    windows = tuple(_window(index + 1, rows, cost) for index, rows in enumerate(window_rows))
    gross_returns = [row[2] for row in ordered]
    net_returns = [value - cost for value in gross_returns]
    count = len(gross_returns)
    metrics = ValidationMetrics(
        trade_count=count,
        walk_forward_windows=len(windows),
        profitable_windows=sum(window.profitable for window in windows),
        gross_expectancy=sum(gross_returns) / count if count else 0.0,
        estimated_cost_per_trade=cost,
        maximum_drawdown=_maximum_drawdown(net_returns),
        p_value=_one_sided_sign_test(sum(value > cost for value in gross_returns), count),
        tested_hypotheses=campaign.config.tested_hypotheses,
    )
    validation = validate_strategy(
        identity,
        metrics,
        campaign.config.policy,
        evaluated_at=campaign.evaluated_at,
    )
    expected_readiness = {
        (symbol, timeframe)
        for symbol in campaign.symbols
        for timeframe in campaign.config.required_timeframes
    }
    observed_readiness = {(item.symbol, item.timeframe) for item in readiness}
    missing_readiness = (
        ("MISSING_READINESS_EVIDENCE",) if observed_readiness != expected_readiness else ()
    )
    readiness_failures = tuple(
        sorted(
            {
                reason
                for item in readiness
                if not item.ready
                for reason in ("DATA_NOT_READY", *missing_readiness, *item.reasons)
            }
            | set(missing_readiness)
        )
    )
    if readiness_failures:
        validation = replace(
            validation,
            passed=False,
            rejection_reasons=tuple(
                dict.fromkeys((*validation.rejection_reasons, *readiness_failures))
            ),
        )
    state = ResearchState.RESEARCH if validation.passed else ResearchState.FAILED
    data_fingerprint = _fingerprint(used_candles)
    evidence_id = stable_id(
        campaign.campaign_id,
        validation.validation_run_id,
        data_fingerprint,
        windows,
        campaign.config.cost_model,
    )
    gross_expectancy = metrics.gross_expectancy
    stressed = gross_expectancy - cost * 1.5
    return NseStrategyEvidence(
        evidence_id,
        campaign.campaign_id,
        validation,
        state,
        data_fingerprint,
        windows,
        campaign.config.cost_model,
        stressed,
    )
