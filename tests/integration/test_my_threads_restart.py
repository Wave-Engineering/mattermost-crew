"""Story 2.4b against the CI leg's Mattermost: IT-24 (R-19; D-104).

An agent posts a new root through the daemon's `send` op, the daemon restarts, and a human's
reply in that thread is still addressed to the agent: the restarted daemon's `MyThreads`,
loaded from `<state_dir>/threads/`, holds the root the first daemon recorded.

The harness (broker, registry, adapter, inbox) is Story 2.1's, from `test_mattermost_adapter`;
the chat ops are wired over it as `crewchat serve` wires them.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

from crewchat.addressing import addressed_to, is_direct_address
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.server import DaemonServer, build_handlers
from crewchat.threads import MyThreads

from tests.integration.mmhttp import mm_request
from tests.integration.test_mattermost_adapter import (  # noqa: F401 (`setup` is a fixture)
    Daemon,
    Inbox,
    setup,
)

RETENTION_DAYS = 90


def chat_server(d: Daemon, threads: MyThreads) -> DaemonServer:
    handlers = build_handlers(d.registry.config, d.registry)
    handlers.update(
        build_chat_handlers(d.registry, {d.backend.name: d.backend}, threads, AgentContext())
    )
    return DaemonServer(handlers)


async def call(server: DaemonServer, op: str, **args: Any) -> dict[str, Any]:
    frame = {"proto": 1, "id": op, "op": op, "args": args}
    reply = await server.handle_line(json.dumps(frame).encode())
    assert reply["ok"] is True, reply
    result: dict[str, Any] = reply["result"]
    return result


def test_it24_my_threads_survive_restart(
    mattermost: Any,
    setup: dict[str, str],  # noqa: F811
    tmp_path: Path,
) -> None:
    """IT-24."""
    channel = setup["channel_id"]
    state_dir = tmp_path / "state"

    async def scenario() -> None:
        # The first daemon: babel posts a new root through the `send` op.
        d1 = Daemon(mattermost, setup, tmp_path)
        await d1.register("babel")
        threads1 = MyThreads.load(state_dir, RETENTION_DAYS)
        server1 = chat_server(d1, threads1)
        try:
            sent = await call(
                server1,
                "send",
                dev_name="babel",
                backend=d1.backend.name,
                to=channel,
                text="babel opens a thread before the restart",
            )
        finally:
            await d1.backend.aclose()
        root = sent["post_id"]
        assert sent["thread"] == root
        assert threads1.roots("babel") == {root}
        path = state_dir / "threads" / "babel.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

        # The restart: a new daemon over the same state and token directories.
        d2 = Daemon(mattermost, setup, tmp_path)
        d2.store.load()
        reg = await d2.registry.register("babel", None, "it-session-2")
        threads2 = MyThreads.load(state_dir, RETENTION_DAYS)
        assert threads2.roots("babel") == {root}
        inbox = Inbox(d2.backend, "babel")
        try:
            await inbox.connected()
            resp = mm_request(
                mattermost,
                "POST",
                "/posts",
                json={"channel_id": channel, "root_id": root, "message": "a human replies"},
            )
            assert resp.status_code == 201, resp.text
            m = (await inbox.until("a human replies"))[-1]
            assert m.root_id == root and m.dm is False
            mine = threads2.roots("babel")
            # No mention, no DM: only the persisted thread set makes it babel's.
            assert addressed_to(m, reg, mine)
            assert is_direct_address(m, reg, mine)
            # Without persistence (the bare in-memory set) it would not be.
            assert not addressed_to(m, reg, MyThreads().roots("babel"))
        finally:
            await inbox.close()
            await d2.backend.aclose()

    asyncio.run(scenario())
