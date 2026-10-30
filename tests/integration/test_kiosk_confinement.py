"""IT-31b (#142; D-111, D-113): inside a research kiosk unit, only its own server's broker is
reachable.

`scripts/ci/kiosk-up.sh` installs the research kiosk as the runbook does: the runner, the
relay, the shared unit body and two servers' units (`itdev` and `itother`). Its stub `claude`
reports what its namespace holds whenever it runs in research mode. Here a fake broker holds
both servers' sockets and a control socket, all mode 0666, and a broker model key that
`crewkiosk` could read outside the unit: so only the unit stands between the kiosk and them.
Each connection goes through the real socket, unit, drop-in, runner (its uid, sockname and
header checks, and the hello) and relay; the test asserts what each instance saw. The last two
tests take the uid wall: each server's sockets, owned as the broker owns them, open only to that
server's user, and no instance can reach another server's live one through /proc or a signal.

Root via sudo, systemd, and kiosk-up.sh's install: it runs in `scripts/ci/installed-test.sh`.
Elsewhere it skips, unless `CREWCHAT_INSTALLED_REQUIRED=1`, which makes that a failure.
"""

from __future__ import annotations

import json
import os
import pwd
import socket
import subprocess
import sys
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


def ask_argv(via: str, server: str, question: str, answer: str) -> list[str]:
    header = f"crewkiosk/1 server={server} post=p1 answer={answer}"
    sock = f"/run/crewkiosk/research-{via}.sock"
    client = CLIENT.replace("THE-QUESTION", question)
    return ["sudo", "-n", "-u", "crewchat", "/usr/bin/python3", "-I", "-c", client, sock, header]


def ask(via: str, server: str, question: str = "THE-QUESTION", answer: str = ANSWER) -> bytes:
    """One answer, as the daemon asks it: as `crewchat`, on `via`'s research socket, framed
    for `server`."""
    proc = subprocess.run(ask_argv(via, server, question, answer), capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def research_user(server: str) -> str:
    return f"crewkiosk-r-{server}"


def instance_pids(server: str) -> list[int]:
    """Every process in `server`'s running research instances, by their units' cgroups: so the
    lookup depends on the unit, not on the user it runs as."""
    listed = sudo(
        "systemctl", "list-units", "--plain", "--no-legend", f"crewkiosk-research-{server}@*"
    ).stdout
    pids: list[int] = []
    for line in listed.splitlines():
        unit = line.split()[0]
        cgroup = sudo("systemctl", "show", "-P", "ControlGroup", unit).stdout.strip()
        if cgroup:
            procs = sudo("cat", f"/sys/fs/cgroup{cgroup}/cgroup.procs").stdout
            pids += [int(p) for p in procs.split()]
    return pids


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
    # The server's own user: neither crewkiosk nor the other server's.
    assert probe["uid"] == pwd.getpwnam(research_user(mine)).pw_uid
    assert probe["uid"] != pwd.getpwnam(research_user(other)).pw_uid
    assert probe["uid"] != pwd.getpwnam("crewkiosk").pw_uid
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
    # DNS is asked raw, at systemd-resolved's stub and at a public resolver, since glibc with
    # no resolv.conf would only try 127.0.0.1:53, where nothing listens even unconfined.
    assert probe["tcp"] != "connected"
    assert set(probe["dns"]) == {"127.0.0.53", "1.1.1.1"}  # type: ignore[arg-type]
    assert "reply" not in probe["dns"].values(), probe["dns"]  # type: ignore[attr-defined]
    # Setuid binaries are there (/usr/bin is whole), and buy nothing: NoNewPrivs is the guard.
    assert probe["setuid"], "no setuid binary in the namespace: this check would prove nothing"
    assert probe["no_new_privs"] == "1"
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


CONNECT = """
import socket, sys
s = socket.socket(socket.AF_UNIX)
try:
    s.connect(sys.argv[1])
    print("connected")
except OSError as e:
    print(type(e).__name__)
"""


def test_broker_sockets_open_only_to_their_servers_user(
    servers: list[str], broker: FakeBroker
) -> None:
    """The uid wall, with the mount wall out of the way: outside any unit, by path, each
    server's broker sockets, owned as crewkiosk-broker owns them, open to that server's user
    and to no other server's, nor to crewkiosk."""
    own = (
        "import sys; from pathlib import Path; from crewkiosk.broker import SOCKET_GROUP,"
        " own_socket; own_socket(Path(sys.argv[1]), SOCKET_GROUP, sys.argv[2])"
    )
    socks = {s: [BROKER_DIR / f"{s}.sock", BROKER_DIR / f"{s}.model.sock"] for s in servers}
    try:
        for server, paths in socks.items():
            for path in paths:
                done = sudo(sys.executable, "-c", own, str(path), server)
                assert done.returncode == 0, done.stderr
        for user in [research_user(s) for s in servers] + ["crewkiosk"]:
            for server, paths in socks.items():
                for path in paths:
                    got = sudo("-u", user, "/usr/bin/python3", "-I", "-c", CONNECT, str(path))
                    want = "connected" if user == research_user(server) else "PermissionError"
                    assert got.stdout.strip() == want, (user, str(path), got.stdout, got.stderr)
    finally:
        for paths in socks.values():
            sudo("chmod", "0666", *map(str, paths))


def test_no_instance_reaches_another_servers_live_one(
    servers: list[str], broker: FakeBroker
) -> None:
    """With the other server's instance running, by its host pids and by a walk of /proc,
    this one can't signal it, read its environment (its answer id), or reach its broker
    sockets through /proc/<pid>/root. The uid wall does that on any systemd: with both servers
    on one uid and no PrivatePIDs (systemd 255), this test fails."""
    mine, other = servers
    other_answer = "fedcba9876543210fedcba9876543210"
    lingering = subprocess.Popen(
        ask_argv(other, other, "LINGER", other_answer),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        pids: list[int] = []
        for _ in range(60):
            pids = instance_pids(other)
            if len(pids) >= 2:  # the runner and its kiosk, at least
                break
            time.sleep(0.5)
        assert len(pids) >= 2, f"{other}'s instance never started: {pids}"
        question = f"OTHER server={other} answer={other_answer} pids={','.join(map(str, pids))}"
        found = probe_of(ask(mine, mine, question))["others"]
        assert isinstance(found, dict) and found["walked"] > 0, found
        assert found["found"], found  # one kill entry per pid, at least
        kills = {k: v for k, v in found["found"].items() if k.startswith("kill ")}
        assert len(kills) == len(pids), found
        assert "signalled" not in kills.values(), found
        assert not {k for k in found["found"] if not k.startswith("kill ")}, found
    finally:
        lingering.terminate()
        lingering.wait(timeout=30)
