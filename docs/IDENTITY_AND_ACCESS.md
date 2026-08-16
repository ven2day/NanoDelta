# Identity and access operations

NanoDelta uses individual PostgreSQL identities, scrypt password hashes, opaque revocable
sessions, role enforcement, and hashed API keys. Raw passwords, session tokens, and API keys are
never stored. Five failed logins from the same username/source pair within 15 minutes cause a
30-minute lockout. All login, user, session, and key lifecycle changes write `auth.security_audit`.

Apply migrations before provisioning the first administrator:

```bash
docker compose run --rm migrate
docker compose run --rm api nanodelta-auth upsert-user operator@example.com --role admin
```

The password prompt does not echo or place the password in shell history. Updating a user rotates
the password/role and revokes all existing sessions. Disable a departing user with:

```bash
docker compose exec api nanodelta-auth disable-user operator@example.com
```

Create a replacement API key, update its consumer, verify it, and only then revoke the old key:

```bash
docker compose exec api nanodelta-auth create-api-key web-viewer-v2 --actor-id web-viewer --role viewer
docker compose exec api nanodelta-auth revoke-api-key KEY_ID
```

The raw key is displayed exactly once. Existing file-mounted keys continue to work during the
migration window; remove them after every consumer uses a durable key. The admin HTTP endpoints
`POST /api/admin/api-keys` and `DELETE /api/admin/api-keys/{key_id}` provide the same lifecycle and
require an admin API key.

This checkpoint does not claim MFA or SSO. Those require a selected identity provider, issuer and
audience validation, recovery policy, and a credentialed acceptance test.
