"""The thin doorbell one-shot: `crewchat-doorbell` (Dev Spec §5.8; Story 3.4; R-26, CT-08).

An agent runs it as a background task. It sends one `subscribe{dev_name}` frame to the
daemon's socket, waits for the one event the daemon hands over, prints it as **one JSON line**
on stdout and exits 0. The agent wakes on that exit, reads through the MCP, and re-arms. There
is no Monitor primitive on Bedrock, so a task's exit is the only wake-up there is (CT-08).

- **Who.** The Dev-Name is read from `.claude/agent-identity.json` at the repo root (the
  `git rev-parse --show-toplevel` of the working directory, or `--repo-root`), the same file
  the MCP shim reads. The one-shot never registers: the shim does that, and a daemon that
  doesn't know the agent answers `not_registered`.
- **How long.** `CREWCHAT_DOORBELL_TIMEOUT` is the wait in seconds; the default `0` waits
  forever. On a timeout the one-shot hangs up, which cancels the daemon's `subscribe`.
- **Outsider events.** An event marked `"outsider": true` is printed with its text prefixed
  `[from outside the crew]` (`mcp_shim.render_event`): the `text` of the kiosk review (Story
  3.6), and `message.text` of an outsider message delivered under social (Story 3.3, #97).

Exit codes (§5.8). It **never** exits 0 without printing an event:

| code | meaning |
|---|---|
| 0 | an event, printed as one JSON line |
| 1 | a usage error: no identity, a bad timeout, or a daemon error other than the ones below |
| 2 | not registered |
| 3 | timeout |
| 4 | daemon unreachable: no socket, refused, or the reply was cut short or malformed |
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from crewchat_clients.mcp_shim import (
    IDENTITY_FILE,
    MAX_FRAME_BYTES,
    PROTO,
    SOCKET_PATH,
    find_repo_root,
    render_event,
)

EXIT_EVENT = 0
EXIT_ERROR = 1
EXIT_NOT_REGISTERED = 2
EXIT_TIMEOUT = 3
EXIT_UNREACHABLE = 4

TIMEOUT_ENV = "CREWCHAT_DOORBELL_TIMEOUT"
CONNECT_TIMEOUT_S = 10.0
RECV_CHUNK = 65536


class DoorbellExit(Exception):
    """Ends the one-shot with `code`; the message goes to stderr."""

    code = EXIT_ERROR


class NotRegistered(DoorbellExit):
    code = EXIT_NOT_REGISTERED


class TimedOut(DoorbellExit):
    code = EXIT_TIMEOUT


class Unreachable(DoorbellExit):
    code = EXIT_UNREACHABLE


def read_dev_name(repo_root: Path) -> str:
    """The `dev_name` in the repo's identity file."""
    path = repo_root / IDENTITY_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DoorbellExit(f"cannot read {path}: {exc}") from exc
    dev_name = raw.get("dev_name") if isinstance(raw, dict) else None
    if not isinstance(dev_name, str) or not dev_name:
        raise DoorbellExit(f"{path} names no dev_name; run /name first")
    return dev_name


def parse_timeout(environ: Mapping[str, str]) -> float | None:
    """`CREWCHAT_DOORBELL_TIMEOUT` in seconds; `None` (forever) when unset, empty or 0."""
    raw = environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise DoorbellExit(f"{TIMEOUT_ENV}={raw!r} is not a number of seconds") from exc
    if value != value or value < 0 or value == float("inf"):
        raise DoorbellExit(f"{TIMEOUT_ENV}={raw!r} is not a number of seconds >= 0")
    return value or None


def subscribe(sock_path: Path, dev_name: str, timeout_s: float | None) -> dict[str, Any]:
    """One `subscribe{dev_name}` frame and its reply: the event, or a `DoorbellExit`."""
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    req_id = str(uuid.uuid4())
    frame = {"proto": PROTO, "id": req_id, "op": "subscribe", "args": {"dev_name": dev_name}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.settimeout(CONNECT_TIMEOUT_S)
            sock.connect(str(sock_path))
            sock.sendall(json.dumps(frame).encode() + b"\n")
        except OSError as exc:  # absent, refused, no permission, or a connect timeout
            raise Unreachable(f"crewchat daemon at {sock_path}: {exc!r}") from exc
        buf = b""
        while not buf.endswith(b"\n"):
            if deadline is None:
                sock.settimeout(None)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimedOut(f"no event within {timeout_s:g} s")
                sock.settimeout(remaining)
            try:
                chunk = sock.recv(RECV_CHUNK)
            except TimeoutError as exc:
                raise TimedOut(f"no event within {timeout_s:g} s") from exc
            except OSError as exc:
                raise Unreachable(f"crewchat daemon at {sock_path}: {exc!r}") from exc
            if not chunk:
                raise Unreachable(f"crewchat daemon at {sock_path} closed without a reply")
            buf += chunk
            if len(buf) > MAX_FRAME_BYTES:
                raise Unreachable(f"crewchat daemon at {sock_path} sent an oversized reply")
    try:
        reply = json.loads(buf)
    except ValueError as exc:
        raise Unreachable("crewchat daemon sent a malformed reply to subscribe") from exc
    if not isinstance(reply, dict) or reply.get("id") != req_id:
        raise Unreachable("crewchat daemon sent a mismatched reply to subscribe")
    if not reply.get("ok"):
        error = str(reply.get("error", "error"))
        if error.split(":", 1)[0].strip() == "not_registered":
            raise NotRegistered(f"{dev_name} is not registered with the crewchat daemon")
        raise DoorbellExit(f"crewchat subscribe: {error}")
    event = reply.get("result")
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise DoorbellExit("crewchat daemon answered subscribe without an event")
    return event


def run(
    repo_root: Path,
    sock_path: Path,
    environ: Mapping[str, str],
    out: TextIO,
    err: TextIO,
) -> int:
    """The one-shot, apart from argv: its exit code. Only an event's path returns 0."""
    try:
        timeout_s = parse_timeout(environ)
        dev_name = read_dev_name(repo_root)
        event = subscribe(sock_path, dev_name, timeout_s)
    except DoorbellExit as exc:
        print(f"crewchat-doorbell: {exc}", file=err)
        return exc.code
    # `json.dumps` without `indent` never emits a newline: the event is exactly one line.
    out.write(json.dumps(render_event(event)) + "\n")
    out.flush()
    return EXIT_EVENT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crewchat-doorbell", description="Wait for one event from the crewchat daemon."
    )
    parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
    parser.add_argument("--repo-root", type=Path, default=None)
    ns = parser.parse_args(argv)
    root = ns.repo_root or find_repo_root(Path.cwd())
    return run(root, ns.socket, os.environ, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
