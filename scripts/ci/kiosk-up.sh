#!/usr/bin/env bash
# CI ONLY: install the kiosk runner as an install would (D-107), so `make verify-install` and the
# installed tests reach a real crewkiosk.socket.
#
#   scripts/ci/two-user-setup.sh && scripts/ci/kiosk-up.sh
#
# It installs packaging/crewkiosk-run at /usr/local/libexec/crewkiosk-run (0755, root), the two
# units in /etc/systemd/system, a dummy model credential at /etc/crewkiosk/model.credential
# (0600, crewkiosk), a model.conf naming a generic gateway (0640, root:crewkiosk), and, because
# CI has no Claude Code, a stub /usr/local/bin/claude that answers with what it was run as, so a
# test can assert the user, the empty HOME, the gateway, the pinned model, the flags and the
# prompt. It refuses to replace a real claude. It starts crewkiosk.socket and exports
# CREWKIOSK_SOCKET (into $GITHUB_ENV under Actions). It needs root or sudo and systemd: never
# run it on a workstation.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SOCK=/run/crewkiosk/kiosk.sock
STUB_MARK="crewkiosk-ci-stub"
RESEARCH_SERVERS="itdev itother" # IT-31b's two servers

log() { echo "kiosk-up: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

SUDO=""
[[ "$(id -u)" -eq 0 ]] || SUDO="sudo"
id crewkiosk >/dev/null 2>&1 || die "no crewkiosk user; run scripts/ci/two-user-setup.sh first"

if [[ -e /usr/local/bin/claude ]] && ! grep -q "$STUB_MARK" /usr/local/bin/claude; then
  die "/usr/local/bin/claude is a real install; kiosk-up.sh only runs on CI images"
fi
log "installing the stub claude, crewkiosk-run and the units"
cat <<'EOF' | "${ROOT}/scripts/ci/put-file.sh" 0755 root:root /usr/local/bin/claude
#!/usr/bin/env bash
# crewkiosk-ci-stub: what the kiosk runner was run as, for tests/integration/test_daemon_installed.py.
prompt="$(cat)"
printf 'user=%s\n' "$(id -un)"
printf 'home_entries=%s\n' "$(ls -A "$HOME" | wc -l)"
printf 'work_entries=%s\n' "$(ls -A . | wc -l)"
printf 'key_set=%s\n' "${ANTHROPIC_API_KEY:+yes}"
printf 'base_url=%s\n' "${ANTHROPIC_BASE_URL:-}"
printf 'model_env=%s\n' "${ANTHROPIC_MODEL:-}"
printf 'arg=[%s]\n' "$@"
printf 'prompt=%s\n' "$prompt"
# In a research unit (#142), also what its namespace holds, for tests/integration/test_kiosk_confinement.py.
if [[ "${ANTHROPIC_BASE_URL:-}" == */a/* ]]; then
  exec /usr/bin/python3 -I -S -c '
import json, os, re, socket, sys, time
prompt = sys.argv[1]
if "LINGER" in prompt:
    # Stay up, so that another server'"'"'s instance can try to reach this one (IT-31b).
    time.sleep(45)
    sys.exit(0)
def others(prompt):
    # Another server'"'"'s live instance: its host pids and its answer id come in the prompt.
    # What this one can reach of it, by those pids and by a walk of its own /proc. The test
    # wants a failure in every "kill" entry and no "root" or "environ" entry at all.
    m = re.search(r"OTHER server=([a-z0-9_]+) answer=([0-9a-f]{32}) pids=([0-9,]+)", prompt)
    if m is None:
        return None
    server, answer, pids = m.group(1), m.group(2).encode(), [int(p) for p in m.group(3).split(",")]
    found = {}
    for pid in pids:
        try:
            os.kill(pid, 0)
            found["kill %d" % pid] = "signalled"
        except OSError as e:
            found["kill %d" % pid] = type(e).__name__
    walked = {int(p) for p in os.listdir("/proc") if p.isdigit()}
    for pid in sorted((walked | set(pids)) - {os.getpid()}):
        try:
            names = os.listdir("/proc/%d/root/run/crewkiosk-broker" % pid)
            if any(n.startswith(server + ".") for n in names):
                found["root %d" % pid] = sorted(names)
        except OSError:
            pass
        try:
            with open("/proc/%d/environ" % pid, "rb") as f:
                if answer in f.read():
                    found["environ %d" % pid] = "its answer id"
        except OSError:
            pass
    return {"walked": len(walked), "found": found}
def connect(path):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(path)
        return "connected"
    except OSError as e:
        return type(e).__name__
    finally:
        s.close()
def read(path):
    try:
        if os.path.isdir(path):
            return "listed:%d" % len(os.listdir(path))
        with open(path, "rb") as f:
            f.read(1)
        return "read"
    except OSError as e:
        return type(e).__name__
def tcp(host):
    try:
        socket.create_connection((host, 80), timeout=3).close()
        return "connected"
    except OSError as e:
        return type(e).__name__
def dns(server):
    # A raw DNS query for the A record of example.com, straight to a resolver: no resolv.conf, no
    # glibc. Only a reply counts as getting through.
    query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(3)
            s.sendto(query, (server, 53))
            return "reply" if s.recv(512)[:2] == query[:2] else "other"
    except OSError as e:
        return type(e).__name__
def setuid_bins():
    found = []
    for d in ("/usr/bin", "/usr/sbin"):
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else ():
            try:
                if os.stat(os.path.join(d, n)).st_mode & 0o4000:
                    found.append(os.path.join(d, n))
            except OSError:
                pass
    return found
status = dict(l.split(":", 1) for l in open("/proc/self/status").read().splitlines() if ":" in l)
b = "/run/crewkiosk-broker"
print("probe=" + json.dumps({
    "uid": os.getuid(),
    "no_new_privs": status.get("NoNewPrivs", "").strip(),
    "setuid": setuid_bins(),
    "root": sorted(os.listdir("/")),
    "broker_dir": sorted(os.listdir(b)) if os.path.isdir(b) else None,
    "connect": {n: connect(f"{b}/{n}") for s in ("itdev", "itother")
        for n in (f"{s}.sock", f"{s}.model.sock")},
    "control": connect("/run/crewkiosk-broker-control/control.sock"),
    "read": {p: read(p) for p in ("/etc/passwd", "/etc/crewkiosk/model.credential",
        "/etc/crewkiosk/broker-model.credential", "/home", "/opt/mattermost-crew",
        "/var/lib/crewcorpus", "/var/lib/crewchat")},
    "tcp": tcp("1.1.1.1"),
    "dns": {s: dns(s) for s in ("127.0.0.53", "1.1.1.1")},
    "others": others(prompt),
}))
' "$prompt"
fi
EOF
# -D: a stock Ubuntu /usr/local has no libexec (GitHub's hosted image did; the ephemeral pool's doesn't).
$SUDO install -D -m 0755 -o root -g root "${ROOT}/packaging/crewkiosk-run" /usr/local/libexec/crewkiosk-run
$SUDO install -d -m 0750 -o root -g crewkiosk /etc/crewkiosk
echo "sk-ci-dummy-kiosk-key" \
  | "${ROOT}/scripts/ci/put-file.sh" 0600 crewkiosk:crewkiosk /etc/crewkiosk/model.credential
# A generic gateway: the stub never calls it, it only reports what the runner exported.
printf '%s\n' 'base_url=https://llm.example.lan' 'model=haiku-latest' \
  | "${ROOT}/scripts/ci/put-file.sh" 0640 root:crewkiosk /etc/crewkiosk/model.conf
$SUDO install -m 0644 -o root -g root "${ROOT}/packaging/crewkiosk.socket" \
  "${ROOT}/packaging/crewkiosk@.service" /etc/systemd/system/
$SUDO systemctl daemon-reload
$SUDO systemctl enable crewkiosk.socket
$SUDO systemctl restart crewkiosk.socket  # as the runbook's block: a re-run applies the unit (#128)
$SUDO test -S "$SOCK" || die "no socket at $SOCK"
log "crewkiosk.socket is listening on $SOCK"

# The research kiosk (#142; D-113), as the runbook installs it: the runner, the relay and the
# shared body, then two servers' units, so IT-31b can show each sees only its own broker sockets.
log "installing the research runner, the relay, the unit body and two servers' units"
$SUDO install -m 0755 -o root -g root "${ROOT}/packaging/crewkiosk-research-run" \
  /usr/local/libexec/crewkiosk-research-run
$SUDO install -m 0755 -o root -g root "${ROOT}/packages/kiosk/src/crewkiosk/relay.py" \
  /usr/local/libexec/crewkiosk-relay
$SUDO install -D -m 0755 -o root -g root "${ROOT}/packaging/crewkiosk-research-units" \
  /usr/local/sbin/crewkiosk-research-units
$SUDO install -D -m 0644 -o root -g root "${ROOT}/packaging/crewkiosk-research@.service" \
  /usr/local/share/crewkiosk/crewkiosk-research@.service
for server in $RESEARCH_SERVERS; do
  $SUDO /usr/local/sbin/crewkiosk-research-units "$server" >/dev/null
done
$SUDO systemctl daemon-reload
for server in $RESEARCH_SERVERS; do
  $SUDO systemctl enable "crewkiosk-research-$server.socket"
  $SUDO systemctl restart "crewkiosk-research-$server.socket"
  $SUDO test -S "/run/crewkiosk/research-$server.sock" || die "no research socket for $server"
done
log "research sockets are listening for: $RESEARCH_SERVERS"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "CREWKIOSK_SOCKET=${SOCK}" >>"$GITHUB_ENV"
  echo "CREWKIOSK_RESEARCH_SERVERS=${RESEARCH_SERVERS}" >>"$GITHUB_ENV"
fi
echo "export CREWKIOSK_SOCKET=${SOCK}"
echo "export CREWKIOSK_RESEARCH_SERVERS='${RESEARCH_SERVERS}'"
