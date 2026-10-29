"""#115, #123: what the runbook's install blocks do about access and file writes, checked
without a host.

IT-20 (`tests/integration/test_runbook.py`) runs these blocks for real on a fresh CI host. These
tests pin, in the text IT-20 extracts (`tests/runbook_md.py`), the fixes from the first real
installs: the daemon's ACL drop-in for live agent sessions, `/etc/crewbroker`'s mode, and file
writes that work over an existing file on uutils coreutils.
"""

from __future__ import annotations

import getpass
import grp
import os
import re
import stat
import subprocess
from pathlib import Path

from tests.runbook_md import ROOT as REPO
from tests.runbook_md import RUNBOOK, runbook_blocks, sections

DROPIN_DIR = "/etc/systemd/system/crewchat.service.d"
DROPIN = f"{DROPIN_DIR}/agent-acl.conf"


def block(heading: str) -> str:
    (script,) = [s for h, s in runbook_blocks(RUNBOOK.read_text(encoding="utf-8")) if h == heading]
    return script


def line_of(script: str, needle: str) -> int:
    """The index of the first line of `script` that contains `needle`; fails when none does."""
    for n, line in enumerate(script.splitlines()):
        if needle in line:
            return n
    raise AssertionError(f"no line contains {needle!r}")


def test_daemon_block_writes_acl_dropin() -> None:
    script = block("Install the daemon")
    # The drop-in sets the default ACL with the agents' user substituted, and g::rw so the
    # group's access isn't capped at the directory's r-x.
    assert (
        "printf '[Service]\\nExecStartPre=+/usr/bin/setfacl -d -m u:%s:rw,g::rw /run/crewchat\\n'"
        ' "$AGENT_USER"'
    ) in script
    # Mode and owner first, then the content: never `install /dev/stdin` (#123).
    create = line_of(script, f"install -m 0644 -o root -g root /dev/null {DROPIN}")
    assert create < line_of(script, f"sudo tee {DROPIN} >/dev/null")
    assert f"install -d -m 0755 -o root -g root {DROPIN_DIR}" in script
    # After the unit, before daemon-reload, so the first start already has it.
    unit = line_of(script, "packaging/crewchat.service /etc/systemd/system/crewchat.service")
    assert unit < line_of(script, DROPIN_DIR) < line_of(script, "systemctl daemon-reload")
    assert line_of(script, DROPIN) < line_of(script, "systemctl daemon-reload")
    # The group stays: it is the containers' and new logins' path.
    assert 'usermod -aG crewchat-clients "$AGENT_USER"' in script
    # setfacl is checked up front, not discovered by a failed ExecStartPre.
    assert line_of(script, "[[ -x /usr/bin/setfacl ]]") < line_of(script, "systemctl restart")


def test_daemon_block_refuses_without_setfacl(tmp_path: Path) -> None:
    """The setfacl check's refusal branch, which IT-20 never takes: its runners get `acl` first
    (`scripts/ci/host-prereqs.sh`). The runbook's own line runs, with only the path swapped."""
    script = block("Install the daemon")
    check = script.splitlines()[line_of(script, "[[ -x /usr/bin/setfacl ]]")]
    assert "/usr/bin/setfacl" in check and "exit 1" in check

    def run(path: Path) -> subprocess.CompletedProcess[str]:
        line = check.replace("/usr/bin/setfacl", str(path))
        return subprocess.run(["bash", "-c", line], capture_output=True, text=True, check=False)

    missing = run(tmp_path / "setfacl")
    assert missing.returncode == 1
    assert "install acl (Prerequisites)" in missing.stderr
    present = tmp_path / "present"
    present.write_text("#!/bin/sh\n", encoding="utf-8")
    present.chmod(0o755)
    assert run(present).returncode == 0


# `install` reading a pipe: /dev/stdin, a /dev/fd or /proc/self/fd path, or a process
# substitution. Ubuntu's uutils `install` fails on it whenever the destination exists (#123).
PIPE_INSTALL = re.compile(r"\binstall\b[^\n]*(/dev/stdin|/dev/fd/|/proc/self/fd/|<\()")
SHELL_DIRS = ("scripts", "packaging")


def commands(text: str) -> list[str]:
    """`text`'s command lines: backslash continuations joined, so a split command is one line,
    and comment lines dropped, since a comment may name the form it warns against."""
    lines = text.replace("\\\n", " ").splitlines()
    return [line for line in lines if not line.lstrip().startswith("#")]


def fenced(markdown: str) -> str:
    """Every fenced block's body in `markdown`, `text` blocks included, without the prose."""
    out: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if line.startswith(("```", "~~~")):
            inside = not inside
        elif inside:
            out.append(line)
    return "\n".join(out)


def test_no_install_from_a_pipe() -> None:
    """GNU coreutils, which CI runs, accepts `install /dev/stdin` over an existing file, so no
    behaviour test in CI can catch it: this lint does, in every runbook block, fenced `text`
    included (an operator runs those too), and every shell script."""
    sources = {"docs/runbook.md": fenced(RUNBOOK.read_text(encoding="utf-8"))}
    sources.update(shell_scripts())
    assert len(sources) > 5  # the scripts were found
    hits = [
        f"{name}: {line.strip()}"
        for name, text in sources.items()
        for line in commands(text)
        if PIPE_INSTALL.search(line)
    ]
    assert hits == [], "write in two steps: install … /dev/null <dest>, then tee <dest>"


def test_lint_catches_a_pipe_install() -> None:
    """The lint itself: each pipe form is caught, split or not, and a comment is not."""
    for bad in (
        "printf x | sudo install -m 0644 -o root -g root /dev/stdin /etc/x",
        "echo k | install -m 0600 /dev/stdin \\\n  /etc/x",
        "install -m 0644 <(printf x) /etc/x",
        "install -m 0644 /proc/self/fd/0 /etc/x",
    ):
        assert any(PIPE_INSTALL.search(line) for line in commands(bad)), bad
    for good in ("# never `install /dev/stdin`", "sudo install -m 0644 /dev/null /etc/x"):
        assert not any(PIPE_INSTALL.search(line) for line in commands(good)), good


def test_two_step_write_over_an_existing_file(tmp_path: Path) -> None:
    """The daemon block's own drop-in write, run with this host's `install` onto a new file and
    then over it, as an upgrade does. On a uutils host this is the #123 regression test."""
    lines = block("Install the daemon").splitlines()
    first = line_of("\n".join(lines), f"/dev/null {DROPIN}")
    last = line_of("\n".join(lines), f"sudo tee {DROPIN}")
    assert 0 < last - first <= 2  # the two steps, the content's printf between them
    dest = tmp_path / "agent-acl.conf"
    steps = "\n".join(lines[first : last + 1]).replace("sudo ", "")
    steps = steps.replace("-o root -g root ", "").replace(DROPIN, str(dest))
    for agent in ("first", "second"):  # a fresh install, then an upgrade over it
        proc = subprocess.run(
            ["bash", "-c", f"set -euo pipefail\nAGENT_USER={agent}\n{steps}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert f"u:{agent}:rw" in dest.read_text(encoding="utf-8")
        assert stat.S_IMODE(dest.stat().st_mode) == 0o644


def test_ci_put_file_over_an_existing_file(tmp_path: Path) -> None:
    """`scripts/ci/put-file.sh`, the CI scripts' one way to write a file, with this host's
    `install`: a new file, then over it. A pass-through `sudo`, and this user as the owner, so
    it needs no root."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_sudo = bin_dir / "sudo"
    fake_sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    fake_sudo.chmod(0o755)
    owner = f"{getpass.getuser()}:{grp.getgrgid(os.getgid()).gr_name}"
    dest = tmp_path / "servers.toml"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    for content, mode in (("first\n", 0o644), ("second, longer than the first\n", 0o600)):
        proc = subprocess.run(
            [str(REPO / "scripts/ci/put-file.sh"), f"{mode:04o}", owner, str(dest)],
            input=content,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert dest.read_text(encoding="utf-8") == content
        assert stat.S_IMODE(dest.stat().st_mode) == mode
    bad = subprocess.run(
        [str(REPO / "scripts/ci/put-file.sh"), "0644", str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2 and "usage" in bad.stderr


ENABLE_NOW = re.compile(r"\bsystemctl\s+enable\b[^\n]*--now\b")


def shell_scripts() -> dict[str, str]:
    """Every bash script under `SHELL_DIRS`, by its repo-relative path."""
    found: dict[str, str] = {}
    for top in SHELL_DIRS:
        for path in sorted((REPO / top).rglob("*")):
            if path.is_file() and path.read_bytes().startswith(b"#!/usr/bin/env bash"):
                found[str(path.relative_to(REPO))] = path.read_text(encoding="utf-8")
    return found


def test_no_enable_now_in_install_blocks() -> None:
    """#128: `enable --now` starts a stopped unit and leaves an active one as it was, so a
    re-run that installs a changed unit never applies it. Every block and script enables, then
    restarts, as the Upgrade section's "restarts its units" says."""
    # A list, not a dict: one section can hold several blocks.
    sources = runbook_blocks(RUNBOOK.read_text(encoding="utf-8"))
    sources += list(shell_scripts().items())
    hits = [
        f"{name}: {line.strip()}"
        for name, text in sources
        for line in commands(text)
        if ENABLE_NOW.search(line)
    ]
    assert hits == [], "systemctl enable <unit>, then systemctl restart <unit>"
    for bad in ("sudo systemctl enable --now crewkiosk.socket", "systemctl enable x --now"):
        assert ENABLE_NOW.search(bad), bad
    assert not ENABLE_NOW.search("sudo systemctl disable --now crewchat crewkiosk.socket")


def test_kiosk_block_restarts_its_socket() -> None:
    script = block("Install the kiosk runner")
    installed = line_of(script, "packaging/crewkiosk.socket")
    assert installed < line_of(script, "systemctl enable crewkiosk.socket")
    assert installed < line_of(script, "systemctl restart crewkiosk.socket")


def test_broker_block_sets_crewbroker_mode() -> None:
    script = block("Install the broker")
    command = "sudo install -d -m 0755 -o root -g root /etc/crewbroker"
    fix = line_of(script, command)
    assert script.splitlines()[fix].strip() == command
    assert fix < line_of(script, "for entry in $CREWBROKER_INSTANCES")


def test_prerequisites_and_rollback_cover_the_dropin() -> None:
    body = dict(sections(RUNBOOK.read_text(encoding="utf-8")))
    assert "`acl`" in body["Prerequisites"]
    assert "`sg crewchat-clients`" in body["Prerequisites"]
    assert f"sudo rm -rf {DROPIN_DIR}" in body["Rollback"]
