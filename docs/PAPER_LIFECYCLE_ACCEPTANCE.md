# Paper lifecycle acceptance

The runtime now registers immutable stop/target plans after an approved paper entry. Each settled
Gold update checks those plans before considering new entries. A triggered exit must reduce the
existing position; it cannot add, reverse, or open a position. The close fill, position state and
outcome are committed durably, and the existing authoritative `orders`, `positions`, `trades` and
`decisions` APIs expose the result consumed by the UI.

## Disposable TimescaleDB E2E

The opt-in test uses synthetic settled OKX-shaped candles. It explicitly creates a passing
validation and test-only approval, then proves:

`settled candle → Gold → explicit approval → BUY → risk → fill → position → target exit → outcome → authoritative reads`

It never contacts OKX or any broker and is not real-provider evidence. The database name must begin
with `nanodelta_e2e_`, and the test refuses a database containing paper orders.

```bash
createdb nanodelta_e2e_paper_lifecycle
NANODELTA_E2E_DATABASE_URL=postgresql://.../nanodelta_e2e_paper_lifecycle \
  pytest -q tests/test_timescaledb_paper_e2e.py
dropdb nanodelta_e2e_paper_lifecycle
```

Without `NANODELTA_E2E_DATABASE_URL`, the test reports `SKIPPED`; CI must not report it as passed
external evidence. Credentialed provider soak testing remains a separate production acceptance gate.
