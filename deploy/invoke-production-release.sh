#!/usr/bin/env bash
# Runner-side transport only. Deployment behavior lives in deploy-production.sh.
set -Eeuo pipefail
umask 077
[[ $IMAGE_REFERENCE =~ ^ghcr\.io/example-org/exampleapp@sha256:[a-f0-9]{64}$ ]]
[[ $GITHUB_SHA =~ ^[a-f0-9]{40}$ && $GITHUB_RUN_ID =~ ^[0-9]+$ && $GITHUB_RUN_ATTEMPT =~ ^[0-9]+$ ]]
[[ $PRODUCTION_SSH_HOST =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ && $PRODUCTION_SSH_USER =~ ^[a-z_][a-z0-9_-]*$ ]]
[[ -n $PRODUCTION_SSH_PRIVATE_KEY && -n $PRODUCTION_SSH_KNOWN_HOSTS ]]
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
printf '%s\n' "$PRODUCTION_SSH_PRIVATE_KEY" > "$temporary/key"
printf '%s\n' "$PRODUCTION_SSH_KNOWN_HOSTS" > "$temporary/known_hosts"
unset PRODUCTION_SSH_PRIVATE_KEY PRODUCTION_SSH_KNOWN_HOSTS
ssh-keygen -y -P '' -f "$temporary/key" > /dev/null
# Stable alias binds the trusted host key regardless of tailnet DNS/IP changes.
ssh-keygen -F app-prod -f "$temporary/known_hosts" > /dev/null
tar -C "$root" -cf "$temporary/tools.tar" \
  deploy/deploy-production.sh deploy/production_context.py deploy/lib/image-deployment.sh
ssh -F /dev/null -T -i "$temporary/key" \
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$temporary/known_hosts" -o GlobalKnownHostsFile=/dev/null \
  -o HostKeyAlias=app-prod -o HostKeyAlgorithms=ssh-ed25519 \
  -o ConnectTimeout=30 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
  "$PRODUCTION_SSH_USER@$PRODUCTION_SSH_HOST" \
  "deploy-production $GITHUB_SHA $GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT $IMAGE_REFERENCE" < "$temporary/tools.tar"
