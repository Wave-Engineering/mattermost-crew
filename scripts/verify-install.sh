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
#   - `health` answers, asked as the agent's user (who reaches the socket through that group);
#   - a `register` for verify-canary, as the agent's user, succeeds and its reply holds no token;
#   - the daemon's token file for the canary, the KNOWN name
#     /var/lib/crewchat/tokens/verify-canary.json, exists and the agent's user cannot read it.
#     Never a glob: a shell that can't list the directory would pass vacuously (R-08).
#
# CREWBROKER_BIN is the installed broker CLI (default /opt/mattermost-crew/.venv/bin/crewbroker),
# CREWCHAT_BIN the daemon's (default /opt/mattermost-crew/.venv/bin/crewchat).
# AGENT_USER is the agents' OS user (default agent). It needs sudo.
set -euo pipefail

CREWBROKER_BIN="${CREWBROKER_BIN:-/opt/mattermost-crew/.venv/bin/crewbroker}"
CREWCHAT_BIN="${CREWCHAT_BIN:-/opt/mattermost-crew/.venv/bin/crewchat}"
CREWCHAT_SOCKET=/run/crewchat/crewchat.sock
CREWCHAT_TOKENS=/var/lib/crewchat/tokens
CLIENTS_GROUP=crewchat-clients
AGENT_USER="${AGENT_USER:-agent}"
DAEMON_USER=crewchat
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

# The R-08 checks below read a refused sudo as "cannot read", so prove sudo works first.
if ! sudo -n -u "$AGENT_USER" true; then
  echo "verify-install: FAIL cannot run commands as $AGENT_USER (sudo -n -u $AGENT_USER)" >&2
  exit 1
fi

# no_agent_read FILE: the file exists, and the agent's user cannot read it.
no_agent_read() {
  local file="$1"
  if ! $SUDO test -e "$file"; then
    fail "$file does not exist"
  elif sudo -n -u "$AGENT_USER" test -r "$file"; then
    fail "$file is readable by $AGENT_USER"
  else
    pass "$file exists and $AGENT_USER cannot read it"
  fi
}

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

if ((failures > 0)); then
  echo "verify-install: $failures check(s) failed" >&2
  exit 1
fi
echo "verify-install: all checks passed"
