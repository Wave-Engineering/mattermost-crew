#!/usr/bin/env bash
# Print the throwaway Mattermost server's logs, for a failed CI run.
set -uo pipefail

MM_PREFIX="${MM_PREFIX:-mmcrew}"
docker logs --tail "${MM_LOG_LINES:-300}" "${MM_PREFIX}-mm" 2>&1 || echo "no ${MM_PREFIX}-mm container"
