# Provider, database and paper-session verification

This checkpoint provides a controlled proof path from provider payload to a paper order:

```text
provider route -> Bronze -> canonical Silver -> Gold feature
-> fixture strategy candidate -> portfolio allocation -> deterministic risk -> paper order
```

It does not add a live broker execution interface. The included strategy is explicitly a
fixture lineage probe and is not registered by the production runtime.

## Deterministic replay

Run without credentials or network access:

```bash
python scripts/run_recorded_paper_evidence.py \
  --output docs/evidence/recorded-paper-session.json
```

The fixture declares `captured: false`; it validates provider-contract shape and system
wiring without pretending to be exchange evidence.

## TimescaleDB integration

Use a disposable migrated database, never a production database:

```bash
export NANODELTA_INTEGRATION_DATABASE_URL='postgresql://.../nanodelta_test'
pytest tests/test_provider_database_e2e.py
```

The test applies checksum-verified migrations, writes the recorded Dhan payload through
PostgreSQL/TimescaleDB, and reconciles minimum Bronze, Silver and Gold row counts.

## Live provider verification

Live checks are skipped unless `NANODELTA_LIVE_PROVIDER_TESTS=1`. Authenticated values
must be supplied as secret file paths; secrets are never command-line arguments.

Required paths by provider:

- Dhan: `DHAN_CLIENT_ID_PATH`, `DHAN_ACCESS_TOKEN_PATH`, `DHAN_TEST_SECURITY_ID_PATH`
- TrueData: `TRUEDATA_USERNAME_PATH`, `TRUEDATA_PASSWORD_PATH`
- OANDA practice: `OANDA_ACCOUNT_ID_PATH`, `OANDA_ACCESS_TOKEN_PATH`
- OKX/Poloniex public history: global opt-in only

Then run only the controlled live suite:

```bash
NANODELTA_LIVE_PROVIDER_TESTS=1 pytest -q tests/test_live_provider_opt_in.py
```

Limitations:

- This is historical-candle integration evidence, not a realtime soak test.
- TrueData live verification requires the optional SDK extra.
- A skipped database test is not proof of a deployed TimescaleDB environment.
- A deterministic replay is not proof of provider uptime or current data correctness.
- Production readiness still requires a recorded supervised paper session and soak/failover evidence.
