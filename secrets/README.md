# Runtime secrets

Create these files locally before deployment. They are excluded from Git.

- `db_password`: strong PostgreSQL password
- `admin_api_key`: random NanoDelta administrative API key

Use restrictive permissions:

```bash
install -m 700 -d secrets
openssl rand -base64 48 > secrets/db_password
openssl rand -hex 48 > secrets/admin_api_key
chmod 600 secrets/*
```

Never commit, print, or copy these values into Compose or environment templates.
