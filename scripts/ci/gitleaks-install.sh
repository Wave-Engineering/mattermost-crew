#!/usr/bin/env bash
# CI ONLY: put a pinned gitleaks on this ephemeral runner for IT-29 (#140), and make IT-29
# require it. crewkiosk-corpus needs `gitleaks dir` (8.19 or later); Ubuntu 24.04's package is
# 8.18, so this fetches the upstream release and checks its sha256 before installing it.
#
#   scripts/ci/gitleaks-install.sh
#
# It sets CREWKIOSK_GITLEAKS_REQUIRED=1 in $GITHUB_ENV, so IT-29 fails rather than skips if
# gitleaks is missing. It needs root or sudo: never run it on a workstation.
set -euo pipefail

VERSION=8.30.1
SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
TARBALL="gitleaks_${VERSION}_linux_x64.tar.gz"
URL="https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${TARBALL}"

log() { echo "gitleaks-install: $*" >&2; }

SUDO=""
[[ "$(id -u)" -eq 0 ]] || SUDO="sudo"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "CREWKIOSK_GITLEAKS_REQUIRED=1" >>"$GITHUB_ENV"
fi

if [[ "$(command -v gitleaks 2>/dev/null)" && "$(gitleaks version 2>/dev/null)" == "$VERSION" ]]; then
  log "gitleaks ${VERSION} is already installed"
  exit 0
fi
[[ "$(uname -m)" == "x86_64" ]] || { log "ERROR: only x86_64 is pinned, not $(uname -m)"; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
log "fetching ${URL}"
curl -fsSL --retry 3 -o "${work}/${TARBALL}" "$URL"
echo "${SHA256}  ${work}/${TARBALL}" | sha256sum --check --quiet
tar -xzf "${work}/${TARBALL}" -C "$work" gitleaks
$SUDO install -m 0755 "${work}/gitleaks" /usr/local/bin/gitleaks
log "installed $(/usr/local/bin/gitleaks version)"
