#!/usr/bin/env bash
# Start a throwaway Mattermost server for the integration tests, set up like the dev space.
#
#   MM_IMAGE=mattermost/mattermost-team-edition:11.11.3 scripts/ci/mattermost-up.sh
#
# MM_IMAGE is one of the three CI legs (CT-01, CT-11):
#   mattermost/mattermost-team-edition:11.11.3
#   mattermost/mattermost-enterprise-edition:11.11.3   (unlicensed: the stand-in for Entry)
#   mattermost/mattermost-team-edition:11.8.<latest patch, pinned in ci.yml>
#
# It starts Postgres and the server on a private docker network, waits for
# /api/v4/system/ping, creates the first user (who becomes the system admin), sets
# EnableBotAccountCreation and EnableUserAccessTokens, applies the dev space's rate limits
# (per user, 10/s, burst 50; CT-06), and mints the admin a personal access token.
#
# It exports MM_URL and MM_ADMIN_TOKEN three ways, because a script can't export into its
# caller's shell:
#   - into $GITHUB_ENV, when run under GitHub Actions (the token is masked first);
#   - into $MM_ENV_FILE (default .mattermost.env, gitignored), for `source .mattermost.env`;
#   - as `export` lines on stdout, for `eval "$(scripts/ci/mattermost-up.sh)"`.
# Everything else goes to stderr. scripts/ci/mattermost-down.sh removes it all.
set -euo pipefail

: "${MM_IMAGE:?set MM_IMAGE to the Mattermost image to start}"
MM_PORT="${MM_PORT:-8065}"
MM_PREFIX="${MM_PREFIX:-mmcrew}"
MM_ENV_FILE="${MM_ENV_FILE:-.mattermost.env}"
MM_PG_IMAGE="${MM_PG_IMAGE:-postgres:16-alpine}"
MM_READY_TIMEOUT="${MM_READY_TIMEOUT:-240}"

ADMIN_USER="${MM_ADMIN_USER:-crewadmin}"
ADMIN_EMAIL="${MM_ADMIN_EMAIL:-crewadmin@example.com}"
# A throwaway container's password: it never leaves this host and dies with the container.
ADMIN_PASSWORD="${MM_ADMIN_PASSWORD:-Crew-admin-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')!}"

NET="${MM_PREFIX}-net"
PG="${MM_PREFIX}-pg"
MM="${MM_PREFIX}-mm"
MM_URL="http://localhost:${MM_PORT}"
API="${MM_URL}/api/v4"

log() { echo "mattermost-up: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

for tool in docker curl jq; do
  command -v "$tool" >/dev/null || die "$tool is required"
done

if docker container inspect "$MM" >/dev/null 2>&1; then
  die "container $MM already exists; run scripts/ci/mattermost-down.sh first"
fi

log "starting $MM_IMAGE on $MM_URL"
docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" \
  -e POSTGRES_USER=mmuser -e POSTGRES_PASSWORD=mmuser-pw -e POSTGRES_DB=mattermost \
  "$MM_PG_IMAGE" >/dev/null

# The rate limits are startup settings (a change needs a restart), so they are set here. The
# environment locks them in the System Console, which is what we want: the dev space's limits
# are fixed, and tests must live within them.
docker run -d --name "$MM" --network "$NET" -p "127.0.0.1:${MM_PORT}:8065" \
  -e MM_SQLSETTINGS_DRIVERNAME=postgres \
  -e "MM_SQLSETTINGS_DATASOURCE=postgres://mmuser:mmuser-pw@${PG}:5432/mattermost?sslmode=disable&connect_timeout=10" \
  -e "MM_SERVICESETTINGS_SITEURL=${MM_URL}" \
  -e MM_RATELIMITSETTINGS_ENABLE=true \
  -e MM_RATELIMITSETTINGS_PERSEC=10 \
  -e MM_RATELIMITSETTINGS_MAXBURST=50 \
  -e MM_RATELIMITSETTINGS_VARYBYUSER=true \
  -e MM_RATELIMITSETTINGS_VARYBYREMOTEADDR=false \
  "$MM_IMAGE" >/dev/null

log "waiting up to ${MM_READY_TIMEOUT}s for /api/v4/system/ping"
deadline=$((SECONDS + MM_READY_TIMEOUT))
until curl -fsS "${API}/system/ping" 2>/dev/null | jq -e '.status == "OK"' >/dev/null; do
  if ((SECONDS >= deadline)); then
    docker logs --tail 50 "$MM" >&2 || true
    die "server did not answer ping within ${MM_READY_TIMEOUT}s"
  fi
  if [[ "$(docker container inspect -f '{{.State.Running}}' "$MM")" != "true" ]]; then
    docker logs --tail 50 "$MM" >&2 || true
    die "container $MM exited"
  fi
  sleep 2
done
log "server is up: $(curl -fsS -D - -o /dev/null "${API}/system/ping" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-version-id"{print $2}')"

# api METHOD PATH [JSON] -- prints the body; fails on any non-2xx.
api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-fsS -X "$method" -H 'Content-Type: application/json')
  [[ -n "${SESSION_TOKEN:-}" ]] && args+=(-H "Authorization: Bearer ${SESSION_TOKEN}")
  [[ -n "$body" ]] && args+=(--data "$body")
  curl "${args[@]}" "${API}${path}"
}

log "creating the admin, $ADMIN_USER"
user_json="$(jq -n --arg u "$ADMIN_USER" --arg e "$ADMIN_EMAIL" --arg p "$ADMIN_PASSWORD" \
  '{username: $u, email: $e, password: $p}')"
admin_id="$(api POST /users "$user_json" | jq -r .id)"

login_json="$(jq -n --arg u "$ADMIN_USER" --arg p "$ADMIN_PASSWORD" '{login_id: $u, password: $p}')"
SESSION_TOKEN="$(curl -fsS -D - -o /dev/null -H 'Content-Type: application/json' \
  --data "$login_json" "${API}/users/login" | tr -d '\r' | awk -F': ' 'tolower($1)=="token"{print $2}')"
[[ -n "$SESSION_TOKEN" ]] || die "login as $ADMIN_USER returned no token"

roles="$(api GET /users/me | jq -r .roles)"
[[ " $roles " == *" system_admin "* ]] || die "$ADMIN_USER is not a system admin (roles: $roles)"

log "enabling bot accounts and personal access tokens"
api PUT /config/patch \
  '{"ServiceSettings": {"EnableBotAccountCreation": true, "EnableUserAccessTokens": true}}' \
  >/dev/null

MM_ADMIN_TOKEN="$(api POST "/users/${admin_id}/tokens" '{"description": "mattermost-crew CI admin"}' \
  | jq -r .token)"
[[ -n "$MM_ADMIN_TOKEN" && "$MM_ADMIN_TOKEN" != "null" ]] || die "no admin access token"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "::add-mask::${MM_ADMIN_TOKEN}"
  {
    echo "MM_URL=${MM_URL}"
    echo "MM_ADMIN_TOKEN=${MM_ADMIN_TOKEN}"
  } >>"$GITHUB_ENV"
fi

umask 077
{
  echo "export MM_IMAGE='${MM_IMAGE}'"
  echo "export MM_URL='${MM_URL}'"
  echo "export MM_ADMIN_TOKEN='${MM_ADMIN_TOKEN}'"
} | tee "$MM_ENV_FILE"
log "ready: MM_URL=${MM_URL}; MM_URL and MM_ADMIN_TOKEN are in ${MM_ENV_FILE}"
