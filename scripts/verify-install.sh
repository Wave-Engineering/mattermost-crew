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
# Last, the canary's bot is deactivated on every broker instance (the broker's `deactivate`),
# and the server's `delete_at` read back must be > 0, so a verify leaves no active bot (#127).
#
# The kiosk (Story 3.5, D-107), on a host with the crewkiosk user:
#   - its socket /run/crewkiosk/kiosk.sock exists, mode 0660, group crewchat;
#   - its model config /etc/crewkiosk/model.conf, when present, is mode 0640, root:crewkiosk;
#   - the running daemon has NoNewPrivs set: it reaches the kiosk without changing user;
#   - crewkiosk cannot read any of the known credential files above.
#
# Kiosk research (#142), on a host with the crewkiosk-broker user:
#   - crewkiosk-broker, with its unit's supplementary groups, reads its own key,
#     /etc/crewkiosk/broker-model.credential, and not the runner's; crewkiosk reads its own and
#     not the broker's; the agent's user reads neither;
#   - crewcorpus may traverse /etc/crewkiosk (ACL user:crewcorpus:--x) and not list it;
#   - crewkiosk-broker is active whenever any research server has its pool manifest written;
#   - for each server in CREWKIOSK_RESEARCH_SERVERS: its user crewkiosk-r-<server> and its group
#     exist, and the running broker holds that group; its broker sockets are group
#     crewkiosk-r-<server>, 0660, and present once its manifest is written; its linked unit points
#     at the shared body, which sits in no unit search path, and loads as crewkiosk-r-<server>
#     with that user's current uid, an empty root, no network, no new privileges,
#     ProtectProc=invisible, both keys hidden, and exactly that server's two broker sockets
#     bound (D-113). Whether PrivatePIDs is in effect (systemd 257+) is reported, not required.
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
BROKER_USER=crewkiosk-broker
CORPUS_USER=crewcorpus
BROKER_KEY=/etc/crewkiosk/broker-model.credential
RESEARCH_BODY=/usr/local/share/crewkiosk/crewkiosk-research@.service
SERVERS_TOML=/etc/crewchat/servers.toml
# The daemon's own interpreter, which has tomllib (Python 3.12).
CREWCHAT_PYTHON="$(dirname "$CREWCHAT_BIN")/python"
RUN_DIR=/run/crewbroker
CONFIG_DIR=/etc/crewbroker
STATE_ROOT=/var/lib/crewbroker

failures=0
pass() { echo "verify-install: PASS $*"; }
info() { echo "verify-install: INFO $*"; }
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

# Nothing mints the canary after this: deactivate its bot on every broker instance, so a verify
# leaves no active bot on the server (#127). `delete_at` is the server's, read back after the
# disable. The next verify's mint reactivates it.
for entry in $CREWBROKER_INSTANCES; do
  [[ "$entry" == *:* && -n "${entry%%:*}" && -n "${entry#*:}" ]] || continue  # failed above
  crew="${entry%%:*}"
  instance="${crew}-${entry#*:}"
  reply="$(sudo -u "$DAEMON_USER" "$CREWBROKER_BIN" call "${RUN_DIR}/${instance}.sock" deactivate \
    "{\"dev_name\": \"verify-canary\", \"crew\": \"${crew}\"}" 2>/dev/null || true)"
  if jq -e '.ok and (.result.delete_at | type == "number" and . > 0)' <<<"$reply" >/dev/null 2>&1; then
    pass "deactivated verify-canary.${crew} on ${instance} (delete_at $(jq -r .result.delete_at <<<"$reply"))"
  else
    fail "could not deactivate verify-canary.${crew} on ${instance}: $(jq -c '.error // .' <<<"$reply" 2>/dev/null || echo "no reply")"
  fi
done

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

# Kiosk research (#142; D-111, D-113), on a host with the crewkiosk-broker user. The two model
# keys, as the users who hold them: the research-off runner (crewkiosk) opens only its own, and
# the broker only its own. Each research server's runner is that server's own user,
# crewkiosk-r-<server> (D-114), which no key's file mode names: its unit also hides both keys,
# and each research unit is checked here as systemd loads it. What such a unit can actually reach
# is IT-31b's, on this same body.
if id "$BROKER_USER" >/dev/null 2>&1; then
  # The broker as its unit runs it: it reaches /etc/crewkiosk (0750 root:crewkiosk) through the
  # unit's SupplementaryGroups, which `sudo -u` would not give it.
  broker_groups="$(systemctl show -P SupplementaryGroups crewkiosk-broker.service)"
  as_broker() {
    local groups=(--clear-groups)
    [[ -z "$broker_groups" ]] || groups=(--groups "${broker_groups// /,}")
    $SUDO setpriv --reuid "$BROKER_USER" --regid "$BROKER_USER" "${groups[@]}" -- "$@"
  }
  if as_broker test -r "$BROKER_KEY"; then
    pass "$BROKER_USER (groups: ${broker_groups:-none}) can read its own key, $BROKER_KEY"
  else
    fail "$BROKER_USER (groups: ${broker_groups:-none}) cannot read its key $BROKER_KEY"
  fi
  if ! $SUDO test -e "$KIOSK_CREDENTIAL"; then
    fail "$KIOSK_CREDENTIAL does not exist"
  elif as_broker test -r "$KIOSK_CREDENTIAL"; then
    fail "$KIOSK_CREDENTIAL is readable by $BROKER_USER"
  else
    pass "$KIOSK_CREDENTIAL exists and $BROKER_USER cannot read it"
  fi
  no_read_as "$KIOSK_USER" "$BROKER_KEY"
  no_agent_read "$BROKER_KEY"
  if sudo -n -u "$KIOSK_USER" test -r "$KIOSK_CREDENTIAL"; then
    pass "the research-off runner ($KIOSK_USER) can read its own key, $KIOSK_CREDENTIAL"
  else
    fail "$KIOSK_USER cannot read its key $KIOSK_CREDENTIAL"
  fi
  # The corpus builder traverses /etc/crewkiosk to its forge credentials by an ACL: x, and no
  # r, so it can't list the directory. A later chmod or chgrp must not silently undo either.
  acl="$($SUDO getfacl -cp /etc/crewkiosk 2>/dev/null | grep -E "^user:${CORPUS_USER}:" || true)"
  if [[ "$acl" == "user:${CORPUS_USER}:--x" ]] &&
    sudo -n -u "$CORPUS_USER" test -x /etc/crewkiosk &&
    ! sudo -n -u "$CORPUS_USER" ls /etc/crewkiosk >/dev/null 2>&1; then
    pass "$CORPUS_USER may traverse /etc/crewkiosk (ACL --x) and not list it"
  else
    fail "/etc/crewkiosk: $CORPUS_USER's ACL is '${acl:-none}' (want user:${CORPUS_USER}:--x, traverse only)"
  fi
  search="$(systemd-analyze unit-paths)"
  systemd_version="$(systemctl --version | awk 'NR == 1 { print $2 }')"
  # A server with its pool manifest written is one the broker should be serving: a broker that
  # is down, or in its restart back-off, serves it nothing. One FAIL names them all.
  broker_active=no
  systemctl is-active --quiet crewkiosk-broker.service && broker_active=yes
  unserved=()
  for server in ${CREWKIOSK_RESEARCH_SERVERS:-}; do
    unit="crewkiosk-research-${server}@verify.service"
    ruser="crewkiosk-r-${server}"
    if ! kiosk_uid="$(id -u -- "$ruser" 2>/dev/null)"; then
      fail "no user $ruser for research server $server (re-run crewkiosk-research-units $server)"
      continue
    fi
    manifest=no
    $SUDO test -f "/var/lib/crewchat-pool/${server}.json" && manifest=yes
    [[ "$manifest" == yes && "$broker_active" == no ]] && unserved+=("$server")
    # A running broker keeps the groups it started with. Started before crewkiosk-research-units
    # added it to this server's group, it can't give the server's sockets that group, and so
    # serves the server nothing.
    broker_pid="$(systemctl show -p MainPID --value crewkiosk-broker.service 2>/dev/null || true)"
    if [[ -n "$broker_pid" && "$broker_pid" != 0 ]]; then
      gid="$(getent group -- "$ruser" | cut -d: -f3 || true)"
      if [[ -z "$gid" ]]; then
        fail "no group $ruser for research server $server (re-run crewkiosk-research-units $server)"
      elif $SUDO grep '^Groups:' "/proc/$broker_pid/status" | tr -s '[:space:]' '\n' | grep -qxF -- "$gid"; then
        pass "the running broker holds $ruser's group"
      else
        fail "the running broker lacks $ruser's group (gid $gid): systemctl restart crewkiosk-broker.service"
      fi
    fi
    # Its broker sockets, once the broker serves them: that server's group, 0660, and so no
    # other server's kiosk, nor crewkiosk, can open them by path. With the server's pool manifest
    # written and the broker running, a missing socket is a server the broker failed to serve.
    for sock in "/run/crewkiosk-broker/${server}.sock" "/run/crewkiosk-broker/${server}.model.sock"; do
      if ! $SUDO test -S "$sock"; then
        if [[ "$manifest" == yes && "$broker_active" == yes ]]; then
          fail "$sock is missing, with $server's pool manifest written and the broker running (restart crewkiosk-broker.service; see its journal)"
        elif [[ "$manifest" == no ]]; then
          info "$sock is not served yet (no pool manifest for $server)"
        fi
      elif [[ "$($SUDO stat -c '%G %a' -- "$sock")" == "$ruser 660" ]]; then
        pass "$sock is $ruser's alone (group $ruser, 0660)"
      else
        fail "$sock is $($SUDO stat -c '%U:%G %a' -- "$sock") (want group $ruser, 0660)"
      fi
    done
    link="/etc/systemd/system/crewkiosk-research-${server}@.service"
    if [[ "$(readlink -- "$link" || true)" != "$RESEARCH_BODY" ]]; then
      fail "$link is not a link to $RESEARCH_BODY (re-run crewkiosk-research-units $server)"
      continue
    fi
    if grep -qxF -- "$(dirname "$RESEARCH_BODY")" <<<"$search"; then
      fail "$RESEARCH_BODY is in a unit search path (D-113)"
    fi
    declare -A prop=()
    while IFS='=' read -r key value; do
      prop["$key"]="$value"
    done < <(systemctl show "$unit" -p LoadState -p User -p TemporaryFileSystem -p PrivateNetwork \
      -p NoNewPrivileges -p InaccessiblePaths -p BindReadOnlyPaths -p Environment \
      -p ProtectProc -p PrivatePIDs)
    bad=()
    [[ "${prop[LoadState]:-}" == loaded ]] || bad+=("LoadState=${prop[LoadState]:-}")
    [[ "${prop[User]:-}" == "$ruser" ]] || bad+=("User=${prop[User]:-} (want $ruser)")
    [[ "${prop[ProtectProc]:-}" == invisible ]] || bad+=("ProtectProc=${prop[ProtectProc]:-}")
    [[ " ${prop[TemporaryFileSystem]:-} " == *" /:ro "* ]] || bad+=("no TemporaryFileSystem=/:ro")
    [[ "${prop[PrivateNetwork]:-}" == yes ]] || bad+=("PrivateNetwork=${prop[PrivateNetwork]:-}")
    [[ "${prop[NoNewPrivileges]:-}" == yes ]] || bad+=("NoNewPrivileges=${prop[NoNewPrivileges]:-}")
    for key in "$KIOSK_CREDENTIAL" "$BROKER_KEY"; do
      [[ " ${prop[InaccessiblePaths]:-} " == *" -${key} "* ]] || bad+=("$key not hidden")
    done
    # Of the broker's sockets, exactly this server's two (D-113).
    broker_binds="$(tr ' ' '\n' <<<"${prop[BindReadOnlyPaths]:-}" | grep -F '/run/crewkiosk-broker' |
      sed 's/^-//; s/:.*//' | sort | tr '\n' ' ')"
    want="/run/crewkiosk-broker/${server}.model.sock /run/crewkiosk-broker/${server}.sock "
    [[ "$broker_binds" == "$want" ]] || bad+=("broker binds: ${broker_binds:-none}")
    [[ " ${prop[Environment]:-} " == *" CREWKIOSK_UID=${kiosk_uid} "* ]] ||
      bad+=("CREWKIOSK_UID is not $kiosk_uid (re-run crewkiosk-research-units $server)")
    if ((${#bad[@]} == 0)); then
      pass "$unit: as $ruser, empty root, no network, both keys hidden, only $server's broker sockets"
    else
      fail "$unit: ${bad[*]}"
    fi
    # The uid is the wall between servers; a PID namespace is a second one, on systemd 257+.
    if [[ "${prop[PrivatePIDs]:-}" == yes ]]; then
      info "$unit: PrivatePIDs=yes in effect (systemd $systemd_version)"
    else
      info "$unit: PrivatePIDs not in effect (systemd $systemd_version; it needs 257): the uid wall holds alone"
    fi
    unset prop
  done
  if ((${#unserved[@]} > 0)); then
    fail "crewkiosk-broker.service is not active, and these servers have pool manifests: ${unserved[*]} (see its journal)"
  fi
fi

if ((failures > 0)); then
  echo "verify-install: $failures check(s) failed" >&2
  exit 1
fi
echo "verify-install: all checks passed"
