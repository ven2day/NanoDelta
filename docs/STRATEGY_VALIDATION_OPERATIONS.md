# Strategy validation and admission

NanoDelta does not approve a strategy because its implementation exists. An
operator first generates immutable, cost-aware evidence from authoritative Gold
rows, reviews the result, and then approves the exact identity explicitly.

```bash
nanodelta-strategy --database-url "$DATABASE_URL" validate \
  --market nse --strategy momentum_continuation \
  --round-trip-cost 0.001 --tested-hypotheses 3
```

The command uses only each row and the following settled row for its outcome;
it does not read a future row while generating the signal. It reports trade
count, walk-forward stability, cost-adjusted expectancy, drawdown, an exact
one-sided sign-test p-value, and the Bonferroni hypothesis count. Failed results
are persisted but cannot be approved.

After independent review, an operator may admit the exact passing run:

```bash
nanodelta-strategy --database-url "$DATABASE_URL" approve \
  --market nse --strategy momentum_continuation \
  --validation-run-id VALIDATION_ID --approved-by operator@example.com \
  --reason "reviewed validation evidence" --days 30
```

Approval never occurs during startup, validation, CI, or deployment. The
runtime queries PostgreSQL for a current approval on every strategy-admission
cycle. The Strategy Lab API/UI exposes the stored definition, validation, and
latest approval evidence.

This workflow is production control infrastructure, not profitability evidence.
A production acceptance package still requires a credentialed provider session,
representative history, operator review, and a sustained paper-only soak run.
