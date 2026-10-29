#!/usr/bin/env bash
# CI ONLY: give this ephemeral runner the OS packages the runbook's Prerequisites name and a stock
# image lacks: today `acl` (/usr/bin/setfacl and getfacl, #115). An operator installs them
# before the runbook; here runbook-test.sh and daemon-up.sh call this first.
#
#   scripts/ci/host-prereqs.sh
#
# A package already present is left alone, so the script is idempotent. It needs root or sudo and
# apt: never run it on a workstation.
set -euo pipefail

log() { echo "host-prereqs: $*" >&2; }

SUDO=""
[[ "$(id -u)" -eq 0 ]] || SUDO="sudo"

missing=()
[[ -x /usr/bin/setfacl && -x /usr/bin/getfacl ]] || missing+=(acl)

if [[ ${#missing[@]} -eq 0 ]]; then
  log "nothing to install"
  exit 0
fi
command -v apt-get >/dev/null || { log "ERROR: no apt-get to install ${missing[*]}"; exit 1; }
log "installing ${missing[*]}"
$SUDO apt-get -o DPkg::Lock::Timeout=120 update -qq
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y -qq \
  --no-install-recommends "${missing[@]}"
[[ -x /usr/bin/setfacl ]] || { log "ERROR: acl installed, but no /usr/bin/setfacl"; exit 1; }
