"""Environment-driven construction of paper trading allocation and risk policy.

Every market gets an equal, independent paper account of this size -- NSE,
Forex, and Crypto do not share capital. Values not given an explicit
environment override default to a generous multiple of equity, since these
are paper-only ceilings, not real capital constraints: order sizing is
already governed by NANODELTA_PAPER_RISK_PER_TRADE_INR against the stop
distance, so these caps are a backstop, not the primary control.
"""

from __future__ import annotations

import os

from nanodelta.orchestration.decision_pipeline import AllocationPolicy
from nanodelta.risk import RiskLimits


def _required(name: str) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for paper trading policy")
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc


def _optional(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def build_allocation_policy() -> AllocationPolicy:
    equity = _required("NANODELTA_PAPER_EQUITY_INR")
    risk_per_trade = _required("NANODELTA_PAPER_RISK_PER_TRADE_INR")
    max_positions = int(_required("NANODELTA_PAPER_MAX_POSITIONS"))
    max_sector_positions = int(_required("NANODELTA_PAPER_MAX_SECTOR_POSITIONS"))
    return AllocationPolicy(
        equity=equity,
        risk_fraction_per_trade=risk_per_trade / equity,
        max_order_notional=_optional("NANODELTA_PAPER_MAX_ORDER_NOTIONAL_INR", equity),
        max_total_new_notional=_optional("NANODELTA_PAPER_MAX_TOTAL_NEW_NOTIONAL_INR", equity),
        max_positions=max_positions,
        max_sector_positions=max_sector_positions,
    )


def build_risk_limits() -> RiskLimits:
    equity = _required("NANODELTA_PAPER_EQUITY_INR")
    max_positions = int(_required("NANODELTA_PAPER_MAX_POSITIONS"))
    return RiskLimits(
        max_order_notional=_optional("NANODELTA_PAPER_MAX_ORDER_NOTIONAL_INR", equity),
        max_position_notional=_optional("NANODELTA_PAPER_MAX_POSITION_NOTIONAL_INR", equity),
        max_market_gross_exposure=_optional(
            "NANODELTA_PAPER_MAX_MARKET_GROSS_EXPOSURE_INR", equity
        ),
        max_total_gross_exposure=_optional(
            "NANODELTA_PAPER_MAX_TOTAL_GROSS_EXPOSURE_INR", equity
        ),
        max_daily_loss=_optional("NANODELTA_PAPER_MAX_DAILY_LOSS_INR", equity * 0.05),
        max_open_positions=int(
            _optional("NANODELTA_PAPER_MAX_OPEN_POSITIONS", float(max_positions))
        ),
        max_snapshot_age_seconds=int(
            _optional("NANODELTA_PAPER_MAX_SNAPSHOT_AGE_SECONDS", 30)
        ),
    )
