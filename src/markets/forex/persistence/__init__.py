"""Forex-only repository factories and deterministic persistence identities."""

import hashlib
from typing import Any

from sqlalchemy.engine import Engine

from src.core.decisions import SchemaBoundDecisionRepository
from src.core.features import SchemaBoundFeatureRepository
from src.core.market_data import SchemaBoundRawMarketRepository
from src.core.models import MarketProvider
from src.core.persistence import SchemaBoundCandleRepository, SchemaBoundTradingRepository


def bind_candle_repository(store: Any) -> SchemaBoundCandleRepository:
    return SchemaBoundCandleRepository(store, market="FOREX", provider="OANDA")


def bind_trading_repository(engine: Engine) -> SchemaBoundTradingRepository:
    return SchemaBoundTradingRepository(engine, market="FOREX", provider="OANDA")


def bind_raw_market_repository(engine: Engine) -> SchemaBoundRawMarketRepository:
    return SchemaBoundRawMarketRepository(
        engine,
        market="FOREX",
        provider=MarketProvider.OANDA,
    )


def bind_feature_repository(engine: Engine) -> SchemaBoundFeatureRepository:
    return SchemaBoundFeatureRepository(engine, market="FOREX", provider="OANDA")


def bind_decision_repository(engine: Engine) -> SchemaBoundDecisionRepository:
    return SchemaBoundDecisionRepository(engine, market="FOREX", provider="OANDA")


def forex_record_id(kind: str, *parts: object) -> str:
    material = "|".join(("FOREX", kind.strip().upper(), *(str(part) for part in parts)))
    return f"forex-{kind.strip().lower()}-{hashlib.sha256(material.encode()).hexdigest()[:32]}"


__all__ = [
    "bind_candle_repository",
    "bind_decision_repository",
    "bind_feature_repository",
    "bind_raw_market_repository",
    "bind_trading_repository",
    "forex_record_id",
]
