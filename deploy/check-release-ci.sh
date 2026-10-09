#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${GITHUB_EVENT_NAME:-} == workflow_dispatch && ${GITHUB_REF:-} == refs/heads/main ]] || {
  echo 'Production deployment must be manually dispatched from main.' >&2
  exit 1
}
[[ ${GITHUB_REPOSITORY,,} == example-org/exampleapp && $GITHUB_SHA =~ ^[a-f0-9]{40}$ ]]
runs=$(gh api --method GET "repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/runs" \
  -f "head_sha=$GITHUB_SHA" -f branch=main -f event=push -f per_page=100)
# Check the newest run for this exact commit, not a successful run on another SHA
# or an earlier success followed by a failed/in-progress rerun.
python3 -c '
import json, sys
runs = [run for run in json.load(sys.stdin)["workflow_runs"]
        if run["head_sha"] == sys.argv[1] and run["head_branch"] == "main" and run["event"] == "push"]
latest = max(runs, key=lambda run: run["id"], default={})
sys.exit(0 if latest.get("status") == "completed" and latest.get("conclusion") == "success" else 1)
' "$GITHUB_SHA" <<< "$runs" || {
  echo 'The selected commit does not have a completed successful main CI run. Wait for CI or fix it before deploying.' >&2
  exit 1
}
printf 'Verified green CI for %s\n' "$GITHUB_SHA"
