"""Deterministic Silver-to-Gold feature materialization."""

from __future__ import annotations

from collections import defaultdict

from nanodelta.contracts import CanonicalCandle, FeatureRecord, stable_id


def materialize_features(candles: list[CanonicalCandle]) -> list[FeatureRecord]:
    groups: dict[tuple[str, str, str], list[CanonicalCandle]] = defaultdict(list)
    for candle in candles:
        if candle.is_settled:
            groups[(candle.market.value, candle.symbol, candle.timeframe)].append(candle)

    features: list[FeatureRecord] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda candle: candle.open_time)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if previous.close == 0 or current.open == 0:
                continue
            volume_change = None
            if previous.volume > 0:
                volume_change = current.volume / previous.volume - 1
            features.append(
                FeatureRecord(
                    record_id=stable_id(current.record_id, "features", 1),
                    candle_record_id=current.record_id,
                    market=current.market,
                    symbol=current.symbol,
                    timeframe=current.timeframe,
                    event_time=current.open_time,
                    close=current.close,
                    return_1=current.close / previous.close - 1,
                    range_pct=(current.high - current.low) / current.open,
                    body_pct=(current.close - current.open) / current.open,
                    volume_change=volume_change,
                )
            )
    return sorted(features, key=lambda row: (row.market.value, row.symbol, row.event_time))
