# NanoDelta agent guidance

NanoDelta currently implements the ETL foundation. The target platform is documented in
`docs/`, but later checkpoints are not implemented merely because they are documented.

## Scope

- Bronze stores immutable provider payloads.
- Silver stores validated canonical market data.
- Gold stores reproducible analytical features.
- NSE, Forex, and Crypto are isolated by market at every layer.
- Provider-specific field mapping belongs only in `nanodelta.markets`.

Do not add later-phase UI, strategies, agents, risk, or paper execution before their
preceding contracts, database migrations, tests, and roadmap checkpoint are complete.
TradingAgents is advisory only and can never write orders or bypass deterministic risk.
Execution remains paper-only unless the owner explicitly changes that policy.

## Checks

```bash
python -m pytest
ruff check .
mypy src
```

