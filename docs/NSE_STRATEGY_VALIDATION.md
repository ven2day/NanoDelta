# NSE strategy validation and paper admission

This checkpoint adds an evidence workflow for NanoDelta's existing NSE technical
plugins:

- VWAP Pullback (`5m`);
- EMA(9)/EMA(21) with RSI(14) continuation (`15m`);
- SuperTrend (ATR 14 × 3) with ADX(14) (`15m`).

An implementation is not an approved strategy. Validation writes immutable
research evidence. A separate, named reviewer may promote only a passing exact
strategy identity into the existing paper-approval registry. Nothing in this
workflow creates live-order authority.

## Evidence path

```text
explicit credentialed run
  -> 760 calendar-day Dhan request window
  -> settled nse_silver candles
  -> readiness: 5m + 15m + 30m + 1h
  -> deterministic technical features
  -> chronological disjoint walk-forward folds
  -> brokerage + taxes/fees + slippage
  -> stability, drawdown, sign test, Bonferroni gate
  -> FAILED or RESEARCH
  -> explicit human review
  -> PAPER_APPROVED (only when every gate passed)
```

The runner requests 760 days so the stored evidence can prove a full 730-day
window after weekends, last-settled-bar alignment, and feature warm-up. Readiness
requires all configured symbols at all four timeframes, a first candle at or
before the two-year boundary, a fresh last candle, Dhan provenance, and at least
80% of a conservative NSE weekday/session candle estimate. `30m` remains the
existing deterministic aggregation of complete Dhan `15m` pairs.

The strategies have fixed parameters, so no model is fitted. Each walk-forward
fold is a disjoint chronological forward test slice. A signal uses technical
features through time `t`; its outcome uses only the next settled close for the
same symbol. A candle after the campaign's `as_of` boundary cannot change the
result or data fingerprint.

## Database artifacts

Migration `0016_nse_strategy_validation_evidence.sql` adds:

- `research.nse_validation_campaigns` — immutable run configuration and source;
- `research.nse_validation_readiness` — symbol/timeframe coverage evidence;
- `research.nse_strategy_evidence` — walk-forward and cost evidence linked to
  the existing immutable `research.validation_runs` record;
- `research.nse_strategy_promotions` — reviewer linkage to the existing
  `research.strategy_approvals` artifact;
- `research.nse_strategy_validation_read` and
  `research.nse_backtest_read` — fixed authoritative read views.

No failed validation can be inserted as a promotion: both the application
service and the promotion insert require passing `RESEARCH` evidence. Revocation
continues to use the existing approval registry. A database trigger also refuses
new `APPROVED` rows for these three NSE strategies unless the exact validation
run has passing NSE evidence. The authoritative view labels an approval
`PAPER_APPROVED` only when it also has the reviewer-promotion linkage.

An upgraded database may already contain approvals created before migration
0016. Deployment review must inspect and revoke any legacy NSE technical approval
that lacks this evidence; the migration does not silently delete or rewrite
operator records.

## Credentialed validation

Apply migrations first. Mount credentials as files; do not commit them.

Required configuration:

```bash
export DATABASE_URL='postgresql://...'
export NSE_SYMBOLS_CSV='/run/config/nse-symbols.csv'
export DHAN_CLIENT_ID='...'
export DHAN_ACCESS_TOKEN_PATH='/run/secrets/dhan_access_token'
export NANODELTA_ENABLE_CREDENTIALED_NSE_VALIDATION=true
```

PIN/TOTP file authentication is also supported through `DHAN_PIN_PATH` and
`DHAN_TOTP_SECRET_PATH`. The explicit enable flag is mandatory and is checked
before the first provider call.

Run validation:

```bash
python scripts/validation/run_nse_strategy_validation.py validate \
  --concurrency 2 \
  --brokerage-bps 3 \
  --taxes-and-fees-bps 7 \
  --slippage-bps 5
```

The command prints campaign IDs, backfill job outcomes, exact metrics, rejection
reasons, and `approval_created: false`. Provider or readiness failures remain
durable `FAILED` evidence; they are never represented as zero-cost success.

After independent review of a passing evidence ID:

```bash
python scripts/validation/run_nse_strategy_validation.py promote \
  --evidence-id "$EVIDENCE_ID" \
  --reviewed-by 'quant-reviewer@example.com' \
  --reason 'reviewed provider provenance, costs and walk-forward evidence' \
  --days 30
```

Promotion is deliberately a separate command. Validation, startup, deployment,
CI, and a successful backfill never auto-approve a strategy.

## Strategies and Backtests integration hook

`build_nse_validation_router()` in `nanodelta.validation.router` exposes fixed,
paginated reads at:

- `GET /api/nse/strategy-validation/strategies` with `strategy_id`, `timeframe`,
  and `lifecycle_state` filters;
- `GET /api/nse/strategy-validation/backtests` with `strategy_id`, `timeframe`,
  and `research_state` filters.

The production API assembler must include this router with its existing operator
authentication dependency. This branch intentionally does not edit the shared
API assembler or web application, allowing the validation work to merge without
conflicting with the concurrent UI and runtime tracks.

## Honest limitations

- Unit tests prove deterministic behavior and governance invariants; they do not
  constitute a real Dhan or TimescaleDB validation run.
- Dhan account permissions, retention limits, throttling, and a complete 760-day
  intraday response must be verified with the operator's account.
- The current outcome is next-settled-close return, not an intrabar order-book or
  fill simulation. Costs are explicit assumptions and must be reviewed.
- A strategy may truthfully remain `FAILED`. No profitability or expected return
  is claimed by this implementation.
- Production readiness still requires the credentialed run, reviewer evidence,
  a continuous paper session, soak/failover testing, and acceptance artifacts.
