from __future__ import annotations

from nanodelta.runtime.regime import classify_breadth


def test_too_few_symbols_is_insufficient_breadth() -> None:
    breadth = classify_breadth({"A": 30.0}, {"A": True}, minimum_symbols=5)
    assert breadth.label == "INSUFFICIENT_BREADTH"
    assert breadth.fit == 1.0


def test_low_average_adx_is_ranging() -> None:
    adx = {s: 10.0 for s in "ABCDE"}
    bullish = {s: True for s in "ABCDE"}
    breadth = classify_breadth(adx, bullish, minimum_symbols=5)
    assert breadth.label == "RANGING"
    assert breadth.fit < 1.0


def test_strong_bullish_majority_is_trending_up() -> None:
    adx = {s: 30.0 for s in "ABCDE"}
    bullish = {"A": True, "B": True, "C": True, "D": True, "E": False}
    breadth = classify_breadth(adx, bullish, minimum_symbols=5)
    assert breadth.label == "TRENDING_UP"
    assert breadth.fit > 1.0


def test_strong_bearish_majority_is_trending_down() -> None:
    adx = {s: 30.0 for s in "ABCDE"}
    bullish = {"A": False, "B": False, "C": False, "D": False, "E": True}
    breadth = classify_breadth(adx, bullish, minimum_symbols=5)
    assert breadth.label == "TRENDING_DOWN"
    assert breadth.fit > 1.0


def test_mixed_direction_with_strong_trend_is_volatile() -> None:
    adx = {s: 30.0 for s in "ABCDEF"}
    bullish = {"A": True, "B": True, "C": True, "D": False, "E": False, "F": False}
    breadth = classify_breadth(adx, bullish, minimum_symbols=5)
    assert breadth.label == "VOLATILE"
    assert breadth.fit < 1.0
