#!/usr/bin/env bash
# Create the CI image's OS users and group, so tests can run as `agent` while the services run
# as their own users (that is what makes R-08 testable; Dev Spec §6.1, IT-01).
#
#   crewchat          the daemon. Primary group crewchat; also in crewchat-clients, so it can
#                     give its socket that group (/run/crewchat/crewchat.sock, 0660).
#   crewbroker        the broker. Its own group only; never in crewchat.
#   crewkiosk         the kiosk's CLI runner, if spike 1.2 picks it. An empty HOME, no
#                     credentials.
#   agent             the stand-in for an agent's user. In crewchat-clients, so it can reach
#                     the daemon's socket, and nothing else.
#   crewchat-clients  the group allowed on the daemon socket.
#
# It is idempotent, needs root (it uses sudo when not root), and is meant for CI runners and
# throwaway containers. Don't run it on a workstation you care about.
set -euo pipefail

SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  command -v sudo >/dev/null || { echo "two-user-setup: needs root or sudo" >&2; exit 1; }
  SUDO="sudo"
fi

log() { echo "two-user-setup: $*" >&2; }

ensure_group() {
  local group="$1"
  if getent group "$group" >/dev/null; then
    log "group $group exists"
  else
    $SUDO groupadd --system "$group"
    log "created group $group"
  fi
}

# ensure_user NAME SHELL [--system]
ensure_user() {
  local name="$1" shell="$2" system="${3:-}"
  if getent passwd "$name" >/dev/null; then
    log "user $name exists"
    return
  fi
  ensure_group "$name"
  # shellcheck disable=SC2086  # $system is empty or a single flag
  $SUDO useradd $system --gid "$name" --create-home --home-dir "/home/$name" \
    --shell "$shell" "$name"
  log "created user $name"
}

ensure_group crewchat-clients

ensure_user crewchat /usr/sbin/nologin --system
ensure_user crewbroker /usr/sbin/nologin --system
ensure_user crewkiosk /usr/sbin/nologin --system
ensure_user agent /bin/bash

$SUDO usermod -aG crewchat-clients crewchat
$SUDO usermod -aG crewchat-clients agent

# crewkiosk's HOME is empty: no dotfiles from /etc/skel, no settings, no CLAUDE.md.
$SUDO find /home/crewkiosk -mindepth 1 -delete
$SUDO chmod 0700 /home/crewkiosk

# The broker must never be in the daemon's group, and the agent in neither service's group.
for pair in crewbroker:crewchat agent:crewchat agent:crewbroker; do
  user="${pair%%:*}" group="${pair##*:}"
  if id -nG "$user" | tr ' ' '\n' | grep -qx "$group"; then
    log "ERROR: $user is in group $group"
    exit 1
  fi
done

if [[ -n "$(ls -A /home/crewkiosk)" ]]; then
  log "ERROR: /home/crewkiosk is not empty"
  exit 1
fi

for user in crewchat crewbroker crewkiosk agent; do
  log "$(id "$user")"
done
