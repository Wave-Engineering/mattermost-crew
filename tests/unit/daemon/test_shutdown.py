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


def test_close_clients_lets_wait_closed_return(short_dir: Path) -> None:
    """Two connections each with a waiting op: after `close()` and `close_clients()`,
    `wait_closed()` returns, and both ops were cancelled."""
    sock = short_dir / "s.sock"

    async def scenario() -> list[Waiting]:
        waits = [Waiting(), Waiting()]
        server = DaemonServer(
            {"wait0": waits[0], "wait1": waits[1]},
            hangup_cancels=frozenset({"wait0", "wait1"}),
        )
        listener = await server.start(sock, group=None)
        writers = []
        for i in range(2):
            _, writer = await asyncio.open_unix_connection(str(sock))
            writer.write(frame(f"wait{i}", {}))
            await writer.drain()
            writers.append(writer)
        for w in waits:
            await asyncio.wait_for(w.started.wait(), 2)
        listener.close()
        server.close_clients()
        await asyncio.wait_for(listener.wait_closed(), 1)
        for writer in writers:
            writer.close()
        return waits

    waits = asyncio.run(scenario())
    assert [w.cancelled for w in waits] == [True, True]


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
        listener = await server.start(sock, group=None)
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame("wait", {}))
        await writer.drain()
        tail = await asyncio.wait_for(reader.read(), 2)
        assert tail == b""
        listener.close()
        await asyncio.wait_for(listener.wait_closed(), 1)  # no handler left behind
        writer.close()
        return wait

    wait = asyncio.run(scenario())
    assert wait.cancelled
