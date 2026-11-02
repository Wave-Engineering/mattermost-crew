"""`scripts/ci/host-prereqs.sh` retries apt on a held lock, and only then (#220).

A fresh ephemeral VM still has the image's own apt-get running for its first minute, holding
/var/lib/apt/lists/lock. The script must wait that out, but must not hide a real apt failure. A fake
`apt-get` and `sudo` on PATH and an empty bin directory (so `acl` looks missing) stand in for the
host.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ci" / "host-prereqs.sh"

FAKE_APT = r"""#!/usr/bin/env bash
sub=update
for a in "$@"; do [ "$a" = install ] && sub=install; done
echo "$sub" >> "$APT_LOG"
n=$(grep -c "^$sub$" "$APT_LOG")
case "${FAKE_APT_MODE:-ok}" in
  lock-twice)
    if [ "$n" -le 2 ]; then
      echo "E: Could not get lock /var/lib/apt/lists/lock. It is held by process 99 (apt-get)" >&2
      echo "E: Unable to lock directory /var/lib/apt/lists/" >&2
      exit 100
    fi ;;
  lock-always)
    echo "E: Could not get lock /var/lib/apt/lists/lock. It is held by process 99 (apt-get)" >&2
    exit 100 ;;
  real-error)
    echo "E: Unable to locate package acl" >&2
    exit 100 ;;
  perm-denied)
    echo "E: Could not get lock /var/lib/dpkg/lock-frontend - open (13: Permission denied)" >&2
    exit 100 ;;
esac
if [ "$sub" = install ] && [ -z "${FAKE_APT_NO_SETFACL:-}" ]; then
  touch "$HOST_PREREQS_BIN_DIR/setfacl" "$HOST_PREREQS_BIN_DIR/getfacl"
  chmod +x "$HOST_PREREQS_BIN_DIR/setfacl" "$HOST_PREREQS_BIN_DIR/getfacl"
fi
exit 0
"""


class Host:
    def __init__(self, tmp: Path) -> None:
        self.bin = tmp / "hostbin"  # where setfacl/getfacl would be
        self.bin.mkdir()
        self.fake = tmp / "fakebin"  # fake apt-get and sudo, first on PATH
        self.fake.mkdir()
        (self.fake / "apt-get").write_text(FAKE_APT)
        (self.fake / "sudo").write_text('#!/usr/bin/env bash\nexec "$@"\n')
        for f in ("apt-get", "sudo"):
            (self.fake / f).chmod(0o755)
        self.log = tmp / "apt.log"
        self.log.write_text("")

    def run(self, mode: str = "ok", **env: str) -> subprocess.CompletedProcess[str]:
        e = {
            **os.environ,
            "PATH": f"{self.fake}:{os.environ['PATH']}",
            "HOST_PREREQS_BIN_DIR": str(self.bin),
            "HOST_PREREQS_APT_SLEEP": "0",
            "APT_LOG": str(self.log),
            "FAKE_APT_MODE": mode,
            **env,
        }
        return subprocess.run([str(SCRIPT)], capture_output=True, text=True, env=e, check=False)

    def calls(self) -> list[str]:
        return self.log.read_text().split()


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def test_installs_when_acl_is_missing(host: Host) -> None:
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert host.calls() == ["update", "install"]
    assert (host.bin / "setfacl").exists()


def test_a_lock_held_twice_is_waited_out_and_logged(host: Host) -> None:
    r = host.run("lock-twice")
    assert r.returncode == 0, r.stderr
    assert (
        host.calls() == ["update"] * 3 + ["install"] * 3
    )  # two lock errors, then success, for each step
    assert r.stderr.count("apt is locked (try") == 4
    assert "Could not get lock" in r.stderr  # apt's own message is still shown


def test_a_lock_that_never_clears_fails_after_exactly_the_configured_tries(host: Host) -> None:
    r = host.run("lock-always", HOST_PREREQS_APT_ATTEMPTS="4")
    assert r.returncode != 0
    assert host.calls() == ["update"] * 4  # it never gets to install
    assert "Could not get lock" in r.stderr


def test_a_real_apt_failure_is_not_retried(host: Host) -> None:
    r = host.run("real-error")
    assert r.returncode != 0
    assert host.calls() == ["update"]  # one attempt, no retry on an error that is not a lock
    assert "Unable to locate package acl" in r.stderr
    assert "apt is locked" not in r.stderr


def test_a_permission_denied_lock_error_is_not_retried(host: Host) -> None:
    """Waiting cannot fix a missing root or sudo, so a lock error that is EACCES fails at once."""
    r = host.run("perm-denied")
    assert r.returncode != 0
    assert host.calls() == ["update"]
    assert "Permission denied" in r.stderr and "apt is locked" not in r.stderr


def test_the_dpkg_lock_wait_stays_short_so_retries_do_not_multiply_it() -> None:
    """12 tries around a 120 s wait would near the job's 30 minutes on a lock that never clears."""
    text = SCRIPT.read_text(encoding="utf-8")
    waits = [int(n) for n in re.findall(r"DPkg::Lock::Timeout=(\d+)", text)]
    assert waits and max(waits) <= 30, waits


def test_nothing_to_install_never_calls_apt(host: Host) -> None:
    for tool in ("setfacl", "getfacl"):
        (host.bin / tool).write_text("#!/bin/sh\n")
        (host.bin / tool).chmod(0o755)
    r = host.run()
    assert r.returncode == 0 and "nothing to install" in r.stderr
    assert host.calls() == []


def test_a_successful_install_that_leaves_no_setfacl_still_fails(host: Host) -> None:
    r = host.run(FAKE_APT_NO_SETFACL="1")
    assert r.returncode == 1
    assert "acl installed, but no" in r.stderr


@pytest.mark.parametrize("value", ["0", "-3", "lots", ""])
def test_a_bad_attempts_setting_falls_back_to_the_default(host: Host, value: str) -> None:
    r = host.run("lock-twice", HOST_PREREQS_APT_ATTEMPTS=value)
    assert r.returncode == 0, r.stderr  # the default (12) is plenty for two lock errors
