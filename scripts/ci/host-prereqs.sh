#!/usr/bin/env bash
# CI ONLY: give this ephemeral runner the OS packages the runbook's Prerequisites name and a stock
# image lacks: today `acl` (/usr/bin/setfacl and getfacl, #115). An operator installs them
# before the runbook; here runbook-test.sh and daemon-up.sh call this first.
#
#   scripts/ci/host-prereqs.sh
#
# A package already present is left alone, so the script is idempotent. It needs root or sudo and
# apt: never run it on a workstation.
#
# A fresh ephemeral VM still has the image's own apt-get running for its first minute or so, and it
# holds /var/lib/apt/lists/lock ("E: Could not get lock ... It is held by process N (apt-get)").
# `-o DPkg::Lock::Timeout` waits for dpkg's lock only, not that one, so apt_retry retries a command
# that failed ON A LOCK, and on nothing else: any other failure, and the last attempt's, fails at once
# with apt's own output (#220).
#
#   HOST_PREREQS_APT_ATTEMPTS  tries per apt-get call, default 12
#   HOST_PREREQS_APT_SLEEP     seconds between tries, default 10 (about two minutes in all)
#   HOST_PREREQS_BIN_DIR       where setfacl and getfacl are looked for, default /usr/bin (for tests)
set -euo pipefail

log() { echo "host-prereqs: $*" >&2; }

ATTEMPTS="${HOST_PREREQS_APT_ATTEMPTS:-12}"
PAUSE="${HOST_PREREQS_APT_SLEEP:-10}"
BIN_DIR="${HOST_PREREQS_BIN_DIR:-/usr/bin}"
[[ "$ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || ATTEMPTS=12
[[ "$PAUSE" =~ ^[0-9]+$ ]] || PAUSE=10

SUDO=""
[[ "$(id -u)" -eq 0 ]] || SUDO="sudo"

# apt_retry CMD... — run CMD; if it fails and its output says it could not get an apt/dpkg lock, wait
# and try again, up to ATTEMPTS times. Its output is shown either way.
apt_retry() {
  local out try=1 rc
  out="$(mktemp)"
  while :; do
    rc=0
    "$@" >"$out" 2>&1 || rc=$?
    cat "$out" >&2
    if ((rc == 0)); then
      rm -f "$out"
      return 0
    fi
    # A lock error that is really "Permission denied" (no root, no sudo) will not clear by waiting.
    if ! grep -qE 'Could not get lock|Unable to lock' "$out" || grep -q 'Permission denied' "$out" ||
      ((try >= ATTEMPTS)); then
      rm -f "$out"
      return "$rc"
    fi
    log "apt is locked (try $try of $ATTEMPTS); waiting ${PAUSE}s"
    try=$((try + 1))
    sleep "$PAUSE"
  done
}

missing=()
[[ -x "$BIN_DIR/setfacl" && -x "$BIN_DIR/getfacl" ]] || missing+=(acl)

if [[ ${#missing[@]} -eq 0 ]]; then
  log "nothing to install"
  exit 0
fi
command -v apt-get >/dev/null || { log "ERROR: no apt-get to install ${missing[*]}"; exit 1; }
log "installing ${missing[*]}"
# The dpkg-lock wait is short on purpose: apt_retry loops around it, so a long one multiplies (12
# tries of 120s would approach the job's 30 minutes on a lock that never clears).
apt_retry $SUDO apt-get -o DPkg::Lock::Timeout=30 update -qq
apt_retry $SUDO env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=30 install -y -qq \
  --no-install-recommends "${missing[@]}"
[[ -x "$BIN_DIR/setfacl" ]] || { log "ERROR: acl installed, but no $BIN_DIR/setfacl"; exit 1; }
