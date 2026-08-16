#!/bin/sh
set -eu
destination=${1:-backups}
mkdir -p "$destination"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$destination/nanodelta-$timestamp.dump"
temporary="$target.tmp"
trap 'rm -f "$temporary"' EXIT
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-nanodelta}" -d "${POSTGRES_DB:-nanodelta}" --format=custom --no-owner > "$temporary"
test -s "$temporary"
mv "$temporary" "$target"
sha256sum "$target" > "$target.sha256"
echo "$target"
