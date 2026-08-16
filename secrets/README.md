# Runtime secrets

Create these files locally before deployment. They are excluded from Git.

- `db_password`: strong PostgreSQL password
- `admin_api_key`: random NanoDelta administrative API key
- `backend_keys.json`: distinct `viewer`, `operator`, and `admin` API keys used by the UI BFF
- `web_username`: operator UI login name
- `web_password`: strong operator UI password
- `web_session_secret`: random session-signing secret (at least 32 characters)

Use restrictive permissions:

```bash
install -m 700 -d secrets
openssl rand -base64 48 > secrets/db_password
openssl rand -hex 48 > secrets/admin_api_key
printf '{"viewer":"%s","operator":"%s","admin":"%s"}\n' \
  "$(openssl rand -hex 48)" "$(openssl rand -hex 48)" "$(openssl rand -hex 48)" \
  > secrets/backend_keys.json
printf '%s' 'replace-with-operator-name' > secrets/web_username
openssl rand -base64 48 > secrets/web_password
openssl rand -hex 48 > secrets/web_session_secret
chmod 600 secrets/*
```

Never commit, print, or copy these values into Compose or environment templates.
