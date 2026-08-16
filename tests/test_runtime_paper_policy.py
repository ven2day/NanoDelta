from __future__ import annotations

import pytest

from nanodelta.runtime import paper_policy


def configure_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANODELTA_PAPER_EQUITY_INR", "3000000")
    monkeypatch.setenv("NANODELTA_PAPER_RISK_PER_TRADE_INR", "2000")
    monkeypatch.setenv("NANODELTA_PAPER_MAX_POSITIONS", "300")
    monkeypatch.setenv("NANODELTA_PAPER_MAX_SECTOR_POSITIONS", "30")


def test_allocation_policy_derives_risk_fraction_from_equity_and_rupee_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_required(monkeypatch)

    policy = paper_policy.build_allocation_policy()

    assert policy.equity == 3_000_000
    assert policy.risk_fraction_per_trade == pytest.approx(2000 / 3_000_000)
    assert policy.max_positions == 300
    assert policy.max_sector_positions == 30
    # Notional caps default to full equity when not overridden -- a paper-only
    # backstop, not the primary control (that's the risk-per-trade fraction).
    assert policy.max_order_notional == 3_000_000
    assert policy.max_total_new_notional == 3_000_000


def test_allocation_policy_honors_explicit_notional_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_required(monkeypatch)
    monkeypatch.setenv("NANODELTA_PAPER_MAX_ORDER_NOTIONAL_INR", "500000")
    monkeypatch.setenv("NANODELTA_PAPER_MAX_TOTAL_NEW_NOTIONAL_INR", "1000000")

    policy = paper_policy.build_allocation_policy()

    assert policy.max_order_notional == 500_000
    assert policy.max_total_new_notional == 1_000_000


def test_allocation_policy_requires_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_required(monkeypatch)
    monkeypatch.delenv("NANODELTA_PAPER_EQUITY_INR", raising=False)

    with pytest.raises(RuntimeError, match="NANODELTA_PAPER_EQUITY_INR is required"):
        paper_policy.build_allocation_policy()


def test_allocation_policy_rejects_non_numeric_value(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_required(monkeypatch)
    monkeypatch.setenv("NANODELTA_PAPER_EQUITY_INR", "thirty-lakh")

    with pytest.raises(RuntimeError, match="must be numeric"):
        paper_policy.build_allocation_policy()


def test_risk_limits_default_daily_loss_to_five_percent_of_equity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_required(monkeypatch)

    limits = paper_policy.build_risk_limits()

    assert limits.max_daily_loss == pytest.approx(150_000)
    assert limits.max_open_positions == 300
    assert limits.max_snapshot_age_seconds == 30
    assert limits.max_order_notional == 3_000_000
    assert limits.max_position_notional == 3_000_000
    assert limits.max_market_gross_exposure == 3_000_000
    assert limits.max_total_gross_exposure == 3_000_000


def test_risk_limits_honor_explicit_daily_loss_override(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_required(monkeypatch)
    monkeypatch.setenv("NANODELTA_PAPER_MAX_DAILY_LOSS_INR", "75000")

    limits = paper_policy.build_risk_limits()

    assert limits.max_daily_loss == 75_000
