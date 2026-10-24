"""Story 1.4: the daemon socket, its dispatch, and what it never returns (§5.3, §5.4; R-08)."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

from crewchat.server import DaemonServer, build_handlers


async def exchange(path: Path, frames: list[dict[str, Any] | bytes]) -> list[dict[str, Any]]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    replies: list[dict[str, Any]] = []
    try:
        for f in frames:
            line = f if isinstance(f, bytes) else json.dumps(f).encode() + b"\n"
            writer.write(line)
            await writer.drain()
            replies.append(json.loads(await reader.readline()))
    finally:
        writer.close()
    return replies


def run_server(daemon: Any, path: Path, frames: list[dict[str, Any] | bytes]) -> Any:
    async def main() -> list[dict[str, Any]]:
        server = DaemonServer(build_handlers(daemon.config, daemon.registry))
        listener = await server.start(path, group=None)
        try:
            assert stat.S_IMODE(path.stat().st_mode) == 0o660
            return await exchange(path, frames)
        finally:
            listener.close()
            await listener.wait_closed()

    return asyncio.run(main())


def test_unknown_op_unsupported(daemon: Any, short_dir: Path) -> None:
    replies = run_server(
        daemon,
        short_dir / "crewchat.sock",
        [
            {"proto": 1, "id": "a", "op": "no_such_op", "args": {}},
            # A known op still answers on the same connection afterwards.
            {"proto": 1, "id": "b", "op": "health"},
        ],
    )
    assert replies[0] == {"proto": 1, "id": "a", "ok": False, "error": "unsupported"}
    assert replies[1]["ok"] is True and replies[1]["id"] == "b"


def test_proto_2_unsupported_and_proto_0_handled(daemon: Any, short_dir: Path) -> None:
    replies = run_server(
        daemon,
        short_dir / "crewchat.sock",
        [
            {"proto": 2, "id": "new", "op": "health"},
            {"proto": 0, "id": "old", "op": "health"},
            b"garbage\n",
        ],
    )
    assert replies[0] == {"proto": 1, "id": "new", "ok": False, "error": "unsupported"}
    assert replies[1]["ok"] is True
    assert set(replies[1]["result"]["backends"]) == {"dev", "discord"}
    assert replies[2]["ok"] is False and replies[2]["error"].startswith("bad_request")


def test_register_over_the_socket_returns_no_token(daemon: Any, short_dir: Path) -> None:
    """R-08: register answers the account, backends and needs_list, and never a token."""
    replies = run_server(
        daemon,
        short_dir / "crewchat.sock",
        [
            {
                "proto": 1,
                "id": "r",
                "op": "register",
                "args": {"dev_name": "babel", "crew": "oaw", "session_id": "s1"},
            },
            {"proto": 1, "id": "h", "op": "health"},
            {"proto": 1, "id": "bad", "op": "register", "args": {"dev_name": "babel"}},
        ],
    )
    assert replies[0]["result"] == {
        "account": "babel.oaw",
        "backends": ["dev", "discord"],
        "needs_list": "bj:needs",
    }
    token = daemon.store.token_for("babel", "dev")
    assert token
    for reply in replies:
        assert token not in json.dumps(reply)
        assert "token" not in json.dumps(reply)
    assert replies[2]["ok"] is False and replies[2]["error"].startswith("bad_request")


def test_handler_crash_is_internal(daemon: Any) -> None:
    async def boom(_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("secret detail")

    server = DaemonServer({"boom": boom})
    reply = asyncio.run(server.handle_line(b'{"proto":1,"id":"x","op":"boom"}'))
    assert reply == {"proto": 1, "id": "x", "ok": False, "error": "internal"}
