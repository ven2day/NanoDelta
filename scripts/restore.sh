#!/bin/sh
set -eu
dump=${1:?usage: scripts/restore.sh backups/file.dump}
test -f "$dump"
test -f "$dump.sha256"
sha256sum -c "$dump.sha256"
docker compose exec -T db pg_restore -U "${POSTGRES_USER:-nanodelta}" -d "${POSTGRES_DB:-nanodelta}" --clean --if-exists --no-owner < "$dump"
docker compose exec -T db psql -U "${POSTGRES_USER:-nanodelta}" -d "${POSTGRES_DB:-nanodelta}" -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM control.schema_migrations;"
