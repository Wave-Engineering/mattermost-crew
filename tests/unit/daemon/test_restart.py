"""#129: a daemon restart, as the agent's side lives through it.

The pieces are the real ones, wired as `crewchat serve` wires them: `DaemonServer` with
`register`, `health`, `ping` and the doorbell's `subscribe`, over the conftest's registry and
fake brokers; the shim over its `SocketTransport`; and the one-shot's `run`, in a thread, as
the agent's background task runs it. The restart is the server's own shutdown (every
connection closed, the socket file left refusing), a gap, then a second daemon with a fresh
registry on the same path.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from crewchat.config import ServersConfig
from crewchat.doorbell import CursorStore, Doorbell, DoorbellEvent, build_doorbell_handlers
from crewchat.server import DaemonServer, build_handlers
from crewchat_clients import doorbell as one_shot
from crewchat_clients import mcp_shim
from crewchat_clients.mcp_shim import Shim, SocketTransport

from tests.unit.daemon.conftest import Clock, Daemon

DEV = "babel"


def serve(daemon: Daemon) -> tuple[DaemonServer, Doorbell]:
    bell = Doorbell({}, CursorStore(None))
    handlers = build_handlers(daemon.config, daemon.registry)
    handlers.update(build_doorbell_handlers(daemon.registry, bell, handlers["register"]))
    return DaemonServer(handlers, hangup_cancels=frozenset({"subscribe"})), bell


def make_repo(root: Path) -> Path:
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "agent-identity.json").write_text(json.dumps({"dev_name": DEV}))
    return root


def test_one_shot_and_shim_ride_out_a_daemon_restart(
    servers_config: ServersConfig,
    tmp_path: Path,
    clock: Clock,
    short_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_shim, "PING_EVERY", timedelta(0))  # the timer's every tick
    # Long enough for the restart below, short enough that a failure isn't a minute's hang.
    monkeypatch.setattr(one_shot, "RESTART_GRACE_S", 3.0)
    repo = make_repo(tmp_path / "repo")
    sock = short_dir / "crewchat.sock"
    out, err = io.StringIO(), io.StringIO()
    environ = {"XDG_STATE_HOME": str(tmp_path / "state")}
    event = {"type": "message", "backend": "discord", "message": {"text": "after the restart"}}

    async def scenario() -> tuple[int, Daemon]:
        first = Daemon(servers_config, tmp_path / "tokens-1", clock)
        server, _ = serve(first)
        await server.start(sock, group=None)
        shim = Shim(
            repo,
            SocketTransport(sock),
            headless=False,
            defaults_file=tmp_path / "chat-defaults.json",
            remote_for=lambda _root: None,
            session_id="s1",
            environ={},
            legacy_dir=tmp_path / "no-legacy",
        )
        await shim.start()
        ringing = asyncio.create_task(
            asyncio.to_thread(one_shot.run, repo, sock, environ, out, err)
        )
        await asyncio.sleep(0.3)  # the one-shot is waiting in `subscribe`

        await server.aclose()  # the stop: its connection is closed under it
        await asyncio.sleep(0.5)  # the gap: the socket file refuses
        second = Daemon(servers_config, tmp_path / "tokens-2", clock)
        server, bell = serve(second)
        await server.start(sock, group=None)
        try:
            await asyncio.sleep(0.5)  # the one-shot gets `not_registered`, and waits
            assert not ringing.done(), err.getvalue()
            assert second.registry.get(DEV) is None
            await shim.tick()  # no tool call: the ping registers the agent again
            assert second.registry.get(DEV) is not None
            bell.deliver(DEV, DoorbellEvent("discord", event, keys=("m1",)))
            code = await asyncio.wait_for(ringing, timeout=10)
        finally:
            await server.aclose()
        return code, second

    code, _ = asyncio.run(scenario())
    assert code == one_shot.EXIT_EVENT, err.getvalue()
    printed: dict[str, Any] = json.loads(out.getvalue())
    assert printed == {**event, "event_id": "discord:m1"}
