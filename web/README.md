# NanoDelta Web UI

Authenticated operations interface for NSE, Forex, and Crypto paper trading. The browser talks only to the Next.js BFF. The BFF validates an HTTP-only signed session, allowlists backend read routes, and injects the backend API key server-side.

## Required configuration

Use secret files in production. Direct values are supported for local development only.

| Variable | Purpose |
|---|---|
| `NANODELTA_BACKEND_URL` | Internal FastAPI origin, for example `http://api:8000` |
| `NANODELTA_BACKEND_KEYS_PATH` | JSON secret file containing distinct `viewer`, `operator`, and `admin` keys |
| `NANODELTA_WEB_USERNAME_FILE` | UI username secret file |
| `NANODELTA_WEB_PASSWORD_FILE` | UI password secret file |
| `NANODELTA_WEB_SESSION_SECRET_FILE` | Random session-signing secret of at least 32 characters |
| `NANODELTA_WEB_ROLE` | `viewer`, `operator`, or `admin`; defaults to `viewer` |

Each secret-file variable has a direct-value equivalent without `_FILE` for local development only.

```bash
npm ci
npm run lint
npm run build
npm run dev
```

## Authoritative pages

- Overview: `/api/overview`
- BUY/SELL Decisions: `/api/{market}/decision-events`
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

Candlestick charts remain absent because they require an explicit symbol and timeframe selection.
Strategy experiment mutations, report downloads, settings writes, alert workflow mutations and live
orders remain unavailable because the backend intentionally exposes no such production contract.
