# NanoDelta agent guidance

NanoDelta implements tested foundations for market data, strategy governance, staged decisions,
deterministic risk, paper execution, operations APIs, and a multi-market UI prototype. A component
is production-ready only when its deployment, integration, failure, and operational evidence is
complete; documentation alone never establishes completion.

## Scope

- Bronze stores immutable provider payloads.
- Silver stores validated canonical market data.
- Gold stores reproducible features.
- NSE, Forex, and Crypto are isolated at every layer.
- Provider mapping belongs only in `nanodelta.markets`.
- TradingAgents and Qwen are advisory and cannot write orders or bypass risk.
- Execution remains paper-only.
- UI state must come from authoritative APIs; fixtures must be labelled and removed during integration.
- Production changes require explicit health, migration, backup, rollback, and secret-handling behavior.

## Checks

```bash
python -m pytest
ruff check .
mypy src
cd web && npm run lint && npm run build
docker compose config
```
