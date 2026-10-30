"""Story 3.5: the kiosk's runner has no tools (R-31, R-08; D-096, D-107).

The daemon keeps `NoNewPrivileges=yes` and never changes user: `ClaudeCliRunner` connects to
`/run/crewkiosk/kiosk.sock`, writes the prompt, half-closes, and reads the framed reply. systemd
runs the wrapper (`packaging/crewkiosk-run`) as `crewkiosk`, one instance per connection; the
wrapper holds the CLI flags, the empty HOME and working directory, and the model credential.
Here the client talks to a real local socket, and the wrapper runs for real with its fixed
paths pointed at a fake `claude` that records what it was given. The units themselves are
checked as text: `systemd-analyze` is not on every runner.
"""

from __future__ import annotations

import asyncio
import configparser
import getpass
import json
import os
import re
import shlex
import socket
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from crewchat.kiosk import (
    CLAUDE_FLAGS,
    KIOSK_RESEARCH_SOCKET,
    KIOSK_RUN,
    KIOSK_SOCKET,
    KIOSK_USER,
    MAX_OUTPUT_BYTES,
    ClaudeCliRunner,
    ResearchFrame,
    RunnerError,
    parse_reply,
)

ROOT = Path(__file__).resolve().parents[3]
PACKAGING = ROOT / "packaging"
WRAPPER = PACKAGING / "crewkiosk-run"

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
exit "${FAKE_EXIT:-0}"
"""


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string((PACKAGING / name).read_text())
    return parser


Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def serve(tmp_path: Path, handler: Handler, scenario: Callable[[str], Awaitable[Any]]) -> Any:
    """Run `scenario(socket_path)` against a local socket served by `handler`."""
    path = str(tmp_path / "kiosk.sock")

    async def main() -> Any:
        server = await asyncio.start_unix_server(handler, path=path)
        async with server:
            return await scenario(path)

    return asyncio.run(main())


def reply_with(reply: bytes, seen: list[bytes]) -> Handler:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.read())  # to EOF: the client half-closed after the prompt
        writer.write(reply)
        await writer.drain()
        writer.close()

    return handler


# -- the client ------------------------------------------------------------------------------
def test_runner_has_no_tools(tmp_path: Path) -> None:
    """The CLI runner: the daemon sends only the prompt, on the kiosk socket; it never runs a
    command or changes user. The wrapper runs `claude -p --restricted --strict-mcp-config
    --tools ""` with the MCP exclusions, and nothing else."""
    seen: list[bytes] = []
    answer = serve(
        tmp_path,
        reply_with(b"ok\n  We build a chat daemon.\n", seen),
        lambda path: ClaudeCliRunner(socket=path).answer("PROMPT-TEXT"),
    )
    assert answer == "We build a chat daemon."
    assert seen == [b"PROMPT-TEXT"]  # the prompt, then EOF; no argument, no command line
    assert KIOSK_SOCKET == "/run/crewkiosk/kiosk.sock" and KIOSK_USER == "crewkiosk"
    assert ClaudeCliRunner().socket == KIOSK_SOCKET

    # The wrapper's one CLI invocation is exactly those flags (redirections aside).
    calls = [
        [word for word in shlex.split(line) if not word.startswith(("<", ">", "|"))]
        for line in WRAPPER.read_text().splitlines()
        if line.startswith('"$CLAUDE"') or line.startswith("exec ")
    ]
    assert [call[: 1 + len(CLAUDE_FLAGS)] for call in calls] == [["$CLAUDE", *CLAUDE_FLAGS]]
    assert CLAUDE_FLAGS == (
        "-p",
        "--restricted",
        "--strict-mcp-config",
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
        "--settings",
        '{"switchModelsOnFlag":false}',
    )
    assert re.search(r"^KIOSK_USER=crewkiosk$", WRAPPER.read_text(), re.M)
    assert re.search(r"^CREDENTIAL=/etc/crewkiosk/model.credential$", WRAPPER.read_text(), re.M)


def test_research_frame_on_the_wire(tmp_path: Path) -> None:
    """#142: with a frame, the header line then the prompt, on the research socket only;
    without one, today's bytes on today's socket. A frame the runner would refuse is never
    sent."""
    seen: list[bytes] = []
    frame = ResearchFrame("dev", "p1", "a" * 32)
    nowhere = str(tmp_path / "nowhere.sock")
    answer = serve(
        tmp_path,
        reply_with(b"ok\nresearched\n", seen),
        lambda path: ClaudeCliRunner(socket=nowhere, research_socket=path).answer(
            "PROMPT", frame=frame
        ),
    )
    assert answer == "researched"
    assert seen == [b"crewkiosk/1 server=dev post=p1 answer=" + b"a" * 32 + b"\nPROMPT"]
    (tmp_path / "kiosk.sock").unlink()
    serve(
        tmp_path,
        reply_with(b"ok\nplain\n", seen),
        lambda path: ClaudeCliRunner(socket=path, research_socket=nowhere).answer("PROMPT"),
    )
    assert seen[1] == b"PROMPT"
    assert ClaudeCliRunner().research_socket == KIOSK_RESEARCH_SOCKET
    assert KIOSK_RESEARCH_SOCKET == "/run/crewkiosk/research.sock"
    with pytest.raises(RunnerError, match="no research frame"):
        asyncio.run(
            ClaudeCliRunner(research_socket=nowhere).answer(
                "P", frame=ResearchFrame("dev", "p\nx", "a" * 32)
            )
        )


def test_runner_failure_is_an_error(tmp_path: Path) -> None:
    for reply, match in (
        (b"error 3\n", "exited 3"),
        (b"", "gave no answer"),  # the wrapper refused before running the CLI
        (b"garbage\n", "gave no answer"),
    ):
        with pytest.raises(RunnerError, match=match):
            serve(tmp_path, reply_with(reply, []), lambda p: ClaudeCliRunner(socket=p).answer("x"))
        (tmp_path / "kiosk.sock").unlink(missing_ok=True)


def test_runner_unavailable_and_timeout(tmp_path: Path, run: Any) -> None:
    """No socket, or a refusing one, is `unavailable`; a slow answer times out. Neither waits
    past `timeout_s`, and the kiosk logs it and posts nothing."""
    with pytest.raises(RunnerError, match="unavailable at"):
        run(ClaudeCliRunner(socket=str(tmp_path / "missing.sock")).answer("x"))

    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        await asyncio.sleep(1)  # past the client's timeout
        writer.close()  # the server's wait_closed() waits for every connection

    with pytest.raises(RunnerError, match="timed out"):
        serve(tmp_path, stall, lambda p: ClaudeCliRunner(socket=p, timeout_s=0.2).answer("x"))


def test_parse_reply_caps_the_answer() -> None:
    assert parse_reply(b"ok\nline one\nline two\n") == "line one\nline two"
    assert len(parse_reply(b"ok\n" + b"a" * (MAX_OUTPUT_BYTES + 100))) == MAX_OUTPUT_BYTES


# -- the units (D-107) -----------------------------------------------------------------------
def test_units_run_the_kiosk_as_crewkiosk() -> None:
    sock = unit("crewkiosk.socket")["Socket"]
    assert sock["ListenStream"] == KIOSK_SOCKET
    assert (sock["SocketMode"], sock["SocketGroup"], sock["Accept"]) == ("0660", "crewchat", "yes")
    svc = unit("crewkiosk@.service")["Service"]
    assert svc["User"] == KIOSK_USER and svc["ExecStart"] == KIOSK_RUN
    assert (svc["StandardInput"], svc["StandardOutput"]) == ("socket", "socket")
    assert svc["NoNewPrivileges"] == "yes" and svc["ProtectSystem"] == "strict"
    hidden = {p.lstrip("-") for p in svc["InaccessiblePaths"].split()}
    # The pool manifests name the repos the operator excluded.
    state = {"/var/lib/crewchat", "/etc/crewchat", "/etc/crewbroker", "/var/lib/crewbroker"}
    state |= {"/var/lib/crewchat-pool", "/var/lib/crewcorpus"}  # and the corpus (D-111)
    assert state <= hidden
    # The daemon keeps NoNewPrivileges, and no sudo rule ships (D-107).
    assert unit("crewchat.service")["Service"]["NoNewPrivileges"] == "yes"
    assert not (PACKAGING / "crewkiosk.sudoers").exists()


# -- the wrapper -----------------------------------------------------------------------------
def sandboxed_wrapper(tmp_path: Path, user: str) -> tuple[Path, Path, Path]:
    """The wrapper with its fixed paths pointed into `tmp_path`: (script, record dir, key).
    Its `MODEL_CONF` is `tmp_path / "model.conf"`, absent until a test writes it, so the
    host's own /etc/crewkiosk/model.conf is never read."""
    record = tmp_path / "record"
    record.mkdir()
    claude = tmp_path / "claude"
    claude.write_text(FAKE_CLAUDE.replace("$RECORD_DIR_PLACEHOLDER", str(record)))
    claude.chmod(0o755)
    key = tmp_path / "model.credential"
    key.write_text("sk-kiosk-test-key\n")
    key.chmod(0o600)
    text = WRAPPER.read_text()
    for name, value in (
        ("KIOSK_USER", user),
        ("CREDENTIAL", str(key)),
        ("MODEL_CONF", str(tmp_path / "model.conf")),
        ("CLAUDE", str(claude)),
    ):
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
    assert proc.stdout == b"ok\nthe answer\n"  # framed: a socket carries no exit status
    assert parse_reply(proc.stdout) == "the answer"
    argv = (record / "argv").read_text().split("\n")[:-1]
    assert tuple(argv) == (*CLAUDE_FLAGS, "--model", "claude-haiku-5-5")
    assert (record / "argc").read_text().strip() == "11"
    assert (record / "stdin").read_bytes() == b"THE-PROMPT"
    # An empty HOME and an empty working directory, both fresh and gone afterwards.
    assert (record / "home").read_text() == "" and (record / "work").read_text() == ""
    home = Path((record / "homedir").read_text().strip())
    work = Path((record / "pwd").read_text().strip())
    assert home != Path.home() and work != tmp_path and home.parent == work.parent
    assert not home.exists() and not work.exists()
    # Nothing inherited: an inherited ANTHROPIC_BASE_URL is dropped, and no model.conf sets one.
    env_out = recorded_env(record)
    assert "LEAKED-CHAT-TOKEN" not in json.dumps(env_out)
    assert "ANTHROPIC_BASE_URL" not in env_out
    assert env_out["ANTHROPIC_API_KEY"] == "sk-kiosk-test-key"
    assert env_out["PATH"] == "/usr/local/bin:/usr/bin:/bin"


MODEL_VARS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
)
BASH_OWN = {"PWD", "SHLVL", "_", "OLDPWD"}


def recorded_env(record: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in (record / "env").read_text().splitlines() if "=" in line
    )


def run_wrapper(script: Path, **env: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(script)],
        input=b"x",
        capture_output=True,
        env={"PATH": os.environ["PATH"], **env},
        timeout=30,
    )


def test_wrapper_pins_default_model(tmp_path: Path) -> None:
    """No model.conf: the pin is the full Haiku model name, on the command line and in all six
    variables. A variable's value is sent as a model ID, never resolved again as an alias."""
    script, record, _key = sandboxed_wrapper(tmp_path, getpass.getuser())
    proc = run_wrapper(script, ANTHROPIC_MODEL="opus", ANTHROPIC_SMALL_FAST_MODEL="opus")
    assert proc.returncode == 0, proc.stderr
    argv = (record / "argv").read_text().split("\n")[:-1]
    assert argv[-2:] == ["--model", "claude-haiku-5-5"]
    env_out = recorded_env(record)
    pinned = dict.fromkeys(MODEL_VARS, "claude-haiku-5-5")
    assert {name: env_out.get(name) for name in MODEL_VARS} == pinned
    assert "ANTHROPIC_BASE_URL" not in env_out


def test_wrapper_refuses_bare_alias_without_gateway(tmp_path: Path) -> None:
    """With no base_url, a Claude Code alias would reach the API as a model ID: refused. Behind
    a gateway the same word is the gateway's own name for a model, and passes."""
    script, record, _key = sandboxed_wrapper(tmp_path, getpass.getuser())
    for alias in ("haiku", "sonnet", "opus", "fable", "opusplan", "default", "best", "Haiku"):
        (tmp_path / "model.conf").write_text(f"model={alias}\n")
        proc = run_wrapper(script)
        assert proc.returncode == 66 and b"full model name" in proc.stderr, alias
        assert proc.stdout == b"" and not (record / "argv").exists()
    for conf in ("model=claude-sonnet-5-5\n", "base_url=https://llm.example.lan\nmodel=haiku\n"):
        (tmp_path / "model.conf").write_text(conf)
        proc = run_wrapper(script)
        assert proc.returncode == 0, (conf, proc.stderr)


def test_wrapper_uses_gateway_conf(tmp_path: Path) -> None:
    script, record, _key = sandboxed_wrapper(tmp_path, getpass.getuser())
    (tmp_path / "model.conf").write_text(
        "# the model gateway\n\nbase_url=https://llm.example.lan\nmodel=haiku-latest\n"
    )
    proc = run_wrapper(script)
    assert proc.returncode == 0, proc.stderr
    argv = (record / "argv").read_text().split("\n")[:-1]
    assert tuple(argv) == (*CLAUDE_FLAGS, "--model", "haiku-latest")
    env_out = recorded_env(record)
    assert env_out["ANTHROPIC_BASE_URL"] == "https://llm.example.lan"
    assert {name: env_out[name] for name in MODEL_VARS} == dict.fromkeys(MODEL_VARS, "haiku-latest")


def test_wrapper_refuses_unknown_conf_key(tmp_path: Path) -> None:
    script, record, _key = sandboxed_wrapper(tmp_path, getpass.getuser())
    for conf, line in (
        ("api_key=sk-sneaky\n", 1),
        ("model=claude-haiku-5-5\nPATH=/tmp\n", 2),
        ("base_url = https://a.lan\n", 1),
        ("# a comment\n\nsk-ant-pasted-by-mistake\n", 3),  # no `=`: the whole line is the key
    ):
        (tmp_path / "model.conf").write_text(conf)
        proc = run_wrapper(script)
        assert proc.returncode == 66 and b"unknown key" in proc.stderr, conf
        assert f"line {line}:".encode() in proc.stderr, conf
        # Never the line's text: it may be a key pasted into the wrong file.
        for secret in (b"sneaky", b"pasted-by-mistake", b"PATH", b"a.lan"):
            assert secret not in proc.stderr, conf
        assert proc.stdout == b"" and not (record / "argv").exists()


def test_wrapper_refuses_bad_base_url(tmp_path: Path) -> None:
    script, record, _key = sandboxed_wrapper(tmp_path, getpass.getuser())
    for conf in (
        "base_url=http://llm.example.lan\n",
        "base_url=https://llm.example.lan /v1\n",
        "base_url=\n",
        "model=haiku latest\n",
        "model=$(id)\n",
    ):
        (tmp_path / "model.conf").write_text(conf)
        proc = run_wrapper(script)
        assert proc.returncode == 66, conf
        assert proc.stdout == b"" and not (record / "argv").exists()


def test_wrapper_refuses_unreadable_conf(tmp_path: Path) -> None:
    """A model.conf that exists but can't be read never falls back to the default endpoint."""
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-0000 file")
    script, record, _key = sandboxed_wrapper(tmp_path, getpass.getuser())
    conf = tmp_path / "model.conf"
    conf.write_text("base_url=https://llm.example.lan\n")
    conf.chmod(0)
    proc = run_wrapper(script)
    assert proc.returncode == 66 and b"cannot read" in proc.stderr
    assert not (record / "argv").exists()


def test_wrapper_accepts_prefixed_credential(tmp_path: Path) -> None:
    script, record, key = sandboxed_wrapper(tmp_path, getpass.getuser())
    for line in ("ANTHROPIC_API_KEY=k\n", "k\n", "ANTHROPIC_API_KEY=k"):
        key.write_text(line)
        proc = run_wrapper(script)
        assert proc.returncode == 0, proc.stderr
        assert recorded_env(record)["ANTHROPIC_API_KEY"] == "k", line


def test_wrapper_refuses_other_assignment(tmp_path: Path) -> None:
    script, record, key = sandboxed_wrapper(tmp_path, getpass.getuser())
    for line in (
        "FOO=k\n",
        "ANTHROPIC_API_KEY=a=b\n",
        "export ANTHROPIC_API_KEY=k\n",
        "k\r\n",  # a CRLF line ending: the \r would be sent as part of the key
        " k\n",
        "k \n",
        "ANTHROPIC_API_KEY=k\r\n",
    ):
        key.write_text(line)
        proc = run_wrapper(script)
        assert proc.returncode == 65 and str(key).encode() in proc.stderr, repr(line)
        assert proc.stdout == b"" and not (record / "argv").exists()
    key.write_text("ANTHROPIC_API_KEY=\n")
    proc = run_wrapper(script)
    assert proc.returncode == 65 and b"is empty" in proc.stderr


def test_wrapper_env_is_exact(tmp_path: Path) -> None:
    """The CLI's environment is the allowlist: the key, the gateway when set, the six model
    variables, HOME and PATH, and what bash adds."""
    allowed = {"ANTHROPIC_API_KEY", *MODEL_VARS, "HOME", "PATH"}
    script, record, _key = sandboxed_wrapper(tmp_path, getpass.getuser())
    leaks = {
        "CREWCHAT_TOKEN": "LEAKED",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_AUTH_TOKEN": "x",
    }
    assert run_wrapper(script, **leaks).returncode == 0
    assert allowed <= set(recorded_env(record)) <= allowed | BASH_OWN
    (tmp_path / "model.conf").write_text("base_url=https://llm.example.lan\n")
    assert run_wrapper(script, **leaks).returncode == 0
    with_url = allowed | {"ANTHROPIC_BASE_URL"}
    assert with_url <= set(recorded_env(record)) <= with_url | BASH_OWN


def test_wrapper_frames_a_failing_cli(tmp_path: Path) -> None:
    script, _record, _key = sandboxed_wrapper(tmp_path, getpass.getuser())
    claude = tmp_path / "claude"
    claude.write_text(claude.read_text().replace('"${FAKE_EXIT:-0}"', "7"))
    proc = subprocess.run([str(script)], input=b"x", capture_output=True, timeout=30)
    assert proc.stdout == b"error 7\n" and b"claude exited 7" in proc.stderr
    with pytest.raises(RunnerError, match="exited 7"):
        parse_reply(proc.stdout)


def test_wrapper_refuses_another_user(tmp_path: Path) -> None:
    script, record, _ = sandboxed_wrapper(tmp_path, "no-such-kiosk-user")
    proc = subprocess.run([str(script)], input=b"x", capture_output=True, timeout=30)
    assert proc.returncode == 64 and b"must run as no-such-kiosk-user" in proc.stderr
    assert proc.stdout == b"" and not (record / "argv").exists()


def test_wrapper_needs_its_credential(tmp_path: Path) -> None:
    """An empty, missing or unreadable credential is exit 65, the credential's own code."""
    script, record, key = sandboxed_wrapper(tmp_path, getpass.getuser())
    key.write_text("")
    proc = subprocess.run([str(script)], input=b"x", capture_output=True, timeout=30)
    assert proc.returncode == 65 and not (record / "argv").exists()
    if os.geteuid() != 0:  # root reads a mode-0000 file
        key.write_text("k\n")
        key.chmod(0)
        proc = subprocess.run([str(script)], input=b"x", capture_output=True, timeout=30)
        assert proc.returncode == 65 and b"cannot read" in proc.stderr
        assert not (record / "argv").exists()
    key.unlink()
    proc = subprocess.run([str(script)], input=b"x", capture_output=True, timeout=30)
    assert proc.returncode == 65 and b"cannot read" in proc.stderr
    assert not (record / "argv").exists()


# -- the research runner (#142) --------------------------------------------------------------
RESEARCH = PACKAGING / "crewkiosk-research-run"
RESEARCH_TOOLS = "mcp__kiosk__corpus_search,mcp__kiosk__corpus_read,mcp__kiosk__corpus_list"
ANSWER = "0123456789abcdef0123456789abcdef"
# Each step the runner takes appends its name to the log: the order is what's tested.
FAKE_RELAY = """#!/bin/bash
dir="$RECORD_DIR_PLACEHOLDER"
echo "$1" >>"$dir/log"
printf '%s\\n' "$@" >"$dir/relay-$1"
case "$1" in
  hello) [[ -e "$dir/refuse" ]] && exit 1; exit 0 ;;
  http) exec python3 -c 'import socket,sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(4)
while True: s.accept()[0].close()' "$3" ;;
esac
"""
FAKE_RESEARCH_CLAUDE = """#!/bin/bash
dir="$RECORD_DIR_PLACEHOLDER"
echo claude >>"$dir/log"
printf '%s\\n' "$@" >"$dir/argv"
prev=""
for arg in "$@"; do
  [[ "$prev" == "--mcp-config" ]] && cp "$arg" "$dir/mcp.json"
  prev="$arg"
done
ls -A "$HOME" >"$dir/home"
env >"$dir/env"
cat >"$dir/stdin"
echo "the researched answer"
"""


def sandboxed_research(tmp_path: Path, user: str) -> tuple[Path, Path, Path, int]:
    """The research runner with its fixed paths in `tmp_path`: (script, record dir, broker
    dir, relay port). The broker dir holds server `dev`'s two sockets."""
    record = tmp_path / "record"
    record.mkdir()
    fakes = {}
    for name, body in (("claude", FAKE_RESEARCH_CLAUDE), ("relay", FAKE_RELAY)):
        fakes[name] = tmp_path / name
        fakes[name].write_text(body.replace("$RECORD_DIR_PLACEHOLDER", str(record)))
        fakes[name].chmod(0o755)
    broker_dir = tmp_path / "broker"
    broker_dir.mkdir()
    for sock in ("dev.sock", "dev.model.sock"):
        bound = socket.socket(socket.AF_UNIX)
        bound.bind(str(broker_dir / sock))
        bound.close()  # the socket file stays: all the runner checks is that it is one
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    text = RESEARCH.read_text()
    for name, value in (
        ("KIOSK_USER", user),
        ("CLAUDE", str(fakes["claude"])),
        ("RELAY", str(fakes["relay"])),
        ("BROKER_DIR", str(broker_dir)),
        ("RELAY_PORT", str(port)),
    ):
        text, n = re.subn(rf"^{name}=.*$", f"{name}={value}", text, flags=re.M)
        assert n == 1, name
    script = tmp_path / "crewkiosk-research-run"
    script.write_text(text)
    script.chmod(0o755)
    return script, record, broker_dir, port


def run_research(script: Path, stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    env = {"PATH": os.environ["PATH"], "ANTHROPIC_API_KEY": "LEAKED-KEY", "CREWCHAT_TOKEN": "x"}
    return subprocess.run([str(script)], input=stdin, capture_output=True, env=env, timeout=60)


def header(server: str = "dev", post: str = "p1", answer: str = ANSWER) -> bytes:
    return f"crewkiosk/1 server={server} post={post} answer={answer}\n".encode()


def test_research_runner_argv(tmp_path: Path) -> None:
    """Research mode: hello first, then the model relay, then claude with no native tools, a
    strict MCP config naming only the relay, and no model key. Today's crewkiosk-run argv is
    unchanged (test_runner_has_no_tools)."""
    script, record, broker_dir, port = sandboxed_research(tmp_path, getpass.getuser())
    proc = run_research(script, header() + b"THE-PROMPT")
    assert proc.returncode == 0, proc.stderr
    assert parse_reply(proc.stdout) == "the researched answer"
    # The hello is answered before the relay to the model starts, and both before claude.
    assert (record / "log").read_text().split() == ["hello", "http", "claude"]
    relay = str(tmp_path / "relay")
    assert (record / "relay-hello").read_text().split() == [
        "hello",
        str(broker_dir / "dev.sock"),
        ANSWER,
        "p1",
    ]
    assert (record / "relay-http").read_text().split() == [
        "http",
        str(broker_dir / "dev.model.sock"),
        str(port),
    ]
    argv = (record / "argv").read_text().split("\n")[:-1]
    mcp_file = argv[argv.index("--mcp-config") + 1]
    assert argv == [
        "-p",
        "--restricted",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_file,
        "--tools",
        "",
        "--allowedTools",
        RESEARCH_TOOLS,
        "--settings",
        '{"switchModelsOnFlag":false}',
        "--model",
        "claude-haiku-5-5",
    ]
    assert json.loads((record / "mcp.json").read_text()) == {
        "mcpServers": {
            "kiosk": {
                "type": "stdio",
                "command": relay,
                "args": ["mcp", str(broker_dir / "dev.sock"), ANSWER, "p1"],
            }
        }
    }
    assert (record / "stdin").read_bytes() == b"THE-PROMPT"  # the header is the runner's
    assert (record / "home").read_text() == ""
    env = recorded_env(record)
    assert env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{port}/a/{ANSWER}"
    assert env["ANTHROPIC_API_KEY"] == "broker"  # a placeholder: no model key in the kiosk
    assert "CREWCHAT_TOKEN" not in env and "LEAKED-KEY" not in json.dumps(env)
    assert all(env[v] == "claude-haiku-5-5" for v in MODEL_VARS)
    assert not Path(mcp_file).exists()  # the scratch is gone afterwards


@pytest.mark.parametrize(
    "first",
    [
        b"",
        b"\n",
        b"crewkiosk/2 server=dev post=p1 answer=" + ANSWER.encode() + b"\n",
        header(server="Dev"),
        header(server="dev/../x"),
        header(post="p-1"),
        header(answer=ANSWER[:-1]),
        header(answer=ANSWER.upper()),
        header()[:-1] + b" extra=1\n",
        b"THE-PROMPT with no header\n",
    ],
)
def test_research_runner_bad_header(tmp_path: Path, first: bytes) -> None:
    """A malformed header exits 67, before any hello, relay or claude."""
    script, record, _dir, _port = sandboxed_research(tmp_path, getpass.getuser())
    proc = run_research(script, first + b"rest")
    assert proc.returncode == 67 and proc.stdout == b"", proc.stderr
    assert not (record / "log").exists()


def test_research_runner_needs_the_brokers_sockets(tmp_path: Path) -> None:
    """A server whose broker sockets aren't there exits 68; a hello the broker refuses, 69.
    Neither starts the relay to the model or claude."""
    script, record, broker_dir, _port = sandboxed_research(tmp_path, getpass.getuser())
    proc = run_research(script, header(server="other") + b"x")
    assert proc.returncode == 68 and not (record / "log").exists()
    (broker_dir / "dev.model.sock").unlink()
    (broker_dir / "dev.model.sock").write_text("")  # a file, not a socket
    proc = run_research(script, header() + b"x")
    assert proc.returncode == 68 and not (record / "log").exists()

    (tmp_path / "refused").mkdir()
    script, record, _dir, _port = sandboxed_research(tmp_path / "refused", getpass.getuser())
    (record / "refuse").touch()
    proc = run_research(script, header() + b"x")
    assert proc.returncode == 69 and proc.stdout == b"", proc.stderr
    assert (record / "log").read_text().split() == ["hello"]  # no model relay, no claude


def test_research_runner_refuses_another_user(tmp_path: Path) -> None:
    script, record, _dir, _port = sandboxed_research(tmp_path, "no-such-kiosk-user")
    proc = run_research(script, header() + b"x")
    assert proc.returncode == 64 and not (record / "log").exists()
