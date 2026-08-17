from __future__ import annotations

from nanodelta.runtime.regime import classify_index_regime, classify_risk_off


def values(
    adx: float, close: float = 100.0, atr: float = 0.5, bullish: bool = True
) -> dict[str, float]:
    return {
        "adx_14": adx,
        "close": close,
        "atr_14": atr,
        "ema_9": close + 1 if bullish else close - 1,
        "ema_21": close,
    }


def test_low_adx_is_ranging() -> None:
    breadth = classify_index_regime(values(adx=10.0))
    assert breadth.label == "RANGING"
    assert breadth.fit < 1.0


def test_high_adx_bullish_is_trending_up() -> None:
    breadth = classify_index_regime(values(adx=30.0, bullish=True))
    assert breadth.label == "TRENDING_UP"
    assert breadth.fit > 1.0


def test_high_adx_bearish_is_trending_down() -> None:
    breadth = classify_index_regime(values(adx=30.0, bullish=False))
    assert breadth.label == "TRENDING_DOWN"
    assert breadth.fit > 1.0


def test_high_atr_pct_overrides_trend_as_volatile() -> None:
    breadth = classify_index_regime(values(adx=30.0, close=100.0, atr=5.0, bullish=True))
    assert breadth.label == "VOLATILE"
    assert breadth.fit < 1.0


def test_risk_off_thresholds() -> None:
    assert classify_risk_off(12.0).label == "NORMAL"
    assert classify_risk_off(22.0).label == "ELEVATED_VOLATILITY"
    assert classify_risk_off(30.0).label == "RISK_OFF"
    assert classify_risk_off(30.0).fit < classify_risk_off(22.0).fit < classify_risk_off(12.0).fit
