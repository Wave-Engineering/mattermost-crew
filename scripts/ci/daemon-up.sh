#!/usr/bin/env bash
# CI ONLY: start the chat daemon against the broker that scripts/ci/broker-up.sh started, laid
# out as an install (Dev Spec §5.B step 2), so `make verify-install` and IT-01 check a real
# daemon running as crewchat.
#
#   scripts/ci/two-user-setup.sh && scripts/ci/mattermost-up.sh && scripts/ci/broker-up.sh
#   scripts/ci/daemon-up.sh
#
# It writes /etc/crewchat/servers.toml (0644, root) naming the one Mattermost server and the
# broker, creates /run/crewchat (0755, crewchat, with the agents' default ACL) and
# /var/lib/crewchat (0700, crewchat) as the unit's RuntimeDirectory, its agent-acl.conf drop-in
# and its StateDirectory would, and starts `crewchat serve` as crewchat.
# It exports CREWCHAT_BIN and CREWCHAT_SOCKET (into $GITHUB_ENV under Actions, and as export
# lines on stdout). It needs root or sudo: never run it on a workstation.
set -euo pipefail

: "${MM_URL:?source the env file from scripts/ci/mattermost-up.sh}"
: "${CREWBROKER_INSTANCES:?run scripts/ci/broker-up.sh first}"
: "${CREWBROKER_USER_ID:?run scripts/ci/broker-up.sh first}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="${ROOT}/.venv/bin/crewchat"
SOCK=/run/crewchat/crewchat.sock
LOG="${CREWCHAT_CI_LOG:-${PWD}/reports/crewchat.log}"
AGENT_USER="${AGENT_USER:-agent}"

log() { echo "daemon-up: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

SUDO=""
[[ "$(id -u)" -eq 0 ]] || SUDO="sudo"

entry="${CREWBROKER_INSTANCES%% *}"
crew="${entry%%:*}"
server_id="${entry#*:}"
[[ -n "$crew" && -n "$server_id" && "$entry" == *:* ]] || die "bad CREWBROKER_INSTANCES '$entry'"

[[ -x "$BIN" ]] || die "$BIN is missing; run uv sync first"
# broker-up.sh gave every ancestor of the venv traverse; both users must be able to run it.
sudo -u crewchat test -x "$BIN" || die "crewchat cannot run $BIN"
sudo -u "$AGENT_USER" test -x "$BIN" || die "$AGENT_USER cannot run $BIN"

log "writing /etc/crewchat/servers.toml"
$SUDO install -d -m 0755 /etc/crewchat
cat <<EOF | $SUDO install -m 0644 -o root -g root /dev/stdin /etc/crewchat/servers.toml
default_crew = "${crew}"
broker_timeout_s = 2

[[mattermost]]
id = "${server_id}"
url = "${MM_URL}"

[discord]
enabled = false

[crews.${crew}]
needs_list = "${crew}:needs"

[crews.${crew}.${server_id}]
broker_socket = "/run/crewbroker/${crew}-${server_id}.sock"
broker_user_id = "${CREWBROKER_USER_ID}"
EOF

log "creating /run/crewchat and /var/lib/crewchat"
$SUDO install -d -m 0755 -o crewchat -g crewchat /run/crewchat
# The default ACL the runbook's agent-acl.conf drop-in sets in ExecStartPre (#115).
"${ROOT}/scripts/ci/host-prereqs.sh"
$SUDO setfacl -d -m "u:${AGENT_USER}:rw,g::rw" /run/crewchat
$SUDO install -d -m 0700 -o crewchat -g crewchat /var/lib/crewchat

log "starting crewchat serve; log in ${LOG}"
mkdir -p "$(dirname "$LOG")"
# No new privileges, as packaging/crewchat.service runs it (D-107): the kiosk must still answer.
# shellcheck disable=SC2024  # the log is the runner's file, written by this shell
nohup sudo -u crewchat -- setpriv --no-new-privs "$BIN" serve >"$LOG" 2>&1 &
for _ in $(seq 1 30); do
  $SUDO test -S "$SOCK" && break
  sleep 1
done
$SUDO test -S "$SOCK" || { cat "$LOG" >&2; die "no socket at $SOCK after 30s"; }
log "crewchat is up on $SOCK"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "CREWCHAT_BIN=${BIN}"
    echo "CREWCHAT_SOCKET=${SOCK}"
  } >>"$GITHUB_ENV"
fi
echo "export CREWCHAT_BIN='${BIN}'"
echo "export CREWCHAT_SOCKET='${SOCK}'"
