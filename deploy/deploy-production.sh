#!/usr/bin/env bash
# Run on app-prod, manually or through the restricted release receiver.
set -Eeuo pipefail
umask 077

if [[ $# != 1 || ! $1 =~ ^ghcr\.io/example-org/exampleapp@sha256:[a-f0-9]{64}$ ]]; then
  echo 'Usage: bash deploy/deploy-production.sh ghcr.io/example-org/exampleapp@sha256:<64 lowercase hex characters>' >&2
  exit 2
fi
image=$1
tool_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Versioned tooling may live outside the existing Compose checkout.
root=$(cd -- "${JOINTOPS_DEPLOY_ROOT:-$tool_dir/..}" && pwd)
[[ -f $root/.env.production ]] || { echo 'Missing existing .env.production' >&2; exit 2; }
for command in docker python3 flock sha256sum; do
  command -v "$command" > /dev/null
done
# A host-wide lock survives checkout changes and serializes manual deployments.
state=${JOINTOPS_DEPLOY_STATE_DIR:-/opt/jointops/backups/deployments}
mkdir -p -- "$state"
state=$(realpath -- "$state")
exec 9>"$state/deploy.lock"
flock -n 9 || { echo 'A production deployment is already running.' >&2; exit 1; }
attempt=$(mktemp -d "$state/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
record="$attempt/result.txt"
backup="$attempt/database.dump"
printf 'requested_image=%s\n' "$image" > "$record"
phase=preflight
on_error() {
  local status=$1
  trap - ERR INT TERM
  printf 'failed_phase=%s\n' "$phase" >> "$record"
  if [[ $phase == start || $phase == health ]]; then
    "${compose[@]}" stop jointops-app || true
  fi
  printf 'Production deployment failed during %s. No automatic rollback. Review %s\n' "$phase" "$attempt" >&2
  exit "$status"
}
trap 'on_error $?' ERR
trap 'on_error 130' INT
trap 'on_error 143' TERM
export JOINTOPS_PRODUCTION_IMAGE=$image
cd -- "$root"
# Read-only discovery/validation; metadata excludes environment values/secrets.
python3 "$tool_dir/production_context.py" "$root" "$attempt"
mapfile -d '' -t compose < "$attempt/compose.args"
# shellcheck source=deploy/lib/image-deployment.sh
source "$tool_dir/lib/image-deployment.sh"

pull_candidate
phase=retain_previous
previous_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["previous_image_id"])' "$attempt/metadata.json")
rollback_tag="jointops365:rollback-$(basename -- "$attempt")"
docker image tag "$previous_id" "$rollback_tag"
printf 'rollback_local_tag=%s\n' "$rollback_tag" >> "$record"

phase=stop
"${compose[@]}" stop jointops-app
backup_database
migrate_candidate
start_candidate
printf 'Production deployed: %s\nBackup and deployment metadata: %s\n' "$image" "$attempt"
