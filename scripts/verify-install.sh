#!/usr/bin/env bash
# Verify an installed crewchat host (Dev Spec §5.B, "Verify the install").
#
#   CREWBROKER_INSTANCES="<crew>:<server_id> ..." scripts/verify-install.sh
#
# Story 1.3 adds the broker's checks; Story 1.4 adds the daemon's. Per broker instance
# (<crew>-<server_id>):
#   - its socket /run/crewbroker/<crew>-<server_id>.sock exists, mode 0660, group crewchat;
#   - a `mint` for verify-canary, sent as the daemon's user (crewchat), succeeds; the canary's
#     token is revoked straight after and never printed;
#   - the agent's user cannot read its credential or token ledger. Each KNOWN file name is
#     checked with `test -e <file> && ! sudo -u agent test -r <file>`, never a glob (R-08).
#
# The daemon (Story 1.4):
#   - its socket /run/crewchat/crewchat.sock exists, mode 0660, group crewchat-clients;
#   - its ACL (the unit's agent-acl.conf drop-in, #115) holds user:<agent>:rw-, group::rw- and
#     mask::rw-, and three callers prove it: the agent's user WITHOUT crewchat-clients gets
#     `health` (the ACL), a uid with only the crewchat-clients gid gets it (a container's
#     --group-add), and a uid with neither is refused;
#   - `health` answers, asked as the agent's user (who reaches the socket through that group);
#   - a `register` for verify-canary, as the agent's user, succeeds and its reply holds no token;
#   - the daemon's token file for the canary, the KNOWN name
#     /var/lib/crewchat/tokens/verify-canary.json, exists and the agent's user cannot read it.
#     Never a glob: a shell that can't list the directory would pass vacuously (R-08).
#
# The kiosk (Story 3.5, D-107), on a host with the crewkiosk user:
#   - its socket /run/crewkiosk/kiosk.sock exists, mode 0660, group crewchat;
#   - its model config /etc/crewkiosk/model.conf, when present, is mode 0640, root:crewkiosk;
#   - the running daemon has NoNewPrivs set: it reaches the kiosk without changing user;
#   - crewkiosk cannot read any of the known credential files above.
#
# The credentials that servers.toml names (Story 3.7): /etc/crewchat/discord.credential (or
# `[discord] credential`) when Discord is enabled, and `[squawkie] credential` when set; and, on a
# kiosk host, the kiosk's model credential /etc/crewkiosk/model.credential. Neither the agent's
# user nor crewkiosk may read the first two; the agent's user may not read the third.
#
# CREWBROKER_BIN is the installed broker CLI (default /opt/mattermost-crew/.venv/bin/crewbroker),
# CREWCHAT_BIN the daemon's (default /opt/mattermost-crew/.venv/bin/crewchat).
# AGENT_USER is the agents' OS user (default agent). Run it as root, or as an operator who may
# `sudo -u` both the agents' user and root; otherwise it says to re-run as root and exits 1.
set -euo pipefail

CREWBROKER_BIN="${CREWBROKER_BIN:-/opt/mattermost-crew/.venv/bin/crewbroker}"
CREWCHAT_BIN="${CREWCHAT_BIN:-/opt/mattermost-crew/.venv/bin/crewchat}"
CREWCHAT_SOCKET=/run/crewchat/crewchat.sock
CREWCHAT_TOKENS=/var/lib/crewchat/tokens
CLIENTS_GROUP=crewchat-clients
AGENT_USER="${AGENT_USER:-agent}"
DAEMON_USER=crewchat
KIOSK_USER=crewkiosk
KIOSK_SOCKET=/run/crewkiosk/kiosk.sock
KIOSK_CREDENTIAL=/etc/crewkiosk/model.credential
KIOSK_MODEL_CONF=/etc/crewkiosk/model.conf
SERVERS_TOML=/etc/crewchat/servers.toml
# The daemon's own interpreter, which has tomllib (Python 3.12).
CREWCHAT_PYTHON="$(dirname "$CREWCHAT_BIN")/python"
RUN_DIR=/run/crewbroker
CONFIG_DIR=/etc/crewbroker
STATE_ROOT=/var/lib/crewbroker

failures=0
pass() { echo "verify-install: PASS $*"; }
fail() { echo "verify-install: FAIL $*" >&2; failures=$((failures + 1)); }

SUDO=""
[[ "$(id -u)" -eq 0 ]] || SUDO="sudo"

if [[ -z "${CREWBROKER_INSTANCES:-}" ]]; then
  echo "verify-install: FAIL CREWBROKER_INSTANCES is unset; list each broker as <crew>:<server_id>" >&2
  exit 1
fi

# The R-08 checks below read a refused sudo as "cannot read", so prove sudo works first. Root
# may always switch users; an operator who is the agents' user, with sudoers granting (root)
# only, may not, so it is told to re-run as root.
if ! sudo -n -u "$AGENT_USER" true; then
  if [[ "$(id -u)" -ne 0 ]]; then
    printf 'verify-install: re-run as root: sudo env AGENT_USER=%q CREWBROKER_INSTANCES=%q make verify-install\n' \
      "$AGENT_USER" "$CREWBROKER_INSTANCES" >&2
  else
    echo "verify-install: FAIL cannot run commands as $AGENT_USER (sudo -n -u $AGENT_USER)" >&2
  fi
  exit 1
fi

# no_agent_read FILE: the file exists, and the agent's user cannot read it.
no_read_as() {
  local user="$1" file="$2"
  if ! $SUDO test -e "$file"; then
    fail "$file does not exist"
  elif sudo -n -u "$user" test -r "$file"; then
    fail "$file is readable by $user"
  else
    pass "$file exists and $user cannot read it"
  fi
}
no_agent_read() { no_read_as "$AGENT_USER" "$1"; }

for entry in $CREWBROKER_INSTANCES; do
  crew="${entry%%:*}"
  server_id="${entry#*:}"
  if [[ -z "$crew" || -z "$server_id" || "$entry" != *:* ]]; then
    fail "bad CREWBROKER_INSTANCES entry '$entry' (want <crew>:<server_id>)"
    continue
  fi
  instance="${crew}-${server_id}"
  sock="${RUN_DIR}/${instance}.sock"

  # The socket: exists, is a socket, mode 0660, group crewchat.
  if ! $SUDO test -S "$sock"; then
    fail "$sock is not a socket"
  else
    read -r mode group < <($SUDO stat -c '%a %G' "$sock")
    if [[ "$mode" == 660 && "$group" == "$DAEMON_USER" ]]; then
      pass "$sock is mode 0660, group $DAEMON_USER"
    else
      fail "$sock is mode $mode, group $group (want 660, $DAEMON_USER)"
    fi
  fi

  # A mint for the canary, as the daemon's user. Only ok and token_id leave this pipe.
  args="{\"dev_name\": \"verify-canary\", \"crew\": \"${crew}\"}"
  reply="$(sudo -u "$DAEMON_USER" "$CREWBROKER_BIN" call "$sock" mint "$args" 2>/dev/null || true)"
  token_id="$(jq -r 'if .ok and (.result.token | length > 0) then .result.token_id else empty end' \
    <<<"$reply" 2>/dev/null || true)"
  if [[ -n "$token_id" ]]; then
    pass "mint for verify-canary.${crew} on ${instance}"
    revoke="$(sudo -u "$DAEMON_USER" "$CREWBROKER_BIN" call "$sock" revoke \
      "{\"token_id\": \"${token_id}\"}" 2>/dev/null || true)"
    if jq -e '.ok' <<<"$revoke" >/dev/null 2>&1; then
      pass "revoked the canary's token on ${instance}"
    else
      fail "could not revoke the canary's token on ${instance}: $(jq -c '.error' <<<"$revoke" 2>/dev/null)"
    fi
  else
    fail "mint for verify-canary.${crew} on ${instance}: $(jq -c '.error // .' <<<"$reply" 2>/dev/null || echo "no reply")"
  fi

  # Known credential files, by name (R-08).
  no_agent_read "${CONFIG_DIR}/${instance}.credential"
  no_agent_read "${STATE_ROOT}/${instance}/tokens.json"
done

# The daemon: one socket per host.
if ! $SUDO test -S "$CREWCHAT_SOCKET"; then
  fail "$CREWCHAT_SOCKET is not a socket"
else
  read -r mode group < <($SUDO stat -c '%a %G' "$CREWCHAT_SOCKET")
  if [[ "$mode" == 660 && "$group" == "$CLIENTS_GROUP" ]]; then
    pass "$CREWCHAT_SOCKET is mode 0660, group $CLIENTS_GROUP"
  else
    fail "$CREWCHAT_SOCKET is mode $mode, group $group (want 660, $CLIENTS_GROUP)"
  fi
fi

# The socket's ACL: the agents' user by name, and the group's rw kept. getfacl prints
# `#effective:` after an entry the mask cuts down, so each line must match exactly.
if acl="$($SUDO getfacl -p "$CREWCHAT_SOCKET" 2>&1)"; then
  missing=()
  for want in "user:${AGENT_USER}:rw-" "group::rw-" "mask::rw-"; do
    grep -qxF "$want" <<<"$acl" || missing+=("$want")
  done
  if ((${#missing[@]} == 0)); then
    pass "$CREWCHAT_SOCKET has user:${AGENT_USER}:rw-, group::rw- and mask::rw-"
  else
    fail "$CREWCHAT_SOCKET lacks ${missing[*]} (is crewchat.service.d/agent-acl.conf installed?): $(tr '\n' ' ' <<<"$acl")"
  fi
else
  fail "getfacl $CREWCHAT_SOCKET: $acl"
fi

# Three callers, each with exactly the ids named: the agents' user without the group (the ACL),
# a uid with only the group's gid (a container on --group-add), and a uid with neither.
# health_as ARGS...: `health` through setpriv with ARGS; prints the reply, or the error.
health_as() { $SUDO setpriv "$@" -- "$CREWCHAT_BIN" call "$CREWCHAT_SOCKET" health 2>&1; }
if ! command -v setpriv >/dev/null; then
  fail "no setpriv: cannot check who may reach $CREWCHAT_SOCKET"
else
  clients_gid="$(getent group "$CLIENTS_GROUP" | cut -d: -f3)"
  nobody_gid="$(id -g nobody)"
  reply="$(health_as --reuid="$AGENT_USER" --regid="$(id -g "$AGENT_USER")" --clear-groups || true)"
  if jq -e '.ok' <<<"$reply" >/dev/null 2>&1; then
    pass "daemon health answers $AGENT_USER without $CLIENTS_GROUP (the ACL)"
  else
    fail "daemon health as $AGENT_USER without $CLIENTS_GROUP: $reply"
  fi
  reply="$(health_as --reuid=nobody --regid="$nobody_gid" --groups="$clients_gid" || true)"
  if jq -e '.ok' <<<"$reply" >/dev/null 2>&1; then
    pass "daemon health answers a uid with only the $CLIENTS_GROUP gid"
  else
    fail "daemon health as nobody with only the $CLIENTS_GROUP gid: $reply"
  fi
  # Refused means the connect itself failed, not that setpriv or the CLI broke.
  if reply="$(health_as --reuid=nobody --regid="$nobody_gid" --clear-groups)"; then
    fail "daemon health answers nobody, with neither the ACL nor $CLIENTS_GROUP: $reply"
  elif grep -q 'Permission denied' <<<"$reply"; then
    pass "a uid with neither the ACL nor $CLIENTS_GROUP is refused"
  else
    fail "daemon health as nobody failed, but not with Permission denied: $reply"
  fi
fi

health="$(sudo -u "$AGENT_USER" "$CREWCHAT_BIN" call "$CREWCHAT_SOCKET" health 2>/dev/null || true)"
if jq -e '.ok and (.result.backends | type == "object")' <<<"$health" >/dev/null 2>&1; then
  pass "daemon health answers $AGENT_USER: $(jq -c '.result.backends | keys' <<<"$health")"
else
  fail "daemon health: $(jq -c '.error // .' <<<"$health" 2>/dev/null || echo "no reply")"
fi

# The canary registers with the host's default crew. Only ok and the account leave this pipe.
reply="$(sudo -u "$AGENT_USER" "$CREWCHAT_BIN" call "$CREWCHAT_SOCKET" register \
  '{"dev_name": "verify-canary", "session_id": "verify-install"}' 2>/dev/null || true)"
if jq -e '.ok and (.result.account | startswith("verify-canary."))' <<<"$reply" >/dev/null 2>&1; then
  if grep -q '"token' <<<"$reply"; then
    fail "the daemon's register reply carries a token field (R-08)"
  else
    pass "register for $(jq -r .result.account <<<"$reply") through the daemon, no token returned"
  fi
else
  fail "register for verify-canary: $(jq -c '.error // .' <<<"$reply" 2>/dev/null || echo "no reply")"
fi
no_agent_read "${CREWCHAT_TOKENS}/verify-canary.json"

# The credentials servers.toml names (Discord when enabled, squawkie when configured), read
# with the daemon's own interpreter, and the kiosk's model credential on a kiosk host. Each is
# a KNOWN name, checked like the rest (R-08, Story 3.7).
CONFIG_CREDENTIALS_PY='
import sys, tomllib
config = tomllib.loads(sys.stdin.read())
discord = config.get("discord", {})
if discord.get("enabled") is True:
    print(discord.get("credential", "/etc/crewchat/discord.credential"))
squawkie = config.get("squawkie", {})
if isinstance(squawkie.get("credential"), str):
    print(squawkie["credential"])
'
config_credentials=()
if config_text="$($SUDO cat "$SERVERS_TOML" 2>/dev/null)"; then
  if listed="$("$CREWCHAT_PYTHON" -c "$CONFIG_CREDENTIALS_PY" <<<"$config_text" 2>&1)"; then
    while IFS= read -r file; do
      [[ -n "$file" ]] && config_credentials+=("$file")
    done <<<"$listed"
  else
    fail "could not read the credentials $SERVERS_TOML names: $listed"
  fi
else
  fail "cannot read $SERVERS_TOML"
fi
for file in "${config_credentials[@]}"; do
  no_agent_read "$file"
done
if id "$KIOSK_USER" >/dev/null 2>&1; then
  no_agent_read "$KIOSK_CREDENTIAL"
fi

# The kiosk (D-107), when this host runs the CLI runner, i.e. has the crewkiosk user.
if id "$KIOSK_USER" >/dev/null 2>&1; then
  if ! $SUDO test -S "$KIOSK_SOCKET"; then
    fail "$KIOSK_SOCKET is not a socket (is crewkiosk.socket enabled?)"
  else
    read -r mode group < <($SUDO stat -c '%a %G' "$KIOSK_SOCKET")
    if [[ "$mode" == 660 && "$group" == "$DAEMON_USER" ]]; then
      pass "$KIOSK_SOCKET is mode 0660, group $DAEMON_USER"
    else
      fail "$KIOSK_SOCKET is mode $mode, group $group (want 660, $DAEMON_USER)"
    fi
  fi
  # The kiosk's model.conf, when there is one: root's, readable by crewkiosk's group only (#116).
  if $SUDO test -e "$KIOSK_MODEL_CONF"; then
    read -r mode owner group < <($SUDO stat -c '%a %U %G' "$KIOSK_MODEL_CONF")
    if [[ "$mode" == 640 && "$owner" == root && "$group" == "$KIOSK_USER" ]]; then
      pass "$KIOSK_MODEL_CONF is mode 0640, root:$KIOSK_USER"
    else
      fail "$KIOSK_MODEL_CONF is mode $mode, $owner:$group (want 640, root:$KIOSK_USER)"
    fi
  fi
  # The daemon must run with no new privileges: it reaches the kiosk without changing user.
  pid="$(pgrep -u "$DAEMON_USER" -f -o "crewchat serve" || true)"
  if [[ -z "$pid" ]]; then
    fail "no 'crewchat serve' process running as $DAEMON_USER"
  elif $SUDO grep -qE '^NoNewPrivs:[[:space:]]+1$' "/proc/${pid}/status"; then
    pass "the daemon (pid $pid) runs with NoNewPrivileges"
  else
    fail "the daemon (pid $pid) can gain privileges; crewchat.service must set NoNewPrivileges=yes"
  fi
  # crewkiosk can read no chat or broker credential: the same KNOWN names, never a glob.
  if ! sudo -n -u "$KIOSK_USER" true; then
    fail "cannot run commands as $KIOSK_USER (sudo -n -u $KIOSK_USER)"
  else
    for entry in $CREWBROKER_INSTANCES; do
      instance="${entry%%:*}-${entry#*:}"
      for file in "${CONFIG_DIR}/${instance}.credential" "${STATE_ROOT}/${instance}/tokens.json"; do
        no_read_as "$KIOSK_USER" "$file"
      done
    done
    no_read_as "$KIOSK_USER" "${CREWCHAT_TOKENS}/verify-canary.json"
    for file in "${config_credentials[@]}"; do
      no_read_as "$KIOSK_USER" "$file"
    done
  fi
fi

if ((failures > 0)); then
  echo "verify-install: $failures check(s) failed" >&2
  exit 1
fi
echo "verify-install: all checks passed"
