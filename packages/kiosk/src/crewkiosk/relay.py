#!/usr/bin/python3 -I
"""`crewkiosk-relay`: the byte pump the research kiosk runs inside its namespace (#141; D-111).

Installed alone, as `/usr/local/libexec/crewkiosk-relay`: it imports nothing outside the
standard library and holds no policy. Every decision is the broker's; the relay only carries
bytes, verbatim, between the kiosk and a broker socket.

    crewkiosk-relay mcp <sock> <answer_id> <post_id>
        Connect to the broker's op socket, send the `crewkiosk/hello` notification, then pump
        stdin to the socket and the socket to stdout. Claude Code runs this as its one stdio
        MCP server. The hello is a notification, so no reply to it reaches stdout.

    crewkiosk-relay hello <sock> <answer_id> <post_id>
        Send the hello as a request and exit 0 only if the broker answers that it admitted
        the answer, else 1. The research runner runs this before it starts the kiosk, so no
        model call can come before the answer is admitted (#142). The verdict is the broker's;
        the relay only reports it.

    crewkiosk-relay http <sock> <port>
        Listen on 127.0.0.1:<port>; for each connection, connect to the broker's model socket
        and pump both ways. It exits when its parent does (PR_SET_PDEATHSIG).
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import sys
import threading

CHUNK = 65536
PR_SET_PDEATHSIG = 1
HELLO_TIMEOUT_S = 10


def pump(src: int | socket.socket, dst: int | socket.socket) -> None:
    """Copy until `src` ends, then end `dst`'s writing side. Never raises."""
    try:
        while True:
            data = src.recv(CHUNK) if isinstance(src, socket.socket) else os.read(src, CHUNK)
            if not data:
                break
            if isinstance(dst, socket.socket):
                dst.sendall(data)
            else:
                view = memoryview(data)
                while view:
                    view = view[os.write(dst, view) :]
    except OSError:
        pass
    finally:
        try:
            if isinstance(dst, socket.socket):
                dst.shutdown(socket.SHUT_WR)
            else:
                os.close(dst)
        except OSError:
            pass


def hello(answer_id: str, post_id: str, rid: int | None = None) -> bytes:
    msg: dict[str, object] = {
        "jsonrpc": "2.0",
        "method": "crewkiosk/hello",
        "params": {"answer_id": answer_id, "post_id": post_id},
    }
    if rid is not None:
        msg["id"] = rid
    return json.dumps(msg, separators=(",", ":")).encode() + b"\n"


def admit(path: str, answer_id: str, post_id: str) -> int:
    """The hello as a request: 0 once the broker answers that it admitted the answer, 1 for
    anything else (refused, closed, garbled or slow). The runner starts the kiosk on 0 only."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(HELLO_TIMEOUT_S)
            conn.connect(path)
            conn.sendall(hello(answer_id, post_id, 0))
            reply = b""
            while not reply.endswith(b"\n") and len(reply) < CHUNK:
                data = conn.recv(CHUNK)
                if not data:
                    break
                reply += data
        msg = json.loads(reply)
    except (OSError, ValueError):
        return 1
    ok = isinstance(msg, dict) and msg.get("id") == 0 and msg.get("result") == {"admitted": True}
    return 0 if ok else 1


def mcp(path: str, answer_id: str, post_id: str) -> int:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(path)
    conn.sendall(hello(answer_id, post_id))
    up = threading.Thread(target=pump, args=(sys.stdin.fileno(), conn), daemon=True)
    up.start()
    pump(conn, sys.stdout.fileno())  # the broker closing ends the session
    return 0


def bridge(client: socket.socket, path: str) -> None:
    try:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.connect(path)
    except OSError:
        client.close()
        return
    back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
    back.start()
    pump(client, upstream)
    back.join()
    client.close()
    upstream.close()


def die_with_parent() -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        pass
    if os.getppid() == 1:  # the parent is already gone
        sys.exit(0)


def http(path: str, port: int) -> int:
    die_with_parent()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(16)
    while True:
        client, _ = listener.accept()
        threading.Thread(target=bridge, args=(client, path), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 4 and args[0] == "mcp":
        return mcp(args[1], args[2], args[3])
    if len(args) == 4 and args[0] == "hello":
        return admit(args[1], args[2], args[3])
    if len(args) == 3 and args[0] == "http" and args[2].isdigit():
        return http(args[1], int(args[2]))
    print(
        "usage: crewkiosk-relay mcp <sock> <answer_id> <post_id>"
        " | hello <sock> <answer_id> <post_id> | http <sock> <port>",
        file=sys.stderr,
    )
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
