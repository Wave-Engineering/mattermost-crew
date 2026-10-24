#!/usr/bin/env bash
# CI ONLY: give a broker account the patched system_user_access_token role (R-06).
#
#   source .mattermost.env        # MM_URL, MM_ADMIN_TOKEN from scripts/ci/mattermost-up.sh
#   scripts/ci/broker-role-patch.sh <broker_user_id>
#
# It adds create_bot, read_bots and manage_bots to system_user_access_token, then sets the
# broker account's roles to exactly "system_user system_user_access_token": no admin role.
#
# On a real server this step is the human partner's yes-prod (CP-03) and is never automated.
# The script refuses any MM_URL that is not a localhost throwaway, unless
# BROKER_ROLE_PATCH_ALLOW_REMOTE=1 is set for a CI service container.
set -euo pipefail

: "${MM_URL:?source the env file from scripts/ci/mattermost-up.sh}"
: "${MM_ADMIN_TOKEN:?source the env file from scripts/ci/mattermost-up.sh}"
BROKER_ID="${1:?usage: broker-role-patch.sh <broker_user_id>}"
API="${MM_URL%/}/api/v4"
PERMS='["create_bot","read_bots","manage_bots"]'
ROLES="system_user system_user_access_token"

log() { echo "broker-role-patch: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

case "$MM_URL" in
  http://localhost:* | http://127.0.0.1:*) ;;
  *) [[ "${BROKER_ROLE_PATCH_ALLOW_REMOTE:-}" == 1 ]] || die "refusing non-local MM_URL $MM_URL (CP-03)" ;;
esac

api() { # api METHOD PATH [JSON]
  local args=(-fsS -X "$1" -H "Authorization: Bearer ${MM_ADMIN_TOKEN}" -H 'Content-Type: application/json')
  [[ -n "${3:-}" ]] && args+=(--data "$3")
  curl "${args[@]}" "${API}$2"
}

role="$(api GET /roles/name/system_user_access_token)"
role_id="$(jq -r .id <<<"$role")"
perms="$(jq -c --argjson add "$PERMS" '.permissions + $add | unique' <<<"$role")"
patched="$(api PUT "/roles/${role_id}/patch" "$(jq -nc --argjson p "$perms" '{permissions: $p}')")"
jq -e --argjson want "$PERMS" '.permissions as $have | all($want[]; . as $w | $have | index($w))' \
  <<<"$patched" >/dev/null || die "role patch did not take: $(jq -c .permissions <<<"$patched")"
log "system_user_access_token now has create_bot, read_bots, manage_bots"

api PUT "/users/${BROKER_ID}/roles" "$(jq -nc --arg r "$ROLES" '{roles: $r}')" >/dev/null
got="$(api GET "/users/${BROKER_ID}" | jq -r .roles)"
[[ "$got" == "$ROLES" ]] || die "broker $BROKER_ID has roles '$got', wanted '$ROLES'"
log "broker $BROKER_ID has roles: $got"
