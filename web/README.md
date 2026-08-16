# NanoDelta Web UI

Authenticated operations interface for NanoDelta paper trading. NSE is the first composed market
workspace; the remaining market pages retain authoritative record views. The browser talks only to
the Next.js BFF. The BFF validates an HTTP-only opaque session against the durable backend identity
store, allowlists backend read routes, and injects the backend API key server-side.

## Required configuration

Provision users with `nanodelta-auth`; see `docs/IDENTITY_AND_ACCESS.md`.

| Variable | Purpose |
|---|---|
| `NANODELTA_BACKEND_URL` | Internal FastAPI origin, for example `http://api:8000` |
| `NANODELTA_BACKEND_KEYS_PATH` | JSON secret file containing distinct `viewer`, `operator`, and `admin` keys |

```bash
npm ci
npm run lint
npm run build
npm run dev
```

## NSE pages

Every NSE page is composed from the existing authoritative read contracts. Tables never fall back
to examples, representative records, browser-local trading data, or inferred values. Filters are
kept in the URL so an investigation can be bookmarked or shared inside the authenticated console.

| Page | Authoritative source | Useful filters and behavior |
|---|---|---|
| Dashboard | overview, health, session, universe, signals, positions, performance, risk, alerts | Runtime/session state, genuine recent activity and exposure summaries |
| NSE Workspace / Decisions | decision events, signals, strategies, orders, positions, candles | Symbol, timeframe, strategy, BUY/SELL, final decision, readiness, date, freshness and cycle; lifecycle and persisted scoring attribution |
| Universe | configured universe | Symbol, provider and freshness; only the enabled runtime set is currently exposed |
| Strategies | strategy definitions and validation runs | Timeframe, strategy, approval state, date and freshness |
| Signals | immutable signal candidates | Symbol, timeframe, strategy, BUY/SELL, cycle, date and freshness |
| Positions | paper positions and orders | Symbol, state, date and freshness |
| Risk | aggregate risk, open positions and risk-stage decisions | Symbol, state, date and freshness; unavailable mark-to-market fields remain explicit |
| Backtests | strategy validation runs | Strategy, pass/fail, date and freshness |
| Reports | report runs, performance and closed outcomes | State, date and freshness |
| Logs | operational audit and alerts | State, date and freshness |
| Settings | system settings, health and NSE session | Freshness plus role-aware, disabled mutation affordances |

The decision workspace displays each candidate's recorded stage lifecycle, deterministic scoring
attribution, and settled-candle chart. Candidate entry, stop, and target evidence is shown before an
order; a paper fill supersedes the proposed entry.

- Overview: `/api/overview`
- BUY/SELL Decisions: `/api/{market}/decision-events`
- BUY/SELL candidate evidence: `/api/{market}/signals`
- Configured universe: `/api/{market}/universe`
- NSE normal-market status: `/api/nse/session`
- Positions: `/api/{market}/positions`
- Orders: `/api/{market}/orders`
- Trades: `/api/{market}/trades`
- Strategies: `/api/{market}/strategies`
- Features: `/api/{market}/features`
- Performance: `/api/{market}/performance`
- Risk: `/api/{market}/risk/aggregate`
- Alerts, reports, settings and audit: global authoritative read models filtered by market
- Operations: `/api/{market}/health`

Loading, request errors, valid empty results, stale data (newest authoritative timestamp older than
15 minutes), and missing contracts are visually distinct. Refresh is manual on record pages and the
decision workspace refreshes while the browser tab is visible. Runtime/session status refreshes
independently every 15 seconds.

## Deliberate unavailable states

The UI exposes these boundaries instead of simulating them:

- Disabled historical universe rows cannot be requested because the current HTTP contract defaults
  to the enabled set and does not expose an explicit unfiltered mode.
- Dedicated backtest job progress, equity curves and downloadable backtest artifacts have no read
  contract; the Backtests page therefore shows authoritative validation artifacts only.
- Unrealized P&L, mark-to-market exposure, remaining daily risk, Sharpe, Sortino, maximum drawdown
  and an equity curve are returned as unavailable by the backend.
- Strategy experiments/approval, universe edits, position intervention, risk-limit edits, report
  generation/download, alert acknowledgement, settings writes and runtime controls have no audited
  browser mutation contract. Viewer/operator/admin roles are displayed, but the controls remain
  disabled for every role until those contracts exist.

Strategy experiment mutations, report downloads, settings writes, alert workflow mutations and live
orders remain unavailable because the backend intentionally exposes no such production contract.
