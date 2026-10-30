"""The script-side sender: `crewchat-send` (#146).

A script runs it to post one message as the checkout's agent: the kit's page scripts
(`notify-bj.sh`, `reseed-kick.sh`) and anything else in shell that must reach a person. It
sends one `send` (or, with `--dm`, `dm`) frame to the daemon's socket and prints the post's
ID.

- **Who.** The Dev-Name is read from `.claude/agent-identity.json` at the repo root, exactly as
  the doorbell reads it (the `git rev-parse --show-toplevel` of the working directory, or
  `--repo-root`). It **never registers**: the session's shim owns its registration, and a
  script must not make a shadow one. A daemon that doesn't know the agent answers
  `not_registered`, and that is exit 4.
- **What.** The text is read from STDIN only, never argv. A shell caller's message carries
  backticks and `$`, and STDIN keeps them out of every shell parse (the kit's `vox < file`
  rule). One trailing newline is stripped. An empty message, a message given in argv, or a
  terminal on STDIN is a usage error.
- **Where.** `<to>` is a channel ID or name, or a username with `--dm`. The backend is
  `--backend`; else, with `--thread`, the backend that thread is on (`thread_backend`, as the
  shim asks it); else the R-11 default (`home_backend`, then `~/.claude/chat-defaults.json`).
  It never asks: no default is exit 3.

Exit codes. A caller branches on non-zero to fall back:

| code | meaning |
|---|---|
| 0 | sent: the post's ID on stdout |
| 1 | the daemon refused it (its error on stderr), or it answered with no post ID |
| 2 | a usage error: no identity, text in argv, an empty message, a bad backend or thread |
| 3 | no default backend for this repo, and no `--backend` |
| 4 | not registered: no live session for this agent on this host |
| 5 | daemon unreachable: no socket, a refused or timed-out connect, or a bad reply |

Stderr carries one line per failure. It never carries the message text.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

from crewchat_clients.defaults import (
    DefaultsError,
    NeedsHuman,
    is_backend,
    resolve_default,
)
from crewchat_clients.doorbell import DoorbellExit, read_dev_name
from crewchat_clients.mcp_shim import (
    IDENTITY_FILE,
    MAX_FRAME_BYTES,
    PROTO,
    SOCKET_PATH,
    ShimError,
    discord_ref,
    find_repo_root,
    thread_id,
)

EXIT_SENT = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_DEFAULT = 3
EXIT_NOT_REGISTERED = 4
EXIT_UNREACHABLE = 5

CONNECT_TIMEOUT_S = 10.0
REPLY_TIMEOUT_S = 30.0  # a Discord post through scream-hole can take seconds
RECV_CHUNK = 65536

EXIT_CODES_HELP = """exit codes:
  0  sent: the post's ID on stdout
  1  the daemon refused it, or answered with no post ID
  2  usage: no identity, text in argv, an empty message, a bad backend or thread
  3  no default backend for this repo, and no --backend
  4  not registered: no live session for this agent on this host
  5  daemon unreachable"""


class SendExit(Exception):
    """A failure with its exit code. The message is the one stderr line."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


def request(sock_path: Path, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """One `op` frame and its `result`, or a `SendExit`."""
    req_id = str(uuid.uuid4())
    frame = {"proto": PROTO, "id": req_id, "op": op, "args": args}
    buf = b""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.settimeout(CONNECT_TIMEOUT_S)
            sock.connect(str(sock_path))
            sock.settimeout(REPLY_TIMEOUT_S)
            sock.sendall(json.dumps(frame).encode() + b"\n")
            while not buf.endswith(b"\n"):
                chunk = sock.recv(RECV_CHUNK)
                if not chunk:
                    raise SendExit(
                        EXIT_UNREACHABLE, f"crewchat daemon at {sock_path} closed without a reply"
                    )
                buf += chunk
                if len(buf) > MAX_FRAME_BYTES:
                    raise SendExit(
                        EXIT_UNREACHABLE, f"crewchat daemon at {sock_path} sent an oversized reply"
                    )
        except OSError as exc:  # absent, refused, no permission, or a timeout
            raise SendExit(EXIT_UNREACHABLE, f"crewchat daemon at {sock_path}: {exc!r}") from exc
    try:
        reply = json.loads(buf)
    except ValueError as exc:
        raise SendExit(EXIT_UNREACHABLE, f"crewchat daemon sent a malformed reply to {op}") from exc
    if not isinstance(reply, dict) or reply.get("id") != req_id:
        raise SendExit(EXIT_UNREACHABLE, f"crewchat daemon sent a mismatched reply to {op}")
    if not reply.get("ok"):
        error = str(reply.get("error", "error"))
        if error.split(":", 1)[0].strip() == "not_registered":
            raise SendExit(
                EXIT_NOT_REGISTERED,
                f"no registered session for {args.get('dev_name')} on this host",
            )
        raise SendExit(EXIT_ERROR, f"crewchat {op}: {error}")
    result = reply.get("result")
    return result if isinstance(result, dict) else {}


def read_identity(repo_root: Path) -> tuple[str, dict[str, Any]]:
    """The Dev-Name, checked as the doorbell checks it, and the whole identity object."""
    try:
        dev_name = read_dev_name(repo_root)
    except DoorbellExit as exc:
        raise SendExit(EXIT_USAGE, str(exc)) from exc
    identity = json.loads((repo_root / IDENTITY_FILE).read_text(encoding="utf-8"))
    return dev_name, identity


def resolve_backend(
    named: str | None,
    thread: str | None,
    identity: dict[str, Any],
    repo_root: Path,
    sock_path: Path,
    dev_name: str,
    defaults_file: Path | None,
) -> tuple[str, bool]:
    """The backend, and whether it is off the agent's default (the shim's `off_default`)."""
    try:
        default = resolve_default(identity, repo_root, path=defaults_file)
    except DefaultsError as exc:
        raise SendExit(EXIT_USAGE, f"default backend: {exc}") from exc
    if named is not None:
        if not is_backend(named):
            raise SendExit(
                EXIT_USAGE, f"backend {named!r} is not mattermost:<server-id> or discord"
            )
        return named, named != default
    if thread:
        pair = discord_ref(thread)
        lookup = pair[1] if pair else thread
        found = request(sock_path, "thread_backend", {"dev_name": dev_name, "post_id": lookup})
        backend = found.get("backend")
        if not is_backend(backend):
            raise SendExit(EXIT_ERROR, f"crewchat thread_backend answered {backend!r}")
        return str(backend), False
    if isinstance(default, NeedsHuman):
        raise SendExit(
            EXIT_NO_DEFAULT,
            f"no default backend for {default.key}: pass --backend, or record one "
            "(chat_set_default in a session)",
        )
    return default, False


def read_text(stdin: TextIO) -> str:
    if stdin.isatty():
        raise SendExit(EXIT_USAGE, "pipe the message on STDIN; a terminal is not read")
    text = stdin.read()
    if text.endswith("\n"):
        text = text[:-1]
    if not text.strip():
        raise SendExit(EXIT_USAGE, "the message on STDIN is empty")
    return text


def run(
    ns: argparse.Namespace,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    defaults_file: Path | None = None,
) -> int:
    try:
        if ns.text:
            raise SendExit(EXIT_USAGE, "the message is read from STDIN, never argv")
        if ns.dm and ns.thread:
            raise SendExit(EXIT_USAGE, "--thread is a channel reply; a --dm has none")
        try:
            tid = thread_id(ns.thread) if ns.thread else None
        except ShimError as exc:
            raise SendExit(EXIT_USAGE, str(exc)) from exc
        root = ns.repo_root or find_repo_root(Path.cwd())
        dev_name, identity = read_identity(root)
        text = read_text(stdin)
        backend, off = resolve_backend(
            ns.backend, ns.thread, identity, root, ns.socket, dev_name, defaults_file
        )
        args: dict[str, Any] = {"dev_name": dev_name, "backend": backend, "to": ns.to, "text": text}
        if tid:
            args["thread"] = tid
        if off:
            args["off_default"] = True
        result = request(ns.socket, "dm" if ns.dm else "send", args)
        post_id = result.get("post_id")
        if not isinstance(post_id, str) or not post_id:
            raise SendExit(EXIT_ERROR, "crewchat answered with no post ID")
    except SendExit as exc:
        print(f"crewchat-send: {exc}", file=stderr)
        return exc.code
    print(post_id, file=stdout)
    return EXIT_SENT


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crewchat-send",
        description="Post one message, read from STDIN, as this checkout's crewchat agent.",
        epilog=EXIT_CODES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("to", help="a channel ID or name, or a username with --dm")
    p.add_argument("text", nargs="*", help=argparse.SUPPRESS)  # refused: STDIN only
    p.add_argument("--backend", default=None, help="mattermost:<server-id> or discord")
    p.add_argument("--thread", default=None, help="reply on this thread (a post ID)")
    p.add_argument("--dm", action="store_true", help="<to> is a username: a direct message")
    p.add_argument("--socket", type=Path, default=SOCKET_PATH)
    p.add_argument("--repo-root", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    ns = parser().parse_args(argv)
    return run(ns, sys.stdin, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
