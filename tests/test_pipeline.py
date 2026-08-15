from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nanodelta.contracts import CanonicalCandle, EventType, Market, Provider, stable_id
from nanodelta.pipeline import EtlPipeline
from nanodelta.storage import FileLake

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)


def pipeline(tmp_path: Path) -> EtlPipeline:
    return EtlPipeline(FileLake(tmp_path))


def test_okx_flows_to_isolated_bronze_and_silver(tmp_path: Path) -> None:
    result = pipeline(tmp_path).ingest(
        market=Market.CRYPTO,
        provider=Provider.OKX,
        event_type=EventType.CANDLE,
        provider_symbol="BTC-USDT",
        payload={
            "ts": str(int(NOW.timestamp() * 1000)),
            "o": "100",
            "h": "110",
            "l": "90",
            "c": "105",
            "vol": "20",
            "confirm": "1",
            "bar": "5m",
        },
        received_at=NOW,
    )
    assert result.bronze_created is True
    assert result.silver_created is True
    assert result.canonical is not None
    assert result.canonical.symbol == "BTC_USDT"
    assert len(list((tmp_path / "crypto" / "bronze").rglob("*.json"))) == 1
    assert len(list((tmp_path / "crypto" / "silver").rglob("*.json"))) == 1
    assert not (tmp_path / "nse").exists()


def test_incomplete_candle_stays_in_bronze(tmp_path: Path) -> None:
    result = pipeline(tmp_path).ingest(
        market=Market.FOREX,
        provider=Provider.OANDA,
        event_type=EventType.CANDLE,
        provider_symbol="EUR_USD",
        payload={
            "time": NOW.isoformat(),
            "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"},
            "volume": 10,
            "complete": False,
        },
        received_at=NOW,
    )
    assert result.bronze_created is True
    assert result.silver_created is False
    assert result.rejection_reason == "candle is not settled"


def test_invalid_payload_is_retained_only_in_bronze(tmp_path: Path) -> None:
    result = pipeline(tmp_path).ingest(
        market=Market.NSE,
        provider=Provider.DHAN,
        event_type=EventType.CANDLE,
        provider_symbol="RELIANCE",
        payload={"timestamp": NOW.isoformat(), "open": 10},
        received_at=NOW,
    )
    assert result.canonical is None
    assert result.bronze_created is True
    assert result.silver_created is False
    assert "missing required field" in str(result.rejection_reason)


def test_duplicate_raw_and_silver_writes_are_idempotent(tmp_path: Path) -> None:
    kwargs = {
        "market": Market.NSE,
        "provider": Provider.DHAN,
        "event_type": EventType.CANDLE,
        "provider_symbol": "RELIANCE",
        "payload": {
            "timestamp": NOW.isoformat(),
            "open": 10,
            "high": 12,
            "low": 9,
            "close": 11,
            "volume": 100,
        },
        "received_at": NOW,
    }
    first = pipeline(tmp_path).ingest(**kwargs)
    second = pipeline(tmp_path).ingest(**kwargs)
    assert first.bronze_created and first.silver_created
    assert not second.bronze_created and not second.silver_created


def test_provider_market_mismatch_never_enters_silver(tmp_path: Path) -> None:
    result = pipeline(tmp_path).ingest(
        market=Market.NSE,
        provider=Provider.OANDA,
        event_type=EventType.CANDLE,
        provider_symbol="EUR_USD",
        payload={},
        received_at=NOW,
    )
    assert result.bronze_created is True
    assert result.silver_created is False
    assert result.rejection_reason == "oanda cannot ingest nse data"


def candle(index: int, *, settled: bool = True) -> CanonicalCandle:
    open_time = NOW + timedelta(minutes=5 * index)
    close = 100.0 + index
    return CanonicalCandle(
        record_id=stable_id("nse", "RELIANCE", "5m", open_time.isoformat()),
        raw_record_id=f"raw-{index}",
        market=Market.NSE,
        provider=Provider.DHAN,
        symbol="RELIANCE",
        timeframe="5m",
        open_time=open_time,
        open=close - 0.5,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=100 + index * 10,
        is_settled=settled,
    )


def test_gold_features_are_derived_from_settled_silver(tmp_path: Path) -> None:
    features = pipeline(tmp_path).build_gold([candle(0), candle(1), candle(2, settled=False)])
    assert len(features) == 1
    assert features[0].candle_record_id == candle(1).record_id
    assert features[0].return_1 == pytest.approx(0.01)
    gold_file = next((tmp_path / "nse" / "gold").rglob("*.json"))
    assert json.loads(gold_file.read_text())["market"] == "nse"


def test_canonical_rejects_impossible_ohlc() -> None:
    with pytest.raises(ValueError, match="inconsistent"):
        CanonicalCandle(
            record_id="x",
            raw_record_id="r",
            market=Market.CRYPTO,
            provider=Provider.OKX,
            symbol="BTC_USDT",
            timeframe="1m",
            open_time=NOW,
            open=100,
            high=99,
            low=90,
            close=105,
            volume=1,
            is_settled=True,
        )


def test_non_candle_event_stops_after_bronze(tmp_path: Path) -> None:
    result = pipeline(tmp_path).ingest(
        market=Market.CRYPTO,
        provider=Provider.OKX,
        event_type=EventType.ORDER_BOOK,
        provider_symbol="BTC-USDT",
        payload={"bids": [], "asks": []},
        received_at=NOW,
    )
    assert result.canonical is None
    assert result.bronze_created is True
    assert result.silver_created is False
