"""#107: the daemon shuts down while a client's `subscribe` waits, and a failed read on a
waiting op is a hangup."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest
from crewchat.__main__ import serve
from crewchat.server import DaemonServer

CONFIG = """
default_crew = "oaw"

[crews.oaw]
needs_list = "bj:needs"
"""


def frame(op: str, args: dict[str, Any], req_id: str = "1") -> bytes:
    return json.dumps({"proto": 1, "id": req_id, "op": op, "args": args}).encode() + b"\n"


async def wait_for_socket(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


def test_serve_stops_while_a_subscribe_waits(short_dir: Path, tmp_path: Path) -> None:
    """`serve` returns within 2 s of SIGTERM though a client's `subscribe` is still waiting
    on an empty queue: shutdown closes every connection before it waits for them (#107)."""
    config = tmp_path / "servers.toml"
    config.write_text(CONFIG)
    sock = short_dir / "crewchat.sock"

    async def scenario() -> tuple[int, bytes]:
        daemon = asyncio.create_task(
            serve(config, sock, tmp_path / "tokens", None, state_dir=tmp_path / "state")
        )
        await wait_for_socket(sock)
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame("register", {"dev_name": "babel", "session_id": "s-1"}, "r"))
        await writer.drain()
        registered = json.loads(await reader.readline())
        assert registered["ok"] is True, registered
        writer.write(frame("subscribe", {"dev_name": "babel"}, "s"))
        await writer.drain()
        await asyncio.sleep(0.2)  # the subscribe is waiting in the doorbell now
        assert not daemon.done()
        os.kill(os.getpid(), signal.SIGTERM)
        code = await asyncio.wait_for(daemon, 2)
        tail = await asyncio.wait_for(reader.read(), 2)  # the daemon closed the connection
        writer.close()
        return code, tail

    code, tail = asyncio.run(scenario())
    assert code == 0
    assert tail == b""  # the cancelled subscribe wrote nothing: its event would stay held
    assert not sock.exists()


class Waiting:
    """A handler that waits until it's cancelled, as `subscribe` does on an empty queue."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {}


def test_aclose_returns_while_ops_wait(short_dir: Path) -> None:
    """Two connections each with a waiting op, and one that has sent nothing: `aclose()`
    returns, both ops were cancelled, and nothing was written to any of them."""
    sock = short_dir / "s.sock"

    async def scenario() -> tuple[list[Waiting], list[bytes]]:
        waits = [Waiting(), Waiting()]
        server = DaemonServer(
            {"wait0": waits[0], "wait1": waits[1]},
            hangup_cancels=frozenset({"wait0", "wait1"}),
        )
        await server.start(sock, group=None)
        conns = []
        for i in range(3):
            reader, writer = await asyncio.open_unix_connection(str(sock))
            if i < 2:
                writer.write(frame(f"wait{i}", {}))
                await writer.drain()
            else:
                # One round trip, so its handler is surely parked reading its next frame.
                writer.write(frame("nosuch", {}))
                await writer.drain()
                assert json.loads(await reader.readline())["error"] == "unsupported"
            conns.append((reader, writer))
        for w in waits:
            await asyncio.wait_for(w.started.wait(), 2)
        await asyncio.wait_for(server.aclose(), 1)
        tails = [await asyncio.wait_for(r.read(), 1) for r, _ in conns]
        for _, writer in conns:
            writer.close()
        return waits, tails

    waits, tails = asyncio.run(scenario())
    assert [w.cancelled for w in waits] == [True, True]
    assert tails == [b"", b"", b""]


@pytest.mark.parametrize("extra", [b"\n", b"x" * 80 + b"\n"], ids=["blank", "unknown"])
def test_a_frame_sent_while_an_op_waits_keeps_it_cancellable(
    short_dir: Path, extra: bytes
) -> None:
    """A second frame arriving while a `subscribe` waits doesn't take the op out of
    shutdown's reach: `aclose` still cancels it, and nothing is written (#107)."""
    sock = short_dir / "s.sock"

    async def scenario() -> tuple[Waiting, bytes]:
        wait = Waiting()
        server = DaemonServer({"wait": wait}, hangup_cancels=frozenset({"wait"}))
        await server.start(sock, group=None)
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame("wait", {}) + extra)  # in one write
        await writer.drain()
        await asyncio.wait_for(wait.started.wait(), 2)
        for _ in range(5):
            await asyncio.sleep(0)  # the handler reads the second frame and waits on the op
        await asyncio.wait_for(server.aclose(), 1)
        tail = await asyncio.wait_for(reader.read(), 1)
        writer.close()
        return wait, tail

    wait, tail = asyncio.run(scenario())
    assert wait.cancelled
    assert tail == b""


def test_a_connection_accepted_before_close_still_closes(short_dir: Path) -> None:
    """A connection the listener accepted just before `aclose()`, whose handler hasn't taken
    its first step when `aclose()` closes the clients, closes as soon as it does: a `subscribe` it
    sends can't park shutdown (#107)."""
    sock = short_dir / "s.sock"

    async def scenario() -> tuple[Waiting, bytes]:
        wait = Waiting()
        server = DaemonServer({"wait": wait}, hangup_cancels=frozenset({"wait"}))
        gate, entered = asyncio.Event(), asyncio.Event()
        client = server._client

        async def held(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            entered.set()
            await gate.wait()  # accepted, but its handler hasn't started
            await client(reader, writer)

        server._client = held  # type: ignore[method-assign]
        await server.start(sock, group=None)
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame("wait", {}))
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.ensure_future(server.aclose())
        await asyncio.sleep(0)  # `aclose` has closed the clients and waits for the handlers
        gate.set()
        await asyncio.wait_for(closing, 1)
        tail = await asyncio.wait_for(reader.read(), 1)
        writer.close()
        return wait, tail

    wait, tail = asyncio.run(scenario())
    assert not wait.started.is_set()  # the frame was never read
    assert tail == b""


def test_a_bounded_op_in_flight_at_shutdown_finishes(short_dir: Path) -> None:
    """An op outside `hangup_cancels`, a `send` say, runs to its end at shutdown and its reply
    is written: cancelling it after its post reached the server would have the agent post it
    twice. Then the connection closes (#107)."""
    sock = short_dir / "s.sock"

    async def scenario() -> tuple[dict[str, Any], bytes]:
        gate, started = asyncio.Event(), asyncio.Event()

        async def send(args: dict[str, Any]) -> dict[str, Any]:
            started.set()
            await gate.wait()  # the post is on its way
            return {"posted": True}

        server = DaemonServer({"send": send})
        await server.start(sock, group=None)
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame("send", {}))
        await writer.drain()
        await asyncio.wait_for(started.wait(), 2)
        closing = asyncio.ensure_future(server.aclose())
        await asyncio.sleep(0)  # `aclose` has closed the clients and waits for the handlers
        gate.set()
        await asyncio.wait_for(closing, 1)
        reply = json.loads(await asyncio.wait_for(reader.readline(), 1))
        tail = await asyncio.wait_for(reader.read(), 1)
        writer.close()
        return reply, tail

    reply, tail = asyncio.run(scenario())
    assert reply["ok"] is True and reply["result"] == {"posted": True}
    assert tail == b""  # and then the connection closed


@pytest.mark.parametrize("failure", [ConnectionResetError("reset"), OSError("gone")])
def test_failed_read_while_an_op_waits_is_a_hangup(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """The read that watches for a hangup fails: the waiting op is cancelled, nothing is
    written, and the handler returns, instead of waiting on the op forever (#107)."""
    sock = short_dir / "s.sock"
    original = asyncio.StreamReader.readline
    calls = {"n": 0}

    async def readline(self: asyncio.StreamReader) -> bytes:
        calls["n"] += 1
        if calls["n"] == 2:  # the watcher started beside the waiting op
            await asyncio.sleep(0.05)
            raise failure
        return await original(self)

    monkeypatch.setattr(asyncio.StreamReader, "readline", readline)

    async def scenario() -> Waiting:
        wait = Waiting()
        server = DaemonServer({"wait": wait}, hangup_cancels=frozenset({"wait"}))
        await server.start(sock, group=None)
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame("wait", {}))
        await writer.drain()
        tail = await asyncio.wait_for(reader.read(), 2)
        assert tail == b""
        # Not `aclose`, whose `_close_clients` would cancel a stuck op: the handler has to
        # have returned on its own.
        assert server._server is not None
        server._server.close()
        await asyncio.wait_for(server._server.wait_closed(), 1)  # no handler left behind
        writer.close()
        return wait

    wait = asyncio.run(scenario())
    assert wait.cancelled
