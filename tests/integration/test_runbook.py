"""IT-20: the runbook's own install and verify commands, run on a fresh host (R-06, R-08).

Every fenced `bash` block under the "Install …" headings and under "Verify the install" in
`docs/runbook.md` is extracted (`tests/runbook_md.py`) and run in document order, each in a new
`bash` from the repository root, as the runbook tells an operator to. Every block must exit 0.
Before them, `scripts/ci/runbook-configure.sh` does the runbook's Configure step with this CI
host's values, as the operator would before installing. It then sets `/etc/crewbroker` to 0700,
as the first real host had it (#115): the broker block must fix the mode, or the broker can't
reach its credential. After the blocks, the daemon is restarted and its socket must come back
with the same ACL, which the unit's drop-in sets on every start.

The blocks create OS users, install into `/opt/mattermost-crew` and start systemd units, so this
test runs only where `CREWCHAT_RUNBOOK_IT=1` says the host is a fresh throwaway: the IT-20 job,
`scripts/ci/runbook-test.sh`, on the ephemeral pool. Everywhere else it skips. With the flag set,
a missing Mattermost server is a failure, not a skip.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from tests.runbook_md import ROOT, RUNBOOK, runbook_blocks

BLOCK_TIMEOUT_S = 900


def _journal() -> str:
    proc = subprocess.run(
        ["sudo", "-n", "journalctl", "-u", "crewchat", "-u", "crewbroker@*", "-u", "crewkiosk*",
         "-n", "80", "--no-pager"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def fresh_host() -> dict[str, str]:
    if os.environ.get("CREWCHAT_RUNBOOK_IT") != "1":
        pytest.skip(
            "IT-20 changes the host: it runs only on a fresh CI host, with CREWCHAT_RUNBOOK_IT=1 "
            "(scripts/ci/runbook-test.sh)"
        )
    env = dict(os.environ)
    for key in ("MM_URL", "MM_ADMIN_TOKEN", "CREWBROKER_INSTANCES"):
        if not env.get(key, "").strip():
            pytest.fail(f"{key} is unset; run scripts/ci/mattermost-up.sh and runbook-test.sh")
    env.setdefault("AGENT_USER", "agent")
    configure = subprocess.run(
        [str(ROOT / "scripts" / "ci" / "runbook-configure.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert configure.returncode == 0, f"Configure failed:\n{configure.stdout}\n{configure.stderr}"
    _sudo("chmod", "0700", "/etc/crewbroker")
    return env


def _sudo(*argv: str) -> str:
    proc = subprocess.run(
        ["sudo", "-n", *argv], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, f"sudo {' '.join(argv)}: {proc.stdout}{proc.stderr}"
    return proc.stdout


def test_runbook_install_and_verify_blocks_run_clean(fresh_host: dict[str, str]) -> None:
    blocks = runbook_blocks(RUNBOOK.read_text(encoding="utf-8"))
    assert blocks, "docs/runbook.md has no install or verify blocks"
    for n, (heading, script) in enumerate(blocks, 1):
        proc = subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            env=fresh_host,
            capture_output=True,
            text=True,
            timeout=BLOCK_TIMEOUT_S,
            check=False,
        )
        print(f"--- block {n} ({heading}): exit {proc.returncode}\n{proc.stdout}{proc.stderr}")
        assert proc.returncode == 0, (
            f"runbook block {n} under '{heading}' exited {proc.returncode}\n"
            f"--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}\n--- journal\n{_journal()}"
        )


def test_socket_acl_survives_a_daemon_restart(fresh_host: dict[str, str]) -> None:
    """Runs after the blocks: systemd recreates /run/crewchat on every start, and the drop-in's
    ExecStartPre puts the default ACL back before the daemon makes its socket."""
    sock = "/run/crewchat/crewchat.sock"
    _sudo("systemctl", "restart", "crewchat")
    for _ in range(30):
        if subprocess.run(["sudo", "-n", "test", "-S", sock], check=False).returncode == 0:
            break
        time.sleep(1)
    acl = _sudo("getfacl", "-p", sock).splitlines()
    for want in (f"user:{fresh_host['AGENT_USER']}:rw-", "group::rw-", "mask::rw-"):
        assert want in acl, f"{sock} lacks {want} after a restart:\n" + "\n".join(acl)
