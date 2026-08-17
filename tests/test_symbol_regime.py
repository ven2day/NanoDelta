from __future__ import annotations

import pytest

from nanodelta.strategies import SymbolRegimeLimits, evaluate_symbol_regime

LIMITS = SymbolRegimeLimits(
    adx_no_trend=20.0,
    adx_strong_trend=35.0,
    minimum_fit=0.4,
    maximum_fit=1.2,
    misaligned_penalty=0.7,
)


def features(
    adx: float,
    ema_9: float = 110,
    ema_21: float = 100,
    supertrend_direction: float = 1.0,
    volume_ratio_20: float = 1.0,
) -> dict[str, float]:
    return {
        "adx_14": adx,
        "ema_9": ema_9,
        "ema_21": ema_21,
        "supertrend_direction": supertrend_direction,
        "volume_ratio_20": volume_ratio_20,
    }


def test_limits_reject_invalid_ranges() -> None:
    with pytest.raises(ValueError, match="adx_strong_trend"):
        SymbolRegimeLimits(35.0, 20.0, 0.4, 1.2)
    with pytest.raises(ValueError, match="maximum_fit"):
        SymbolRegimeLimits(20.0, 35.0, 1.2, 0.4)
    with pytest.raises(ValueError, match="misaligned_penalty"):
        SymbolRegimeLimits(20.0, 35.0, 0.4, 1.2, misaligned_penalty=0.0)


def test_no_trend_is_scored_at_minimum_fit() -> None:
    fit, label = evaluate_symbol_regime(features(adx=10.0), LIMITS)
    assert fit == LIMITS.minimum_fit
    assert label == "NO_TREND"


def test_strong_trend_aligned_is_scored_at_maximum_fit() -> None:
    fit, label = evaluate_symbol_regime(features(adx=40.0), LIMITS)
    assert fit == LIMITS.maximum_fit
    assert label == "STRONG_TREND"


def test_developing_trend_scales_linearly_between_bounds() -> None:
    fit, label = evaluate_symbol_regime(features(adx=27.5), LIMITS)
    assert fit == (LIMITS.minimum_fit + LIMITS.maximum_fit) / 2
    assert label == "DEVELOPING_TREND"


def test_misaligned_indicators_apply_penalty() -> None:
    aligned_fit, _ = evaluate_symbol_regime(
        features(adx=40.0, ema_9=110, ema_21=100, supertrend_direction=1.0), LIMITS
    )
    misaligned_fit, label = evaluate_symbol_regime(
        features(adx=40.0, ema_9=110, ema_21=100, supertrend_direction=-1.0), LIMITS
    )
    assert misaligned_fit == aligned_fit * LIMITS.misaligned_penalty
    assert label == "STRONG_TREND_MISALIGNED"


def test_bearish_alignment_is_not_penalized() -> None:
    fit, label = evaluate_symbol_regime(
        features(adx=40.0, ema_9=90, ema_21=100, supertrend_direction=-1.0), LIMITS
    )
    assert fit == LIMITS.maximum_fit
    assert label == "STRONG_TREND"


def test_low_volume_participation_applies_penalty() -> None:
    full_volume_fit, _ = evaluate_symbol_regime(features(adx=40.0, volume_ratio_20=1.0), LIMITS)
    low_volume_fit, label = evaluate_symbol_regime(
        features(adx=40.0, volume_ratio_20=0.5), LIMITS
    )
    assert low_volume_fit == full_volume_fit * LIMITS.low_volume_penalty
    assert label == "STRONG_TREND_LOW_VOLUME"


def test_normal_volume_participation_is_not_penalized() -> None:
    fit, label = evaluate_symbol_regime(features(adx=40.0, volume_ratio_20=0.8), LIMITS)
    assert fit == LIMITS.maximum_fit
    assert label == "STRONG_TREND"


def test_mtf_alignment_aligned_boosts_fit() -> None:
    from nanodelta.strategies import evaluate_mtf_alignment

    assert evaluate_mtf_alignment(True) == 1.15


def test_mtf_alignment_misaligned_discounts_fit() -> None:
    from nanodelta.strategies import evaluate_mtf_alignment

    assert evaluate_mtf_alignment(False) == 0.75


def test_mtf_alignment_unknown_stays_neutral() -> None:
    from nanodelta.strategies import evaluate_mtf_alignment

    assert evaluate_mtf_alignment(None) == 1.0
