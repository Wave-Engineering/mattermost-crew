#!/usr/bin/env bash
# CI ONLY: start one broker instance against the throwaway Mattermost, laid out as an install
# (Dev Spec §5.B step 4), so `make verify-install` checks a real broker.
#
#   scripts/ci/two-user-setup.sh && scripts/ci/mattermost-up.sh   # first
#   scripts/ci/broker-up.sh
#
# It creates a broker account and a human partner on the server, gives the broker the patched
# role (scripts/ci/broker-role-patch.sh), writes /etc/crewbroker/<instance>.toml and
# .credential (0600, crewbroker), creates /run/crewbroker from packaging/crewbroker.tmpfiles.conf
# and the state directory (0700, crewbroker), and starts `crewbroker serve` as crewbroker.
# It exports CREWBROKER_INSTANCES and CREWBROKER_BIN for verify-install (into $GITHUB_ENV under
# Actions, and as export lines on stdout). It needs root or sudo: never run it on a workstation.
set -euo pipefail

: "${MM_URL:?source the env file from scripts/ci/mattermost-up.sh}"
: "${MM_ADMIN_TOKEN:?source the env file from scripts/ci/mattermost-up.sh}"
CREW="${BROKER_CI_CREW:-ci}"
SERVER_ID="${BROKER_CI_SERVER_ID:-local}"
INSTANCE="${CREW}-${SERVER_ID}"
LOG="${BROKER_CI_LOG:-${PWD}/reports/crewbroker-${INSTANCE}.log}"
API="${MM_URL%/}/api/v4"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="${ROOT}/.venv/bin/crewbroker"

log() { echo "broker-up: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

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

(cd "$ROOT" && uv sync --frozen -q)
[[ -x "$BIN" ]] || die "$BIN is missing"

# The service users must reach the workspace's venv and its interpreter (CI runners keep both
# under the runner's home). Give every ancestor directory traverse (o+x), nothing more.
for target in "$BIN" "$(readlink -f "${ROOT}/.venv/bin/python")"; do
  dir="$(dirname "$(readlink -f "$target")")"
  while [[ "$dir" != / ]]; do
    $SUDO chmod o+x "$dir"
    dir="$(dirname "$dir")"
  done
done
sudo -u crewbroker test -x "$BIN" || die "crewbroker cannot run $BIN"

log "creating the broker account and the human partner"
broker_id="$(mkuser "crewbroker-${INSTANCE}")"
partner_id="$(mkuser "partner-${INSTANCE}")"
"${ROOT}/scripts/ci/broker-role-patch.sh" "$broker_id"
credential="$(api POST "/users/${broker_id}/tokens" '{"description": "crewbroker CI"}' | jq -r .token)"
[[ -n "$credential" && "$credential" != null ]] || die "no access token for the broker account"
[[ -n "${GITHUB_ENV:-}" ]] && echo "::add-mask::${credential}"

log "writing /etc/crewbroker/${INSTANCE}.toml and .credential"
$SUDO install -d -m 0755 /etc/crewbroker
printf 'crew = "%s"\nserver_id = "%s"\nserver_url = "%s"\nhuman_user_ids = ["%s"]\ntrusted = []\n' \
  "$CREW" "$SERVER_ID" "$MM_URL" "$partner_id" \
  | $SUDO install -m 0644 -o root -g root /dev/stdin "/etc/crewbroker/${INSTANCE}.toml"
printf '%s\n' "$credential" \
  | $SUDO install -m 0600 -o crewbroker -g crewbroker /dev/stdin "/etc/crewbroker/${INSTANCE}.credential"

log "creating /run/crewbroker and the state directory"
$SUDO install -m 0644 "${ROOT}/packaging/crewbroker.tmpfiles.conf" /etc/tmpfiles.d/crewbroker.conf
$SUDO systemd-tmpfiles --create /etc/tmpfiles.d/crewbroker.conf
$SUDO install -d -m 0755 /var/lib/crewbroker
$SUDO install -d -m 0700 -o crewbroker -g crewbroker "/var/lib/crewbroker/${INSTANCE}"

log "starting crewbroker serve ${INSTANCE}; log in ${LOG}"
mkdir -p "$(dirname "$LOG")"
# shellcheck disable=SC2024  # the log is the runner's file, written by this shell
nohup sudo -u crewbroker -- "$BIN" serve "$INSTANCE" >"$LOG" 2>&1 &
sock="/run/crewbroker/${INSTANCE}.sock"
for _ in $(seq 1 30); do
  $SUDO test -S "$sock" && break
  sleep 1
done
$SUDO test -S "$sock" || { cat "$LOG" >&2; die "no socket at $sock after 30s"; }
log "broker ${INSTANCE} is up on $sock"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "CREWBROKER_INSTANCES=${CREW}:${SERVER_ID}"
    echo "CREWBROKER_BIN=${BIN}"
  } >>"$GITHUB_ENV"
fi
echo "export CREWBROKER_INSTANCES='${CREW}:${SERVER_ID}'"
echo "export CREWBROKER_BIN='${BIN}'"
