"""Real NIFTY / sector-index / India VIX market data.

Security IDs below are resolved against Dhan's own instrument master
(https://images.dhan.co/api-data/api-scrip-master-detailed.csv, EXCH_ID=NSE,
SEGMENT=I rows), not guessed -- verified 2026-08-17. Ingested through a
dedicated DhanClient on the IDX_I exchange segment / INDEX instrument type,
completely separate from the NSE_EQ equity client, so nothing here can affect
the live equity feed. Candles land in the same nse_silver.candles table as
equities (Market.NSE), namespaced by these index symbol names, none of which
collide with any configured equity ticker.
"""

from __future__ import annotations

MARKET_INDEX_SYMBOL = "NIFTY"
VIX_SYMBOL = "INDIA_VIX"

INDEX_SECURITY_IDS: dict[str, str] = {
    "NIFTY": "13",
    "INDIA_VIX": "21",
    "BANKNIFTY": "25",
    "NIFTY_AUTO": "14",
    "NIFTY_FMCG": "28",
    "NIFTY_IT": "29",
    "NIFTY_MEDIA": "30",
    "NIFTY_METAL": "31",
    "NIFTY_PHARMA": "32",
    "NIFTY_REALTY": "34",
    "NIFTY_ENERGY": "42",
    "NIFTY_HEALTHCARE": "447",
    "NIFTY_FINSRV": "469",
}

# Maps this system's sector buckets (universe/sectors.py) to a real NIFTY
# sectoral index symbol above. Sectors with no entry here (CEMENT,
# INFRA_CONSTRUCTION, CONSUMER_DURABLES, CHEMICALS, TELECOM, RETAIL,
# LOGISTICS, CAPITAL_GOODS, DEFENSE, AVIATION_TRAVEL, DIVERSIFIED) have no
# published NIFTY sectoral index and keep the breadth-proxy fallback instead
# of a fabricated mapping.
SECTOR_INDEX_SYMBOL: dict[str, str] = {
    "BANKING": "BANKNIFTY",
    "AUTO": "NIFTY_AUTO",
    "FMCG": "NIFTY_FMCG",
    "IT": "NIFTY_IT",
    "MEDIA_ENTERTAINMENT": "NIFTY_MEDIA",
    "METALS": "NIFTY_METAL",
    "PHARMA": "NIFTY_PHARMA",
    "REALTY": "NIFTY_REALTY",
    "ENERGY": "NIFTY_ENERGY",
    "HEALTHCARE_SERVICES": "NIFTY_HEALTHCARE",
    "FINANCIAL_SERVICES": "NIFTY_FINSRV",
}
