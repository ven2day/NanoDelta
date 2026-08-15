# NanoDelta agent guidance

NanoDelta is an ETL project, not a trading application.

## Scope

- Bronze stores immutable provider payloads.
- Silver stores validated canonical market data.
- Gold stores reproducible analytical features.
- NSE, Forex, and Crypto are isolated by market at every layer.
- Provider-specific field mapping belongs only in `nanodelta.markets`.

Do not add UI, order execution, strategies, signals, brokers, agents, LLMs, ML
training, portfolio risk, or paper/live trading code to this repository.

## Checks

```bash
python -m pytest
ruff check .
mypy src
```

