#!/usr/bin/env bash
# CI ONLY: put a pinned ripgrep on this runner's PATH for crewkiosk-broker's corpus_search
# (#141): its unit tests and IT-30. It fetches the upstream static (musl) release and checks
# its sha256 before installing it.
#
#   scripts/ci/rg-install.sh
#
# It needs no root: rg goes into "${RUNNER_TEMP}/rg-bin", which is added to $GITHUB_PATH, so it
# is safe on the persistent unit runner too. It sets CREWKIOSK_RG_REQUIRED=1 in $GITHUB_ENV, so
# the rg-backed tests fail rather than skip if rg is missing.
set -euo pipefail

VERSION=14.1.1
SHA256=4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e
NAME="ripgrep-${VERSION}-x86_64-unknown-linux-musl"
TARBALL="${NAME}.tar.gz"
URL="https://github.com/BurntSushi/ripgrep/releases/download/${VERSION}/${TARBALL}"
BIN_DIR="${RUNNER_TEMP:-/tmp}/rg-bin"

log() { echo "rg-install: $*" >&2; }

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "CREWKIOSK_RG_REQUIRED=1" >>"$GITHUB_ENV"
fi
if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$BIN_DIR" >>"$GITHUB_PATH"
fi

if [[ -x "${BIN_DIR}/rg" && "$("${BIN_DIR}/rg" --version | head -n 1)" == "ripgrep ${VERSION}"* ]]; then
  log "ripgrep ${VERSION} is already in ${BIN_DIR}"
  exit 0
fi
[[ "$(uname -m)" == "x86_64" ]] || { log "ERROR: only x86_64 is pinned, not $(uname -m)"; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
log "fetching ${URL}"
curl -fsSL --retry 3 -o "${work}/${TARBALL}" "$URL"
echo "${SHA256}  ${work}/${TARBALL}" | sha256sum --check --quiet
tar -xzf "${work}/${TARBALL}" -C "$work" "${NAME}/rg"
mkdir -p "$BIN_DIR"
install -m 0755 "${work}/${NAME}/rg" "${BIN_DIR}/rg"
log "installed $("${BIN_DIR}/rg" --version | head -n 1) in ${BIN_DIR}"
