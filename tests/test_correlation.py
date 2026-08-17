from __future__ import annotations

import pytest

from nanodelta.runtime.correlation import compute_return_correlations


def test_too_short_overlap_is_omitted() -> None:
    closes = {"A": [100.0, 101.0, 102.0], "B": [50.0, 51.0, 52.0]}
    assert compute_return_correlations(closes, minimum_overlap=20) == {}


def _geometric_series(start: float, returns: list[float]) -> list[float]:
    prices = [start]
    for r in returns:
        prices.append(prices[-1] * (1 + r))
    return prices


def test_identical_return_series_scores_exactly_one() -> None:
    returns = [0.01, -0.02, 0.015, 0.03, -0.01, 0.02, -0.015, 0.01, 0.005, -0.02, 0.012]
    a = _geometric_series(100.0, returns)
    b = _geometric_series(50.0, returns)
    result = compute_return_correlations({"A": a, "B": b}, minimum_overlap=10)
    assert result[("A", "B")] == pytest.approx(1.0)


def test_negated_return_series_scores_exactly_negative_one() -> None:
    returns = [0.01, -0.02, 0.015, 0.03, -0.01, 0.02, -0.015, 0.01, 0.005, -0.02, 0.012]
    a = _geometric_series(100.0, returns)
    b = _geometric_series(50.0, [-r for r in returns])
    result = compute_return_correlations({"A": a, "B": b}, minimum_overlap=10)
    assert result[("A", "B")] == pytest.approx(-1.0)


def test_flat_series_has_no_variance_and_is_omitted() -> None:
    a = [100.0] * 30
    b = [50.0 + i for i in range(30)]
    assert compute_return_correlations({"A": a, "B": b}, minimum_overlap=10) == {}


def test_pairs_are_keyed_in_sorted_symbol_order() -> None:
    a = [100.0 + i for i in range(15)]
    b = [50.0 + i * 2 for i in range(15)]
    result = compute_return_correlations({"ZEBRA": a, "ALPHA": b}, minimum_overlap=10)
    assert ("ALPHA", "ZEBRA") in result
    assert ("ZEBRA", "ALPHA") not in result


def test_single_symbol_has_no_pairs() -> None:
    assert compute_return_correlations({"A": [100.0, 101.0, 102.0]}, minimum_overlap=2) == {}
