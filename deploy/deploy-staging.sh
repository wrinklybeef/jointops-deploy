#!/usr/bin/env bash
# Manual use on the staging VM only. No source build or remote server access.
set -Eeuo pipefail
umask 077

if [[ $# -lt 3 ]]; then
  echo "Usage: bash deploy/deploy-staging.sh GHCR_IMAGE@sha256:DIGEST EXISTING_PROJECT ENV_FILE [EXISTING_OVERRIDE.yml ...]" >&2
  exit 2
fi
image=$1
project=$2
env_file=$(realpath -- "$3")
shift 3
if [[ ! $image =~ ^ghcr\.io/example-org/exampleapp@sha256:[a-f0-9]{64}$ ]]; then
  echo 'Use ghcr.io/example-org/exampleapp@sha256:<64 lowercase hex characters>; tags are not accepted.' >&2
  exit 2
fi
if [[ ! $project =~ ^[a-z0-9][a-z0-9_-]*$ || ! -f $env_file ]]; then
  echo 'Provide the existing staging Compose project name and an existing env file.' >&2
  exit 2
fi
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
compose=(docker compose --project-directory "$root" --project-name "$project" --env-file "$env_file" -f "$root/compose.yml")
for override in "$@"; do
  [[ -f $override ]] || { echo "Missing override: $override" >&2; exit 2; }
  compose+=(-f "$(realpath -- "$override")")
done
compose+=(-f "$root/compose.staging.yml")
export JOINTOPS_STAGING_IMAGE=$image
cd -- "$root"
# shellcheck source=deploy/lib/image-deployment.sh
source "$root/deploy/lib/image-deployment.sh"

# Keep one operator/migration process per project in this checkout.
state="$root/.staging-deploy/$project"
mkdir -p -- "$state"
exec 9>"$state/deploy.lock"
flock -n 9 || { echo 'A staging deployment is already running for this project.' >&2; exit 1; }
record="$state/$(date -u +%Y%m%dT%H%M%SZ)-$$.txt"
printf 'requested_image=%s\nproject=%s\n' "$image" "$project" > "$record"
phase=preflight
on_error() {
  local status=$1
  printf 'failed_phase=%s\n' "$phase" >> "$record"
  printf 'Staging deployment failed during %s. No automatic rollback. Record: %s\n' "$phase" "$record" >&2
  # An app that failed startup/health must not continue serving traffic.
  if [[ $phase == start || $phase == health ]]; then
    "${compose[@]}" stop jointops-app || true
  fi
  exit "$status"
}
trap 'on_error $?' ERR
trap 'on_error 130' INT
trap 'on_error 143' TERM

# Validate without displaying expanded secrets. Unsupported !reset fails here.
"${compose[@]}" config --quiet > /dev/null 2>&1 || {
  status=$?
  echo 'Unable to validate staging Compose configuration.' >&2
  on_error "$status"
}
pull_candidate

phase=database
# Keep the existing PostgreSQL container, volume, and networks; never upgrade it.
"${compose[@]}" up -d --no-deps --no-recreate --no-build --wait --wait-timeout 120 postgres
previous=$("${compose[@]}" ps -a -q jointops-app)
if [[ -n $previous ]]; then
  docker inspect --format '{{printf "previous_image=%s\nprevious_image_id=%s" .Config.Image .Image}}' "$previous" >> "$record"
fi

phase=stop
"${compose[@]}" stop jointops-app
backup="${record%.txt}.dump"
backup_database
migrate_candidate
start_candidate
printf 'Staging deployed: %s\nDeployment record and pre-migration backup: %s\n' "$image" "$record"
