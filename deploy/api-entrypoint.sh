#!/bin/sh
set -eu
password_file="${POSTGRES_PASSWORD_FILE:-/run/secrets/db_password}"
if [ -z "${DATABASE_URL:-}" ]; then
  test -r "$password_file" || { echo "database password file is not readable" >&2; exit 1; }
  password=$(cat "$password_file")
  test -n "$password" || { echo "database password is empty" >&2; exit 1; }
  export DATABASE_URL="postgresql://${POSTGRES_USER:-nanodelta}:${password}@${POSTGRES_HOST:-db}:5432/${POSTGRES_DB:-nanodelta}"
fi
exec "$@"
