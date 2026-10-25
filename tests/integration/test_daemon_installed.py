"""IT-01: the installed daemon, broker and Mattermost, on the CI leg's two-user image.

The broker creates the bot account and mints a token, and the daemon, running as `crewchat`,
stores it. Each known credential file exists and the `agent` user cannot read it, checked by
name, never by glob (R-08). No socket op returns a token to a peer that isn't the daemon. The
patched role is held only by the configured broker accounts, the `broker_user_id` entries in
this host's `servers.toml` (R-06; in CI every broker is on this host).

These tests need what `scripts/ci/broker-up.sh` and `scripts/ci/daemon-up.sh` install, so they
run in their own CI step (`scripts/ci/installed-test.sh`) after both. Elsewhere they skip,
unless `CREWCHAT_INSTALLED_REQUIRED=1`, which makes a missing install a failure.
"""

from __future__ import annotations

import json
import os
import pwd
import secrets
import subprocess
import sys
import tomllib
from typing import Any

import pytest

from tests.integration.mmhttp import mm_request

AGENT_USER = os.environ.get("AGENT_USER", "agent")
KIOSK_USER = "crewkiosk"
TOKENS_DIR = "/var/lib/crewchat/tokens"
PATCHED_ROLE = "system_user_access_token"

PEERCRED = """
import socket, struct, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
pid, uid, gid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
print(uid)
"""


def _installed() -> tuple[str, str]:
    sock = os.environ.get("CREWCHAT_SOCKET", "").strip()
    binary = os.environ.get("CREWCHAT_BIN", "").strip()
    if not sock or not binary:
        reason = "no installed daemon: run scripts/ci/broker-up.sh and scripts/ci/daemon-up.sh"
        if os.environ.get("CREWCHAT_INSTALLED_REQUIRED") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return sock, binary


def run(*cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=60, check=False)


def as_agent(*cmd: str) -> subprocess.CompletedProcess[str]:
    return run("sudo", "-n", "-u", AGENT_USER, "--", *cmd)


def as_root(*cmd: str) -> subprocess.CompletedProcess[str]:
    return run("sudo", "-n", "--", *cmd)


def daemon_call(binary: str, sock: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
    proc = as_agent(binary, "call", sock, op, json.dumps(args))
    assert proc.stdout, proc.stderr
    reply: dict[str, Any] = json.loads(proc.stdout)
    return reply


@pytest.fixture(scope="module")
def installed() -> dict[str, Any]:
    sock, binary = _installed()
    proc = as_root("cat", "/etc/crewchat/servers.toml")
    assert proc.returncode == 0, proc.stderr
    return {"socket": sock, "bin": binary, "servers": tomllib.loads(proc.stdout)}


@pytest.fixture(scope="module")
def registered(installed: dict[str, Any]) -> dict[str, Any]:
    """An agent registered through the daemon, as the agent's user."""
    dev = f"it01-{secrets.token_hex(3)}"
    reply = daemon_call(
        installed["bin"], installed["socket"], "register", {"dev_name": dev, "session_id": "it01"}
    )
    assert reply["ok"] is True, reply
    proc = as_root("cat", f"{TOKENS_DIR}/{dev}.json")
    assert proc.returncode == 0, proc.stderr
    return {"dev": dev, "reply": reply, "file": json.loads(proc.stdout)}


def broker_instances(servers: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    for crew, table in servers["crews"].items():
        for key, value in table.items():
            if isinstance(value, dict):
                out.append((crew, key, value))
    return out


def test_daemon_runs_as_crewchat(installed: dict[str, Any]) -> None:
    proc = as_root(sys.executable, "-I", "-c", PEERCRED, installed["socket"])
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout) == pwd.getpwnam("crewchat").pw_uid


def test_register_names_the_account(installed: dict[str, Any], registered: dict[str, Any]) -> None:
    """R-01: the daemon's register answers `<dev>.<crew>`, with the crew's needs_list."""
    crew = installed["servers"]["default_crew"]
    result = registered["reply"]["result"]
    assert result["account"] == f"{registered['dev']}.{crew}"
    assert result["needs_list"] == installed["servers"]["crews"][crew]["needs_list"]
    assert registered["file"]["crew"] == crew


def credential_files(installed: dict[str, Any], registered: dict[str, Any]) -> list[str]:
    """Every chat and broker credential file on the host, by its KNOWN name. No globs."""
    files = [f"{TOKENS_DIR}/{registered['dev']}.json", f"{TOKENS_DIR}/verify-canary.json"]
    for crew, server_id, _ in broker_instances(installed["servers"]):
        files.append(f"/etc/crewbroker/{crew}-{server_id}.credential")
        files.append(f"/var/lib/crewbroker/{crew}-{server_id}/tokens.json")
    if installed["servers"].get("discord", {}).get("enabled"):
        files.append("/etc/crewchat/discord.credential")
    return files


def test_credential_files_not_readable_by_agent(
    installed: dict[str, Any], registered: dict[str, Any]
) -> None:
    """R-08: each KNOWN file exists, and the agent's user cannot read it. No globs."""
    # A refused sudo also exits non-zero, which would read as "cannot read": prove it works.
    assert as_agent("true").returncode == 0, f"cannot run commands as {AGENT_USER}"
    for path in credential_files(installed, registered):
        assert as_root("test", "-e", path).returncode == 0, f"{path} does not exist"
        assert as_agent("test", "-r", path).returncode != 0, f"{AGENT_USER} can read {path}"


def test_credential_files_not_readable_by_crewkiosk(
    installed: dict[str, Any], registered: dict[str, Any]
) -> None:
    """R-08, R-31 (Story 3.5): the kiosk's CLI runner runs as `crewkiosk`, which can read no
    chat or broker credential. Each KNOWN file is checked with `test -r`, as `crewkiosk`."""
    pwd.getpwnam(KIOSK_USER)  # the CLI runner is the kiosk (D-096): the user must exist
    as_kiosk = ("sudo", "-n", "-u", KIOSK_USER, "--")
    assert run(*as_kiosk, "true").returncode == 0, f"cannot run commands as {KIOSK_USER}"
    for path in credential_files(installed, registered):
        assert as_root("test", "-e", path).returncode == 0, f"{path} does not exist"
        assert run(*as_kiosk, "test", "-r", path).returncode != 0, f"{KIOSK_USER} can read {path}"


def test_no_socket_op_returns_a_token(
    installed: dict[str, Any], registered: dict[str, Any]
) -> None:
    """R-08: nothing the agent's user can send to a socket comes back with a token."""
    tokens = [t["token"] for t in registered["file"]["servers"].values()]
    assert tokens and all(tokens)
    binary, sock = installed["bin"], installed["socket"]
    replies = [
        json.dumps(registered["reply"]),
        json.dumps(daemon_call(binary, sock, "health", {})),
        json.dumps(daemon_call(binary, sock, "no_such_op", {"dev_name": registered["dev"]})),
        json.dumps(daemon_call(binary, sock, "token_for", {"dev_name": registered["dev"]})),
    ]
    # The brokers' sockets refuse the agent's user outright.
    crewbroker = os.path.join(os.path.dirname(binary), "crewbroker")
    crew = installed["servers"]["default_crew"]
    for _, _, table in broker_instances(installed["servers"]):
        for op, args in (
            ("mint", {"dev_name": registered["dev"], "crew": crew}),
            ("remint", {"dev_name": registered["dev"], "crew": crew}),
        ):
            proc = as_agent(crewbroker, "call", table["broker_socket"], op, json.dumps(args))
            assert proc.returncode != 0
            replies.append(proc.stdout + proc.stderr)
    for text in replies:
        for token in tokens:
            assert token not in text
        assert '"token"' not in text and '"token_id"' not in text


def test_minted_token_works(
    mattermost: Any, installed: dict[str, Any], registered: dict[str, Any]
) -> None:
    """R-03, R-05: the broker created the bot account, and the token the daemon holds works."""
    crew = installed["servers"]["default_crew"]
    for held in registered["file"]["servers"].values():
        resp = mm_request(mattermost, "GET", "/users/me", token=held["token"])
        assert resp.status_code == 200, resp.text
        me = resp.json()
        assert me["username"] == f"{registered['dev']}.{crew}"
        assert me["is_bot"] is True


def test_patched_role_held_only_by_configured_brokers(
    mattermost: Any, installed: dict[str, Any]
) -> None:
    """R-06: exactly the `broker_user_id` entries in servers.toml hold the patched role."""
    configured = {t["broker_user_id"] for _, _, t in broker_instances(installed["servers"])}
    assert configured
    holders: dict[str, set[str]] = {}
    page = 0
    while True:
        resp = mm_request(mattermost, "GET", "/users", params={"page": page, "per_page": 200})
        assert resp.status_code == 200, resp.text
        batch = resp.json()
        for user in batch:
            roles = set(str(user.get("roles", "")).split())
            if PATCHED_ROLE in roles:
                holders[user["id"]] = roles
        if len(batch) < 200:
            break
        page += 1
    assert set(holders) == configured
    for roles in holders.values():
        assert "system_admin" not in roles
