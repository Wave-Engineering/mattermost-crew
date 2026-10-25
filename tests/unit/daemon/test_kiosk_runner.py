"""Story 3.5: the kiosk's runner has no tools (R-31, R-08; D-096).

The daemon runs `sudo -n -u crewkiosk -- /usr/local/libexec/crewkiosk-run`, no arguments, the
prompt on stdin. The wrapper (`packaging/crewkiosk-run`) holds the CLI flags, the empty HOME
and working directory, and the model credential. Here it runs for real, with its fixed paths
pointed at a fake `claude` that records what it was given.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from crewchat.kiosk import (
    CLAUDE_FLAGS,
    KIOSK_RUN,
    KIOSK_USER,
    ClaudeCliRunner,
    Completed,
    RunnerError,
    spawn_process,
)

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "packaging" / "crewkiosk-run"
SUDOERS = ROOT / "packaging" / "crewkiosk.sudoers"

FAKE_CLAUDE = """#!/bin/bash
out="$RECORD_DIR_PLACEHOLDER"
printf '%s\\n' "$@" >"$out/argv"
echo "$#" >"$out/argc"
ls -A "$HOME" >"$out/home"
pwd >"$out/pwd"
ls -A . >"$out/work"
echo "$HOME" >"$out/homedir"
env >"$out/env"
cat >"$out/stdin"
echo "the answer"
"""


class Spawned:
    def __init__(self, result: Completed) -> None:
        self.calls: list[tuple[list[str], bytes, float]] = []
        self.result = result

    async def __call__(self, argv: Sequence[str], stdin: bytes, timeout_s: float) -> Completed:
        self.calls.append((list(argv), stdin, timeout_s))
        return self.result


def test_runner_has_no_tools() -> None:
    """The CLI runner: as crewkiosk, through the fixed sudo rule, no argument from us; the
    wrapper runs `claude -p --restricted --strict-mcp-config --tools ""` with the MCP
    exclusions, and nothing else."""
    spawned = Spawned(Completed(0, b"  We build a chat daemon.\n", b""))
    runner = ClaudeCliRunner(spawn=spawned)
    answer = asyncio.run(runner.answer("PROMPT-TEXT"))
    assert answer == "We build a chat daemon."
    [(argv, stdin, _timeout)] = spawned.calls
    assert argv == ["sudo", "-n", "-u", "crewkiosk", "--", "/usr/local/libexec/crewkiosk-run"]
    assert KIOSK_USER == "crewkiosk" and argv[-1] == KIOSK_RUN
    assert stdin == b"PROMPT-TEXT"  # the prompt never goes on a command line
    assert "crewchat" not in argv

    # The wrapper's one CLI invocation is exactly those flags.
    calls = [
        shlex.split(line)
        for line in WRAPPER.read_text().splitlines()
        if line.startswith('"$CLAUDE"') or line.startswith("exec ")
    ]
    assert calls == [["$CLAUDE", *CLAUDE_FLAGS]]
    assert CLAUDE_FLAGS == (
        "-p",
        "--restricted",
        "--strict-mcp-config",
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
    )
    assert re.search(r"^KIOSK_USER=crewkiosk$", WRAPPER.read_text(), re.M)
    assert re.search(r"^CREDENTIAL=/etc/crewkiosk/model.credential$", WRAPPER.read_text(), re.M)


def test_runner_failure_is_an_error() -> None:
    runner = ClaudeCliRunner(spawn=Spawned(Completed(1, b"", b"sudo: a password is required")))
    with pytest.raises(RunnerError, match="exited 1: sudo: a password"):
        asyncio.run(runner.answer("x"))


def test_sudoers_allows_only_the_wrapper() -> None:
    rules = [
        line
        for line in SUDOERS.read_text().splitlines()
        if line.strip() and not line.startswith("#") and not line.startswith("Defaults")
    ]
    assert rules == [f'crewchat ALL=(crewkiosk) NOPASSWD: {KIOSK_RUN} ""']


def sandboxed_wrapper(tmp_path: Path, user: str) -> tuple[Path, Path, Path]:
    """The wrapper with its fixed paths pointed into `tmp_path`: (script, record dir, key)."""
    record = tmp_path / "record"
    record.mkdir()
    claude = tmp_path / "claude"
    claude.write_text(FAKE_CLAUDE.replace("$RECORD_DIR_PLACEHOLDER", str(record)))
    claude.chmod(0o755)
    key = tmp_path / "model.credential"
    key.write_text("sk-kiosk-test-key\n")
    key.chmod(0o600)
    text = WRAPPER.read_text()
    for name, value in (("KIOSK_USER", user), ("CREDENTIAL", str(key)), ("CLAUDE", str(claude))):
        text, n = re.subn(rf"^{name}=.*$", f"{name}={value}", text, flags=re.M)
        assert n == 1, name
    script = tmp_path / "crewkiosk-run"
    script.write_text(text)
    script.chmod(0o755)
    return script, record, key


def test_wrapper_runs_claude_bare(tmp_path: Path) -> None:
    script, record, _key = sandboxed_wrapper(tmp_path, getpass.getuser())
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(Path.home()),
        "CREWCHAT_TOKEN": "LEAKED-CHAT-TOKEN",
        "ANTHROPIC_BASE_URL": "http://evil.invalid",
    }
    proc = subprocess.run(
        [str(script)], input=b"THE-PROMPT", capture_output=True, env=env, cwd=tmp_path, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"the answer\n"
    argv = (record / "argv").read_text().split("\n")[:-1]
    assert tuple(argv) == CLAUDE_FLAGS and (record / "argc").read_text().strip() == "7"
    assert (record / "stdin").read_bytes() == b"THE-PROMPT"
    # An empty HOME and an empty working directory, both fresh and gone afterwards.
    assert (record / "home").read_text() == "" and (record / "work").read_text() == ""
    home = Path((record / "homedir").read_text().strip())
    work = Path((record / "pwd").read_text().strip())
    assert home != Path.home() and work != tmp_path and home.parent == work.parent
    assert not home.exists() and not work.exists()
    # Nothing inherited: the model key, HOME and PATH, and bash's own.
    env_out = dict(
        line.split("=", 1) for line in (record / "env").read_text().splitlines() if "=" in line
    )
    assert "LEAKED-CHAT-TOKEN" not in json.dumps(env_out)
    assert "ANTHROPIC_BASE_URL" not in env_out
    assert env_out["ANTHROPIC_API_KEY"] == "sk-kiosk-test-key"
    assert env_out["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert set(env_out) <= {"ANTHROPIC_API_KEY", "HOME", "PATH", "PWD", "SHLVL", "_", "OLDPWD"}


def test_wrapper_refuses_another_user(tmp_path: Path) -> None:
    script, record, _ = sandboxed_wrapper(tmp_path, "no-such-kiosk-user")
    proc = subprocess.run([str(script)], input=b"x", capture_output=True, timeout=30)
    assert proc.returncode == 64 and b"must run as no-such-kiosk-user" in proc.stderr
    assert not (record / "argv").exists()


def test_wrapper_needs_its_credential(tmp_path: Path) -> None:
    script, record, key = sandboxed_wrapper(tmp_path, getpass.getuser())
    key.write_text("")
    proc = subprocess.run([str(script)], input=b"x", capture_output=True, timeout=30)
    assert proc.returncode != 0 and not (record / "argv").exists()
    key.unlink()
    proc = subprocess.run([str(script)], input=b"x", capture_output=True, timeout=30)
    assert proc.returncode != 0 and not (record / "argv").exists()


def test_spawn_process(run: Any) -> None:
    done = run(spawn_process(["/bin/cat"], b"echoed", 10.0))
    assert done == Completed(0, b"echoed", b"")
    with pytest.raises(RunnerError, match="timed out"):
        run(spawn_process(["/bin/sleep", "10"], b"", 0.2))
