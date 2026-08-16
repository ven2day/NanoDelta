# Authoritative UI API

The UI backend-for-frontend sends a role-specific API key to the Python API. All UI reads require
`viewer`, `operator`, or `admin`; runtime mutations require `operator` or `admin`; administrative
kill switches require `admin`.

Paginated endpoints return `items`, bounded `page` metadata, and `freshness`. An empty array means
the authoritative table has no matches. An unconfigured PostgreSQL adapter returns `501` with
`AUTHORITATIVE_READ_MODEL_UNAVAILABLE`; representative data is never substituted.

| Endpoint | Authoritative source | Filters |
|---|---|---|
| `/{market}/candles` | `{market}_silver.candles` | symbol/timeframe, pagination |
| `/{market}/features` | `{market}_gold.feature_snapshots` | symbol/timeframe, pagination |
| `/{market}/history-status` | `control.history_runs` | symbol/timeframe, latest run |
| `/{market}/orders` | `paper.orders` and fills | symbol/action/state, pagination |
| `/{market}/trades` | `paper.outcomes` | symbol/strategy, pagination |
| `/{market}/positions` | `paper.positions` | symbol/state, pagination |
| `/{market}/decision-events` | `control.decision_events` | symbol/timeframe/stage/status/reason |
| `/decision-cycles/{cycle_id}` | `control.decision_events` | cycle, pagination |
| `/{market}/risk/aggregate` | open paper positions | market |
| `/{market}/performance` | paper outcomes | market |
| `/alerts`, `/reports`, `/settings`, `/audit` | matching control tables | market, pagination |
| `/strategy-lab/strategies` | definitions/latest approval | market, pagination |
| `/strategy-lab/validations` | validation runs | market, pagination |

Paths above are under `/api`. SQL resources, columns, filters, and ordering are fixed server-side.

| Role | Read | Confirmed runtime actions | Admin kill switches |
|---|---:|---:|---:|
| viewer | yes | no | no |
| operator | yes | yes | no |
| admin | yes | yes | yes |

The API accepts an admin secret file. When `NANODELTA_BACKEND_KEYS_PATH` is configured, it must
contain distinct non-empty `viewer`, `operator`, and `admin` values; invalid JSON or missing roles
fail application startup. Keys remain server-side.

Risk derives entry notional, realized P&L, and fees from open positions. Unrealized P&L,
mark-to-market exposure, and remaining daily risk are explicitly unavailable because there is no
authoritative risk snapshot. Performance derives closed trades, P&L, fees, wins, and win rate from
paper outcomes. Sharpe, Sortino, drawdown, and equity curve remain unavailable until a reproducible
equity-series model exists.

Intentionally unavailable: report generation/download mutations, settings writes, alert workflow
mutations, mark-to-market snapshots, equity curves, strategy creation/backtest/promotion mutations,
and live orders. FinOps and Qwen routes are registered only when their configured services are
composed. History repair still requires an executable history engine/job registry; persisted status
reads do not pretend that such a worker exists. NanoDelta remains paper-only.
