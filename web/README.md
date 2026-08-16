# NanoDelta Web UI

Authenticated operations interface for NSE, Forex, and Crypto paper trading. The browser talks only to the Next.js BFF. The BFF validates an HTTP-only signed session, allowlists backend read routes, and injects the backend API key server-side.

## Required configuration

Use secret files in production. Direct values are supported for local development only.

| Variable | Purpose |
|---|---|
| `NANODELTA_BACKEND_URL` | Internal FastAPI origin, for example `http://api:8000` |
| `NANODELTA_BACKEND_READ_API_KEY_FILE` | Read-role backend API key file |
| `NANODELTA_BACKEND_OPERATOR_API_KEY_FILE` | Operator-role backend API key file |
| `NANODELTA_BACKEND_ADMIN_API_KEY_FILE` | Admin-role backend API key file |
| `NANODELTA_WEB_USERNAME_FILE` | UI username secret file |
| `NANODELTA_WEB_PASSWORD_FILE` | UI password secret file |
| `NANODELTA_WEB_SESSION_SECRET_FILE` | Random session-signing secret of at least 32 characters |
| `NANODELTA_WEB_ROLE` | `read`, `operator`, or `admin`; defaults to `read` |

Each secret-file variable has a direct-value equivalent without `_FILE` for local development only.

```bash
npm ci
npm run lint
npm run build
npm run dev
```

## Authoritative pages

- Overview: `/api/overview`
- BUY/SELL Decisions: `/api/{market}/decisions`
- Positions: `/api/{market}/paper/positions`
- Strategies: `/api/{market}/strategies`
- Features: `/api/{market}/features`
- Agent Runs: `/api/{market}/agent-runs`
- Outcomes: `/api/{market}/paper/outcomes`
- Operations: `/api/{market}/health`

Empty, unavailable, error, loading, and stale states are explicit. The UI does not substitute sample values when a service or record is absent.

Charts, orders/fills, strategy experiments, performance aggregation, alerts, risk utilization, reports, settings, and audit-list views are intentionally absent because `main` does not expose authoritative read contracts for them yet.
