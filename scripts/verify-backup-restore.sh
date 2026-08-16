#!/bin/sh
set -eu

# Destructive restore verification is isolated in a disposable Compose project
# and never targets the configured production volume.
container="nanodelta-restore-drill-$$"
workdir=$(mktemp -d)
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$workdir"
}
trap cleanup EXIT INT TERM

dump=${1:?usage: scripts/verify-backup-restore.sh backups/file.dump}
test -f "$dump"
test -f "$dump.sha256"
sha256sum -c "$dump.sha256"

cp "$dump" "$workdir/restore.dump"
docker run -d --name "$container" \
  -e POSTGRES_DB=nanodelta_restore -e POSTGRES_USER=nanodelta_restore \
  -e POSTGRES_PASSWORD=restore-drill-only timescale/timescaledb:2.17.2-pg16 >/dev/null
until docker exec "$container" pg_isready -U nanodelta_restore -d nanodelta_restore >/dev/null 2>&1; do
  sleep 1
done
docker exec -i "$container" pg_restore \
  -U nanodelta_restore -d nanodelta_restore --clean --if-exists --no-owner \
  < "$workdir/restore.dump"
docker exec "$container" psql \
  -U nanodelta_restore -d nanodelta_restore -v ON_ERROR_STOP=1 \
  -c 'SELECT count(*) FROM control.schema_migrations;'
echo "isolated restore verification passed"
