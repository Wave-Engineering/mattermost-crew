"""IT-31b (#142; D-111, D-113): inside a research kiosk unit, only its own server's broker is
reachable.

`scripts/ci/kiosk-up.sh` installs the research kiosk as the runbook does: the runner, the
relay, the shared unit body and two servers' units (`itdev` and `itother`). Its stub `claude`
reports what its namespace holds whenever it runs in research mode. Here a fake broker holds
both servers' sockets and a control socket, all mode 0666, and a broker model key that
`crewkiosk` could read outside the unit: so only the unit stands between the kiosk and them.
Each connection goes through the real socket, unit, drop-in, runner (its uid, sockname and
header checks, and the hello) and relay; the test asserts what each instance saw.

Root via sudo, systemd, and kiosk-up.sh's install: it runs in `scripts/ci/installed-test.sh`.
Elsewhere it skips, unless `CREWCHAT_INSTALLED_REQUIRED=1`, which makes that a failure.
"""

from __future__ import annotations

import json
import os
import pwd
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

SERVERS = os.environ.get("CREWKIOSK_RESEARCH_SERVERS", "").split()
BROKER_DIR = Path("/run/crewkiosk-broker")
CONTROL_DIR = Path("/run/crewkiosk-broker-control")
BROKER_KEY = Path("/etc/crewkiosk/broker-model.credential")
ANSWER = "0123456789abcdef0123456789abcdef"
# Only these may exist at the namespace's root: the empty tmpfs and its mount points.
ROOT_ENTRIES = {"bin", "dev", "etc", "home", "lib", "lib64", "proc", "run", "sys", "tmp", "usr"}

CLIENT = """
import socket, sys
c = socket.socket(socket.AF_UNIX)
c.settimeout(60)
c.connect(sys.argv[1])
c.sendall(sys.argv[2].encode() + b"\\n" + b"THE-QUESTION")
c.shutdown(socket.SHUT_WR)
out = b""
try:
    while True:
        data = c.recv(65536)
        if not data:
            break
        out += data
except ConnectionResetError:
    pass
sys.stdout.buffer.write(out)
"""


def sudo(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sudo", "-n", *argv], capture_output=True, text=True, timeout=90)


@pytest.fixture(scope="module")
def servers() -> list[str]:
    if len(SERVERS) != 2:
        reason = "no research units: run scripts/ci/kiosk-up.sh"
        if os.environ.get("CREWCHAT_INSTALLED_REQUIRED") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return SERVERS


class FakeBroker:
    """Both servers' op and model sockets and a control socket, each 0666, admitting every
    hello. It records which socket each connection came to."""

    def __init__(self, servers: list[str]) -> None:
        self.seen: list[str] = []
        self.listeners: list[socket.socket] = []
        names = [f"{s}{suffix}" for s in servers for suffix in (".sock", ".model.sock")]
        paths = [BROKER_DIR / n for n in names] + [CONTROL_DIR / "control.sock"]
        for path in paths:
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(path))
            os.chmod(path, 0o666)
            listener.listen(8)
            self.listeners.append(listener)
            threading.Thread(target=self._serve, args=(listener, path.name), daemon=True).start()

    def _serve(self, listener: socket.socket, name: str) -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            self.seen.append(name)
            with conn:
                conn.settimeout(5)
                data = b""
                try:
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    if json.loads(data or b"{}").get("id") == 0:
                        reply = {"jsonrpc": "2.0", "id": 0, "result": {"admitted": True}}
                        conn.sendall(json.dumps(reply).encode() + b"\n")
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        for listener in self.listeners:
            listener.close()


@pytest.fixture(scope="module")
def broker(servers: list[str]) -> Iterator[FakeBroker]:
    for path in (BROKER_DIR, CONTROL_DIR, BROKER_KEY):
        # As root: /etc/crewkiosk is 0750 root:crewkiosk.
        if sudo("test", "-e", str(path)).returncode == 0:
            pytest.fail(f"{path} exists: IT-31b needs a host with no real kiosk broker")
    me = str(os.getuid())
    made = sudo("install", "-d", "-m", "0755", "-o", me, str(BROKER_DIR), str(CONTROL_DIR))
    assert made.returncode == 0, made.stderr
    # A key crewkiosk *could* read, outside the unit: only the unit may hide it.
    put = ["install", "-m", "0600", "-o", "crewkiosk", "-g", "crewkiosk", "/dev/null"]
    assert sudo(*put, str(BROKER_KEY)).returncode == 0
    fake = FakeBroker(servers)
    try:
        yield fake
    finally:
        fake.close()
        sudo("rm", "-rf", "--", str(BROKER_DIR), str(CONTROL_DIR), str(BROKER_KEY))


def ask(via: str, server: str) -> bytes:
    """One answer, as the daemon asks it: as `crewchat`, on `via`'s research socket, framed
    for `server`."""
    header = f"crewkiosk/1 server={server} post=p1 answer={ANSWER}"
    sock = f"/run/crewkiosk/research-{via}.sock"
    proc = subprocess.run(
        ["sudo", "-n", "-u", "crewchat", "/usr/bin/python3", "-I", "-c", CLIENT, sock, header],
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def probe_of(reply: bytes) -> dict[str, object]:
    lines = reply.decode().splitlines()
    assert lines and lines[0] == "ok", reply
    found = [line.removeprefix("probe=") for line in lines if line.startswith("probe=")]
    assert len(found) == 1, reply
    return json.loads(found[0])  # type: ignore[no-any-return]


@pytest.mark.parametrize("which", [0, 1])
def test_each_research_unit_sees_only_its_own_server(
    servers: list[str], broker: FakeBroker, which: int
) -> None:
    """Two servers on one body: each instance holds only its own server's two broker sockets,
    reaches them, and can reach nothing else, not even what is mode 0666 outside it."""
    mine, other = servers[which], servers[1 - which]
    broker.seen.clear()
    probe = probe_of(ask(mine, mine))
    assert probe["uid"] == pwd.getpwnam("crewkiosk").pw_uid
    assert set(probe["root"]) <= ROOT_ENTRIES, probe["root"]  # type: ignore[arg-type]
    assert probe["broker_dir"] == [f"{mine}.model.sock", f"{mine}.sock"]
    assert probe["connect"] == {
        f"{mine}.sock": "connected",
        f"{mine}.model.sock": "connected",
        f"{other}.sock": "FileNotFoundError",
        f"{other}.model.sock": "FileNotFoundError",
    }
    assert probe["control"] in ("FileNotFoundError", "PermissionError")
    for path, got in probe["read"].items():  # type: ignore[attr-defined]
        expected = {"listed:0"} if path == "/home" else {"FileNotFoundError", "PermissionError"}
        assert got in expected, (path, got)
    # /usr/bin is bound whole, so curl and the like are there: the network wall is what holds.
    assert probe["tcp"] != "connected" and probe["dns"] != "connected"
    # And a setuid binary buys nothing: no new privileges, so the uid never changes.
    assert probe["no_new_privs"] == "1"
    for path, result in probe["setuid"].items():  # type: ignore[attr-defined]
        assert result["out"] != "0", (path, result)
    # The broker heard this server's hello on its own socket, and nothing on the other's.
    assert f"{mine}.sock" in broker.seen
    assert not {f"{other}.sock", f"{other}.model.sock", "control.sock"} & set(broker.seen)


def test_a_frame_for_another_server_gets_nothing(servers: list[str], broker: FakeBroker) -> None:
    """D-113: on one server's socket, a header naming the other is refused by the runner (67)
    before any hello: no reply, and the broker hears nothing."""
    mine, other = servers
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    broker.seen.clear()
    assert ask(mine, other) == b""
    assert broker.seen == []
    journal = ""
    for _ in range(20):
        journal = sudo(
            "journalctl",
            "--no-pager",
            "-o",
            "cat",
            "--since",
            since,
            "-u",
            f"crewkiosk-research-{mine}@*",
        ).stdout
        if "a header for another server" in journal:
            break
        time.sleep(0.5)
    assert "a header for another server" in journal, journal
