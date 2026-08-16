#!/bin/sh
set -eu
api_url=${API_URL:-http://127.0.0.1:8000}
web_url=${WEB_URL:-http://127.0.0.1:3000}
curl --fail --silent --show-error "$api_url/health/live"
curl --fail --silent --show-error "$api_url/health/ready"
curl --fail --silent --show-error "$web_url/" >/dev/null
test "$(docker compose ps --status running --services runtime)" = "runtime"
docker compose exec -T db psql -U "${POSTGRES_USER:-nanodelta}" -d "${POSTGRES_DB:-nanodelta}" -v ON_ERROR_STOP=1 -c "SELECT extversion FROM pg_extension WHERE extname='timescaledb';"
docker compose exec -T db psql -U "${POSTGRES_USER:-nanodelta}" -d "${POSTGRES_DB:-nanodelta}" -v ON_ERROR_STOP=1 -c "SELECT version, applied_at FROM control.schema_migrations ORDER BY version;"
echo "NanoDelta deployment verification passed"
