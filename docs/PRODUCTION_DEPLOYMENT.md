# Production deployment foundation

This checkpoint provides a reproducible single-host deployment for the NanoDelta API, UI, and TimescaleDB. It is a foundation, not evidence that realtime trading has passed production acceptance.

## Supported topology

A dedicated Ubuntu VPS runs Docker Engine and Compose. TimescaleDB, API, migration, and UI containers use a private Compose network. Database, API, and UI ports bind to loopback; expose the application through a separately managed TLS reverse proxy.

## Prerequisites

- Ubuntu 24.04 LTS or equivalent
- Docker Engine 27+ and Compose v2
- 4 CPU, 8 GB RAM, 100 GB SSD minimum for initial paper operation
- firewall allowing only SSH and the reverse-proxy ports
- persistent off-host backup destination

## Configure

```bash
cp env/.env.production.example .env
install -m 700 -d secrets backups
openssl rand -base64 48 > secrets/db_password
openssl rand -hex 48 > secrets/admin_api_key
chmod 600 secrets/*
```

Keep the existing market provider credentials in protected files according to their market-specific documentation. Do not add them to Compose until the corresponding runtime worker checkpoint consumes them.

## Deploy

```bash
docker compose config
docker compose build --pull
docker compose up -d db
docker compose run --rm migrate
docker compose up -d api web
docker compose ps
scripts/verify-deployment.sh
```

The API exposes unauthenticated liveness and readiness endpoints. Business and administrative write endpoints continue to require `X-API-Key`.

- `GET /health/live`: process is alive
- `GET /health/ready`: API can execute a database query

## Upgrade and rollback

1. Create and verify a backup.
2. Pull the intended immutable Git commit.
3. Build images from that commit.
4. Run the one-shot migration container.
5. Restart API and UI.
6. Run deployment verification.

Database migrations are forward-only. Rollback means redeploying the prior application commit against a schema that remains backward compatible. Any destructive schema change requires an independently reviewed restore plan.

## Backup and restore verification

```bash
scripts/backup.sh
scripts/restore.sh backups/nanodelta-YYYYMMDDTHHMMSSZ.dump
scripts/verify-deployment.sh
```

A backup is not accepted until its checksum passes and it has been restored successfully into a disposable verification database or isolated recovery environment. Never test restore over the only production database.

Schedule daily backups externally and copy them off-host. Retention policy: 7 daily, 4 weekly, and 12 monthly copies.

## Security boundaries

- containers run as non-root where supported;
- API and UI filesystems are read-only;
- secrets are mounted read-only from files;
- TimescaleDB is not exposed beyond loopback;
- no live-broker execution exists;
- terminate TLS at a maintained reverse proxy;
- restrict SSH, enable unattended security updates, and rotate secrets.

## Acceptance evidence

Before calling this deployment production-ready, retain:

- `docker compose config` output with secrets redacted;
- image digests and Git commit;
- successful migration log;
- deployment-verification output;
- backup checksum;
- isolated restore-verification output;
- resource and disk baseline.

Realtime workers, CI/CD, observability, soak/failover tests, API-backed UI data, and end-to-end paper sessions belong to later checkpoints and are not claimed here.
