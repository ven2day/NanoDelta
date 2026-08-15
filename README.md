# NanoDelta

NanoDelta is a small, market-isolated ETL foundation for NSE, Forex, and Crypto.
It intentionally contains no trading UI, strategies, ML/LLM agents, risk engine,
order execution, or portfolio runtime.

## Architecture

```text
Provider payload
      |
      v
Raw / Bronze       immutable source payload + ingestion lineage
      |
      v
Canonical / Silver validated UTC OHLCV with canonical symbols
      |
      v
Features / Gold    deterministic, reproducible analytical columns
```

Every stored path begins with its market. Data never crosses between NSE,
Forex, and Crypto:

```text
data/{nse|forex|crypto}/{bronze|silver|gold}/event_date=YYYY-MM-DD/{record_id}.json
```

## Providers

| Market | Providers | Canonical symbol example |
|---|---|---|
| NSE | Dhan, TrueData | `RELIANCE` |
| Forex | OANDA | `EUR_USD` |
| Crypto | OKX, Poloniex | `BTC_USDT` |

Adapters translate provider fields at the Bronze → Silver boundary. Provider
symbols and raw payloads never leak into Silver or Gold records.

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
pytest
```

Example:

```python
from datetime import UTC, datetime
from pathlib import Path

from nanodelta.contracts import EventType, Market, Provider
from nanodelta.pipeline import EtlPipeline
from nanodelta.storage import FileLake

pipeline = EtlPipeline(FileLake(Path("data")))
result = pipeline.ingest(
    market=Market.CRYPTO,
    provider=Provider.OKX,
    event_type=EventType.CANDLE,
    provider_symbol="BTC-USDT",
    payload={
        "ts": "1786752000000",
        "o": "60000",
        "h": "61000",
        "l": "59500",
        "c": "60500",
        "vol": "120.5",
        "confirm": "1",
    },
    received_at=datetime.now(UTC),
)
print(result.canonical)
```

`EtlPipeline.ingest` always attempts Bronze first. Invalid or incomplete source
rows remain available in Bronze but do not enter Silver. Gold is built only from
validated Silver candles using `materialize_features`.

## Repository boundary

NanoDelta ends at clean, durable analytical datasets. Any future strategy,
signal, execution, or UI application must consume Gold through a separate
package or repository.

