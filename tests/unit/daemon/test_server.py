"""Story 1.4: the daemon socket, its dispatch, and what it never returns (§5.3, §5.4; R-08)."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

from crewchat.adapters.base import InboundMessage
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.server import DaemonServer, build_handlers
from crewchat.threads import MyThreads


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
    assert set(replies[1]["result"]["backends"]) == {"mattermost:dev", "discord"}
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
        "backends": ["mattermost:dev", "discord"],
        "needs_list": "bj:needs",
    }
    token = daemon.store.token_for("babel", "dev")
    assert token
    for reply in replies:
        assert token not in json.dumps(reply)
        assert "token" not in json.dumps(reply)
    assert replies[2]["ok"] is False and replies[2]["error"].startswith("bad_request")


def call(server: DaemonServer, op: str, **args: Any) -> dict[str, Any]:
    frame = {"proto": 1, "id": "x", "op": op, "args": args}
    return asyncio.run(server.handle_line(json.dumps(frame).encode()))


def test_register_accepts_team_and_avatar(daemon: Any) -> None:
    """D-103: register stores dev_team and dev_avatar; non-strings are bad_request."""
    server = DaemonServer(build_handlers(daemon.config, daemon.registry))
    base = {"dev_name": "babel", "session_id": "s1"}
    got = call(server, "register", **base, dev_team="oaw", dev_avatar="📡")
    assert got["ok"] is True
    assert set(got["result"]) == {"account", "backends", "needs_list"}
    reg = daemon.registry.get("babel")
    assert (reg.dev_team, reg.dev_avatar) == ("oaw", "📡")

    # Absent: both "". A re-register overwrites.
    assert call(server, "register", **base)["ok"] is True
    reg = daemon.registry.get("babel")
    assert (reg.dev_team, reg.dev_avatar) == ("", "")

    for bad in ({"dev_team": 7}, {"dev_avatar": ["x"]}, {"dev_team": None}):
        got = call(server, "register", **base, **bad)
        assert got["ok"] is False and got["error"].startswith("bad_request"), bad


class _Backend:
    name = "mattermost:dev"

    def __init__(self) -> None:
        self.n = 0
        self.fail = False

    async def send(
        self, dev_name: str, channel: str, text: str, thread: str | None = None
    ) -> InboundMessage:
        if self.fail:
            raise RuntimeError("server said no")
        self.n += 1
        return InboundMessage(
            backend=self.name,
            channel=channel,
            post_id=f"p{self.n}",
            root_id=thread or "",
            author_id="me",
            author_name="babel",
            text=text,
        )

    async def dm(self, dev_name: str, to: str, text: str) -> InboundMessage:
        return await self.send(dev_name, "dm-chan", text)


def test_send_records_thread(daemon: Any, tmp_path: Path) -> None:
    """D-104: a successful send (new root, then reply) and dm record into the persisted
    MyThreads; a failed one records nothing."""
    asyncio.run(daemon.registry.register("babel", None, "s1"))
    threads = MyThreads.load(tmp_path / "state", 90)
    backend = _Backend()
    handlers = build_handlers(daemon.config, daemon.registry)
    handlers.update(
        build_chat_handlers(daemon.registry, {backend.name: backend}, threads, AgentContext())
    )
    server = DaemonServer(handlers)
    base = {"dev_name": "babel", "backend": backend.name}

    root = call(server, "send", **base, to="town", text="new thread")
    assert root["ok"] is True and root["result"]["thread"] == "p1"
    reply = call(server, "send", **base, to="town", text="re", thread="other-root")
    assert reply["ok"] is True and reply["result"]["thread"] == "other-root"
    assert threads.roots("babel") == {"p1", "other-root"}
    assert call(server, "dm", **base, to="kessel", text="psst")["ok"] is True
    assert threads.roots("babel") == {"p1", "other-root", "p3"}

    backend.fail = True
    assert call(server, "send", **base, to="town", text="x", thread="never")["ok"] is False
    assert call(server, "dm", **base, to="kessel", text="x")["ok"] is False
    assert threads.roots("babel") == {"p1", "other-root", "p3"}
    # What was recorded is on disk: a fresh load sees it.
    assert MyThreads.load(tmp_path / "state", 90).roots("babel") == {"p1", "other-root", "p3"}


def test_handler_crash_is_internal(daemon: Any) -> None:
    async def boom(_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("secret detail")

    server = DaemonServer({"boom": boom})
    reply = asyncio.run(server.handle_line(b'{"proto":1,"id":"x","op":"boom"}'))
    assert reply == {"proto": 1, "id": "x", "ok": False, "error": "internal"}


def test_backend_name_single_form(daemon: Any) -> None:
    """Story 2.4c (§5.4): one backend-name form on the socket. `register` and `health` answer
    `mattermost:<id>`; a bare server id on a chat frame is `bad_request`, never translated."""
    backend = _Backend()
    handlers = build_handlers(daemon.config, daemon.registry)
    handlers.update(
        build_chat_handlers(daemon.registry, {backend.name: backend}, MyThreads(), AgentContext())
    )
    server = DaemonServer(handlers)
    got = call(server, "register", dev_name="babel", session_id="s1")
    assert got["result"]["backends"] == ["mattermost:dev", "discord"]
    assert set(call(server, "health")["result"]["backends"]) == {"mattermost:dev", "discord"}

    base = {"dev_name": "babel", "to": "town", "text": "hi"}
    for bare in ("dev", "discord:900000000000000001", "Mattermost:dev"):
        got = call(server, "send", **base, backend=bare)
        assert got["ok"] is False and got["error"].startswith("bad_request"), bare
    assert backend.n == 0  # nothing reached the backend
    ok = call(server, "send", **base, backend="mattermost:dev")
    assert ok["ok"] is True and ok["result"]["backend"] == "mattermost:dev"


def test_health_reports_a_backend_that_answers_for_itself(daemon: Any) -> None:
    """D-106: Discord's entry comes from the adapter, `degraded` while scream-hole is down."""

    class Source:
        async def health(self) -> dict[str, Any]:
            return {"state": "degraded", "since": "t", "detail": "scream-hole unhealthy"}

    server = DaemonServer(build_handlers(daemon.config, daemon.registry, {"discord": Source()}))
    backends = call(server, "health")["result"]["backends"]
    assert backends["discord"]["state"] == "degraded"
    assert backends["mattermost:dev"]["state"] == "configured"
