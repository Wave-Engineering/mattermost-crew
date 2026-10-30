"""IT-26 (#137): an idle shim registers again after a daemon restart, with no tool call.

The pieces are the real ones, wired as `crewchat serve` and the MCP server wire them:
`DaemonServer` with `register`, `health`, `ping` and the doorbell's `subscribe`, over the unit
conftest's registry and fake brokers; the shim over its `SocketTransport`, with its timer task
running as the MCP server's lifespan starts it, at the real `POLL_INTERVAL_S` and `PING_EVERY`;
and the one-shot doorbell, armed in a thread, at its real `RESTART_GRACE_S`. Nothing calls a
tool or ticks by hand: only the timer can bring the agent back. The kiosk and squawk pushes
aren't wired here, so the daemon answers them `unsupported`, which the shim logs and carries on.

`ping-less` is the field case from malory's 0.2.3 → 0.2.5 upgrade: the shim started against a
daemon without `ping`, got `unsupported`, and stopped asking for good, so the upgrade (itself
a restart) left every idle session unregistered until a call.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path
from typing import Any

import pytest
from crewchat.config import ServersConfig
from crewchat.doorbell import CursorStore, Doorbell, DoorbellEvent, build_doorbell_handlers
from crewchat.server import DaemonServer, build_handlers
from crewchat_clients import doorbell as one_shot
from crewchat_clients import mcp_shim
from crewchat_clients.mcp_shim import DaemonError, Shim, SocketTransport

from tests.unit.daemon.conftest import (  # noqa: F401 (fixtures)
    Clock,
    Daemon,
    clock,
    servers_config,
    short_dir,
)

DEV = "babel"
BACK_WITHIN_S = 20.0  # the #137 bar: inside the doorbell's 60 s grace, with room to spare


def serve(daemon: Daemon, *, ping: bool) -> tuple[DaemonServer, Doorbell]:
    bell = Doorbell({}, CursorStore(None))
    handlers = build_handlers(daemon.config, daemon.registry)
    if not ping:
        del handlers["ping"]  # a daemon from before #129: the op answers `unsupported`
    handlers.update(build_doorbell_handlers(daemon.registry, bell, handlers["register"]))
    return DaemonServer(handlers, hangup_cancels=frozenset({"subscribe"})), bell


class Recording(SocketTransport):
    """The real transport, keeping each `ping`'s answer: `ok`, or the daemon's error code."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.pings: list[str] = []

    async def __call__(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await super().__call__(op, args)
        except DaemonError as exc:
            if op == "ping":
                self.pings.append(exc.code)
            raise
        if op == "ping":
            self.pings.append("ok")
        return result


def make_repo(root: Path) -> Path:
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "agent-identity.json").write_text(json.dumps({"dev_name": DEV}))
    return root


@pytest.mark.parametrize("first_has_ping", [False, True], ids=["ping-less", "with-ping"])
def test_it26_idle_shim_registers_again_after_a_daemon_restart(
    servers_config: ServersConfig,  # noqa: F811
    tmp_path: Path,
    clock: Clock,  # noqa: F811
    short_dir: Path,  # noqa: F811
    first_has_ping: bool,
) -> None:
    repo = make_repo(tmp_path / "repo")
    sock = short_dir / "crewchat.sock"
    out, err = io.StringIO(), io.StringIO()
    environ = {"XDG_STATE_HOME": str(tmp_path / "state")}
    event = {"type": "message", "backend": "discord", "message": {"text": "after the restart"}}

    async def scenario() -> tuple[int, float]:
        first = Daemon(servers_config, tmp_path / "tokens-1", clock)
        server, _ = serve(first, ping=first_has_ping)
        await server.start(sock, group=None)
        transport = Recording(sock)
        shim = Shim(
            repo,
            transport,
            headless=False,
            defaults_file=tmp_path / "chat-defaults.json",
            remote_for=lambda _root: None,
            session_id="s1",
            environ={},
            legacy_dir=tmp_path / "no-legacy",
        )
        await shim.start()
        timers = asyncio.create_task(shim.run_timers())  # as the MCP server's lifespan does
        ringing = asyncio.create_task(
            asyncio.to_thread(one_shot.run, repo, sock, environ, out, err)
        )
        try:
            # The timer's first tick pings the first daemon. The ping-less one answers
            # `unsupported`, which is what used to switch the ping off.
            await asyncio.sleep(mcp_shim.POLL_INTERVAL_S + 1)
            assert transport.pings == ["ok" if first_has_ping else "unsupported"]

            await server.aclose()  # the stop: the doorbell's `subscribe` is cut under it
            await asyncio.sleep(1.0)  # the gap: the socket file refuses
            second = Daemon(servers_config, tmp_path / "tokens-2", clock)
            server, bell = serve(second, ping=True)
            await server.start(sock, group=None)
            started = time.monotonic()
            while second.registry.get(DEV) is None:
                assert time.monotonic() - started < BACK_WITHIN_S, "the ping never registered"
                assert not ringing.done(), err.getvalue()
                await asyncio.sleep(0.1)
            back_after = time.monotonic() - started

            assert not ringing.done(), err.getvalue()  # the armed doorbell rode through
            await asyncio.sleep(1.0)  # its `subscribe` retry lands on the registered agent
            bell.deliver(DEV, DoorbellEvent("discord", event, keys=("m1",)))
            code = await asyncio.wait_for(ringing, timeout=10)
        finally:
            timers.cancel()
            await server.aclose()
        return code, back_after

    code, back_after = asyncio.run(scenario())
    assert back_after < BACK_WITHIN_S
    assert code == one_shot.EXIT_EVENT, err.getvalue()
    printed: dict[str, Any] = json.loads(out.getvalue())
    assert printed == {**event, "event_id": "discord:m1"}
