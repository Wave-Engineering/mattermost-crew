#!/usr/bin/env bash
# CI ONLY: the runbook's "Configure" step for IT-20, with this CI host's values.
#
#   source .mattermost.env            # MM_URL, MM_ADMIN_TOKEN from scripts/ci/mattermost-up.sh
#   CREWCHAT_RUNBOOK_IT=1 CREWBROKER_INSTANCES=ci:local scripts/ci/runbook-configure.sh
#
# docs/runbook.md has the operator write the configuration before installing. On a fresh CI host
# this script stands in for that operator: it creates the agents' OS user (a Prerequisite), a
# broker account and a human partner on the throwaway server, gives the broker the patched role
# (scripts/ci/broker-role-patch.sh; on a real server that is the partner's yes-prod, CP-03), and
# writes, exactly where and as the runbook says, all root-owned:
#   /etc/crewchat/servers.toml                       0644
#   /etc/crewbroker/<crew>-<server_id>.toml          0644
#   /etc/crewbroker/<crew>-<server_id>.credential    0600 (the install hands it to crewbroker)
#   /etc/crewkiosk/model.credential                  0600 (a dummy; the install hands it over)
#   /etc/crewkiosk/broker-model.credential           0600 (a second dummy, the broker's)
# It creates no service user: the runbook's install blocks must do that themselves.
#
# It refuses to run unless CREWCHAT_RUNBOOK_IT=1: it changes the host, so it only runs on a
# throwaway CI host (tests/integration/test_runbook.py sets it in the IT-20 job).
set -euo pipefail

log() { echo "runbook-configure: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

[[ "${CREWCHAT_RUNBOOK_IT:-}" == 1 ]] || die "CI only: set CREWCHAT_RUNBOOK_IT=1 on a throwaway host"
: "${MM_URL:?source the env file from scripts/ci/mattermost-up.sh}"
: "${MM_ADMIN_TOKEN:?source the env file from scripts/ci/mattermost-up.sh}"
: "${CREWBROKER_INSTANCES:?one broker, as <crew>:<server_id>}"
AGENT_USER="${AGENT_USER:-agent}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
API="${MM_URL%/}/api/v4"

entry="${CREWBROKER_INSTANCES%% *}"
[[ "$entry" == "$CREWBROKER_INSTANCES" ]] || die "IT-20 configures exactly one broker"
crew="${entry%%:*}"
server_id="${entry#*:}"
[[ -n "$crew" && -n "$server_id" && "$entry" == *:* ]] || die "bad CREWBROKER_INSTANCES '$entry'"
instance="${crew}-${server_id}"

SUDO=""
[[ "$(id -u)" -eq 0 ]] || SUDO="sudo"

api() { # api METHOD PATH [JSON]
  local args=(-fsS -X "$1" -H "Authorization: Bearer ${MM_ADMIN_TOKEN}" -H 'Content-Type: application/json')
  [[ -n "${3:-}" ]] && args+=(--data "$3")
  curl "${args[@]}" "${API}$2"
}

mkuser() { # mkuser USERNAME -> user id
  local pw
  pw="Ci-pw-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')!"
  api POST /users "$(jq -nc --arg u "$1" --arg p "$pw" '{username: $u, email: ($u + "@example.com"), password: $p}')" \
    | jq -r .id
}

for user in crewchat crewbroker crewkiosk; do
  if getent passwd "$user" >/dev/null; then
    die "user $user already exists: IT-20 needs a fresh host, where the runbook creates it"
  fi
done

if ! getent passwd "$AGENT_USER" >/dev/null; then
  $SUDO useradd --create-home --shell /bin/bash "$AGENT_USER"
  log "created the agents' user, $AGENT_USER"
fi

log "creating the broker account and the human partner"
broker_id="$(mkuser "crewbroker-${instance}")"
partner_id="$(mkuser "partner-${instance}")"
"${ROOT}/scripts/ci/broker-role-patch.sh" "$broker_id"
credential="$(api POST "/users/${broker_id}/tokens" '{"description": "crewbroker IT-20"}' | jq -r .token)"
[[ -n "$credential" && "$credential" != null ]] || die "no access token for the broker account"
[[ -n "${GITHUB_ENV:-}" ]] && echo "::add-mask::${credential}"

log "writing /etc/crewbroker/${instance}.toml and .credential"
$SUDO install -d -m 0755 -o root -g root /etc/crewbroker
printf 'crew = "%s"\nserver_id = "%s"\nserver_url = "%s"\nhuman_user_ids = ["%s"]\ntrusted = []\n' \
  "$crew" "$server_id" "$MM_URL" "$partner_id" \
  | "${ROOT}/scripts/ci/put-file.sh" 0644 root:root "/etc/crewbroker/${instance}.toml"
printf '%s\n' "$credential" \
  | "${ROOT}/scripts/ci/put-file.sh" 0600 root:root "/etc/crewbroker/${instance}.credential"

log "writing /etc/crewchat/servers.toml"
$SUDO install -d -m 0755 -o root -g root /etc/crewchat
printf '%s\n' \
  "default_crew = \"${crew}\"" \
  "broker_timeout_s = 2" \
  "kiosk_rate_per_hour = 10" \
  "loop_guard_window = 20" \
  "my_threads_retention_days = 90" \
  "" \
  "[[mattermost]]" \
  "id = \"${server_id}\"" \
  "url = \"${MM_URL}\"" \
  "" \
  "[discord]" \
  "enabled = false" \
  "" \
  "[crews.${crew}]" \
  "needs_list = \"${crew}:needs\"" \
  "" \
  "[crews.${crew}.${server_id}]" \
  "broker_socket = \"/run/crewbroker/${instance}.sock\"" \
  "broker_user_id = \"${broker_id}\"" \
  | "${ROOT}/scripts/ci/put-file.sh" 0644 root:root /etc/crewchat/servers.toml

log "writing a dummy /etc/crewkiosk/model.credential"
$SUDO install -d -m 0755 -o root -g root /etc/crewkiosk
echo "sk-ci-dummy-kiosk-key" \
  | "${ROOT}/scripts/ci/put-file.sh" 0600 root:root /etc/crewkiosk/model.credential
# The broker's own key, a second dummy, for "Install kiosk research" (#142).
log "writing a dummy /etc/crewkiosk/broker-model.credential"
echo "sk-ci-dummy-broker-key" \
  | "${ROOT}/scripts/ci/put-file.sh" 0600 root:root /etc/crewkiosk/broker-model.credential

log "configured ${instance} against ${MM_URL}"
