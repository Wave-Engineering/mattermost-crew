#!/usr/bin/env bash
# CI ONLY: install the kiosk runner as an install would (D-107), so `make verify-install` and the
# installed tests reach a real crewkiosk.socket.
#
#   scripts/ci/two-user-setup.sh && scripts/ci/kiosk-up.sh
#
# It installs packaging/crewkiosk-run at /usr/local/libexec/crewkiosk-run (0755, root), the two
# units in /etc/systemd/system, a dummy model credential at /etc/crewkiosk/model.credential
# (0600, crewkiosk), and, because CI has no Claude Code, a stub /usr/local/bin/claude that answers
# with what it was run as, so a test can assert the user, the empty HOME, the flags and the
# prompt. It refuses to replace a real claude. It starts crewkiosk.socket and exports
# CREWKIOSK_SOCKET (into $GITHUB_ENV under Actions). It needs root or sudo and systemd: never
# run it on a workstation.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SOCK=/run/crewkiosk/kiosk.sock
STUB_MARK="crewkiosk-ci-stub"

log() { echo "kiosk-up: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

SUDO=""
[[ "$(id -u)" -eq 0 ]] || SUDO="sudo"
id crewkiosk >/dev/null 2>&1 || die "no crewkiosk user; run scripts/ci/two-user-setup.sh first"

if [[ -e /usr/local/bin/claude ]] && ! grep -q "$STUB_MARK" /usr/local/bin/claude; then
  die "/usr/local/bin/claude is a real install; kiosk-up.sh only runs on CI images"
fi
log "installing the stub claude, crewkiosk-run and the units"
cat <<'EOF' | $SUDO install -m 0755 -o root -g root /dev/stdin /usr/local/bin/claude
#!/usr/bin/env bash
# crewkiosk-ci-stub: what the kiosk runner was run as, for tests/integration/test_daemon_installed.py.
prompt="$(cat)"
printf 'user=%s\n' "$(id -un)"
printf 'home_entries=%s\n' "$(ls -A "$HOME" | wc -l)"
printf 'work_entries=%s\n' "$(ls -A . | wc -l)"
printf 'key_set=%s\n' "${ANTHROPIC_API_KEY:+yes}"
printf 'arg=[%s]\n' "$@"
printf 'prompt=%s\n' "$prompt"
EOF
$SUDO install -m 0755 -o root -g root "${ROOT}/packaging/crewkiosk-run" /usr/local/libexec/crewkiosk-run
$SUDO install -d -m 0750 -o root -g crewkiosk /etc/crewkiosk
echo "sk-ci-dummy-kiosk-key" | $SUDO install -m 0600 -o crewkiosk -g crewkiosk /dev/stdin \
  /etc/crewkiosk/model.credential
$SUDO install -m 0644 -o root -g root "${ROOT}/packaging/crewkiosk.socket" \
  "${ROOT}/packaging/crewkiosk@.service" /etc/systemd/system/
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now crewkiosk.socket
$SUDO test -S "$SOCK" || die "no socket at $SOCK"
log "crewkiosk.socket is listening on $SOCK"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "CREWKIOSK_SOCKET=${SOCK}" >>"$GITHUB_ENV"
fi
echo "export CREWKIOSK_SOCKET=${SOCK}"
