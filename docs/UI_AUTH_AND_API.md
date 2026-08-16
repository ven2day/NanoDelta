# UI authentication and authoritative API integration

## Security model

The Next.js application is a backend-for-frontend (BFF):

1. The browser submits credentials only to `/api/auth/login` on the Next.js server.
2. The server verifies an `scrypt` password hash from a read-only mounted JSON file.
3. A signed, eight-hour `HttpOnly`, `SameSite=Strict` session cookie is returned. Production cookies
   are also `Secure`.
4. Browser requests use `/api/backend/*`. The BFF validates the session, allowlists the requested
   read endpoint, chooses the server-side API key for the user's role, and calls NanoDelta API.
5. Backend API keys, password hashes, and the signing key never enter client-side JavaScript.

Roles are `viewer`, `operator`, and `admin`. This checkpoint exposes read-only BFF routes, so all
roles can view authoritative data. It deliberately does not expose runtime commands. When command UI
is added, operator/admin checks must be enforced both in the BFF and the existing backend controller.

This local file contract is suitable for a single-host deployment. Use an identity provider and a
central secret manager before exposing NanoDelta beyond a trusted private network.

## Secret files

Create these untracked files with mode `0600`:

```text
secrets/ui_session_key
secrets/ui_users.json
secrets/ui_backend_keys.json
```

Generate a signing key with `openssl rand -base64 48 > secrets/ui_session_key`, then run
`chmod 600 secrets/ui_session_key`.

Generate an `scrypt` salt/hash pair without storing the plaintext password:

```bash
cd web
npm run hash-password -- 'choose-a-strong-password'
```

Add the result to `secrets/ui_users.json`:

```json
[{"username":"operator1","role":"operator","salt":"hex-salt","password_hash":"hex-hash"}]
```

Map each role to a backend key in `secrets/ui_backend_keys.json`:

```json
{"viewer":"viewer-key","operator":"operator-key","admin":"admin-key"}
```

The deployed API bootstrap currently provisions only its admin key. Until it provisions distinct
viewer/operator keys, the three values can reference the existing key, but the deployment remains
single-key and must not be described as end-to-end backend RBAC. The BFF still authenticates users and
does not expose write routes. Distinct backend principals are a follow-up API-runtime requirement.

## Authoritative page mapping

| UI page | Backend source | Status |
|---|---|---|
| Overview / Workspace | `GET /api/overview` | Wired |
| Decisions | market decisions or exact decision cycle | Wired |
| Portfolio | market paper positions | Wired |
| Strategies | market strategies | Wired |
| Performance | market paper outcomes | Wired as raw outcomes; no fabricated metrics |
| Data Center | one symbol/timeframe history status | Wired; no aggregate readiness endpoint |
| Operations | market health | Wired |
| Charts, Orders, Strategy Lab | none | Explicit unavailable states |
| Alerts, Risk, Reports, Settings, Audit | none | Explicit unavailable states |

Every connected view implements loading, error, empty, and last-received freshness states. The UI
uses `cache: no-store`. A history query sends symbol and timeframe to the API. Decision cycle filtering
is sent to the cycle endpoint. Other collection APIs do not currently accept server-side filters.

## Local environment

Docker Compose supplies these automatically. For `npm run dev`, configure:

```text
NANODELTA_API_URL=http://127.0.0.1:8000
NANODELTA_UI_USERS_PATH=/absolute/path/to/ui_users.json
NANODELTA_SESSION_KEY_PATH=/absolute/path/to/ui_session_key
NANODELTA_BACKEND_KEYS_PATH=/absolute/path/to/ui_backend_keys.json
```
