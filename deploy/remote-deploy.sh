#!/usr/bin/env bash
set -Eeuo pipefail

: "${API_IMAGE:?API_IMAGE is required}"
: "${WEB_IMAGE:?WEB_IMAGE is required}"

compose=(docker compose --env-file env/.env.production -f docker-compose.yml -f docker-compose.production.yml)
state_dir=.deployment
current_file="$state_dir/current-images.env"
previous_file="$state_dir/previous-images.env"

mkdir -p "$state_dir" backups
if [[ -f "$current_file" ]]; then
  cp "$current_file" "$previous_file"
fi

rollback() {
  local exit_code=$?
  echo "Deployment failed; attempting application-image rollback" >&2
  if [[ -f "$previous_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$previous_file"
    set +a
    "${compose[@]}" up -d --no-build api web || true
  fi
  exit "$exit_code"
}
trap rollback ERR

scripts/backup.sh backups
"${compose[@]}" pull api web
"${compose[@]}" run --rm migrate
"${compose[@]}" up -d --no-build api web
scripts/verify-deployment.sh

printf 'API_IMAGE=%q\nWEB_IMAGE=%q\n' "$API_IMAGE" "$WEB_IMAGE" > "$current_file"
trap - ERR
echo "Deployment verified"
