"""Canonical/Silver validation shared by all provider normalizers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from src.core.models import CanonicalCandle, CanonicalQuote


class CanonicalDataIssue(StrEnum):
    EMPTY_SYMBOL = "EMPTY_SYMBOL"
    NON_CANONICAL_SYMBOL = "NON_CANONICAL_SYMBOL"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_VOLUME = "INVALID_VOLUME"
    INVALID_TIMEFRAME = "INVALID_TIMEFRAME"
    CROSSED_MARKET = "CROSSED_MARKET"


CanonicalT = TypeVar("CanonicalT", CanonicalCandle, CanonicalQuote)


@dataclass(frozen=True)
class CanonicalizationResult(Generic[CanonicalT]):
    value: CanonicalT
    issues: tuple[CanonicalDataIssue, ...]

    @property
    def accepted(self) -> bool:
        return not self.issues

    def require_valid(self) -> CanonicalT:
        if self.issues:
            joined = ",".join(issue.value for issue in self.issues)
            raise ValueError(f"Canonical market data rejected: {joined}")
        return self.value


def _symbol_issues(symbol: str) -> list[CanonicalDataIssue]:
    if not symbol.strip():
        return [CanonicalDataIssue.EMPTY_SYMBOL]
    if symbol != symbol.strip().upper() or " " in symbol:
        return [CanonicalDataIssue.NON_CANONICAL_SYMBOL]
    return []


def validate_canonical_candle(
    candle: CanonicalCandle,
) -> CanonicalizationResult[CanonicalCandle]:
    issues = _symbol_issues(candle.symbol)
    if not candle.timeframe.strip():
        issues.append(CanonicalDataIssue.INVALID_TIMEFRAME)
    if min(candle.open, candle.high, candle.low, candle.close) <= 0:
        issues.append(CanonicalDataIssue.INVALID_PRICE)
    if candle.volume < 0:
        issues.append(CanonicalDataIssue.INVALID_VOLUME)
    return CanonicalizationResult(candle, tuple(dict.fromkeys(issues)))


def validate_canonical_quote(quote: CanonicalQuote) -> CanonicalizationResult[CanonicalQuote]:
    issues = _symbol_issues(quote.symbol)
    if quote.bid <= 0 or quote.ask <= 0:
        issues.append(CanonicalDataIssue.INVALID_PRICE)
    if quote.ask < quote.bid:
        issues.append(CanonicalDataIssue.CROSSED_MARKET)
    return CanonicalizationResult(quote, tuple(dict.fromkeys(issues)))
