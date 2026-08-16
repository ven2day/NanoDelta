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

## Authoritative pages

The NSE decision workspace composes session/runtime health, the configured universe, features,
approved strategies, persisted BUY/SELL candidates, decision events, paper orders, positions, and
Silver candles. Selecting a row displays its recorded stage lifecycle, full deterministic scoring
attribution, and a settled-candle chart. Candidate entry, stop, and target evidence is shown before
an order; a paper fill supersedes the proposed entry. Missing evidence is rendered as unavailable
rather than inferred.

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

Empty, unavailable, error, loading, and stale states are explicit. The UI does not substitute sample values when a service or record is absent.

Strategy experiment mutations, report downloads, settings writes, alert workflow mutations and live
orders remain unavailable because the backend intentionally exposes no such production contract.
