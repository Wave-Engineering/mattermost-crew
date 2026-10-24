#!/usr/bin/env bash
# Remove the throwaway Mattermost server that scripts/ci/mattermost-up.sh started.
set -euo pipefail

MM_PREFIX="${MM_PREFIX:-mmcrew}"
MM_ENV_FILE="${MM_ENV_FILE:-.mattermost.env}"

docker rm -f "${MM_PREFIX}-mm" "${MM_PREFIX}-pg" >/dev/null 2>&1 || true
docker network rm "${MM_PREFIX}-net" >/dev/null 2>&1 || true
rm -f "$MM_ENV_FILE"
echo "mattermost-down: removed ${MM_PREFIX}-mm, ${MM_PREFIX}-pg and ${MM_PREFIX}-net" >&2
