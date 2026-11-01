"""Story 3.2 against the CI leg's Mattermost: IT-11, IT-12 and IT-13 (R-26, R-27, R-28).

The daemon side is wired as `crewchat serve` wires it: the registry, token store and
Mattermost adapter of `test_mattermost_adapter.Daemon` (over its in-process broker), the
Discord adapter on the scream-hole fake's real port, and the squawkie adapter on the squawkie
fake's real port (`tests/fakes/squawkie.py`, a real SSE feed). The doorbell runs one task per
(agent, backend) with its cursors on disk, and `subscribe` is served on a real socket, called
the way the one-shot calls it: one frame, one reply. IT-11 runs end to end through the real
one-shot, the installed `crewchat-doorbell` console script (Story 3.4).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from crewchat.__main__ import call
from crewchat.adapters.discord import DiscordBackend
from crewchat.adapters.mattermost import MattermostBackend
from crewchat.adapters.squawkie import SquawkieBackend
from crewchat.doorbell import CursorStore, Doorbell, Source, build_doorbell_handlers
from crewchat.server import DaemonServer

from tests.fakes.screamhole import BOT_TOKEN, FakeDiscord
from tests.fakes.squawkie import FakeSquawkie
from tests.integration.test_mattermost_adapter import SID, Daemon, setup

__all__ = ["setup"]  # the module's Mattermost team, broker and channel (a fixture)

MM = f"mattermost:{SID}"
DISCORD_CHANNEL = "700000000000000901"
HUMAN = {"id": "500000000000000001", "username": "bj", "global_name": "BJ", "bot": False}


@pytest.fixture
def short_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="cc-it-", dir="/tmp") as d:
        yield Path(d)


class Rig:
    """A daemon's doorbell over the three backends, with `subscribe` on a socket."""

    def __init__(
        self,
        mattermost: Any,
        setup: dict[str, str],
        root: Path,
        sock: Path,
        urls: dict[str, str],
        dev: str,
    ) -> None:
        self.d = Daemon(mattermost, setup, root)
        self.mattermost = mattermost
        self.dev = dev
        self.root = root
        self.sock = sock
        self.urls = urls
        self.ids: dict[str, list[str]] = {}
        cred = root / "discord.credential"
        cred.write_text(BOT_TOKEN + "\n")
        cred.chmod(0o600)
        self.cred = cred

    def build(self) -> None:
        self.discord = DiscordBackend(
            self.urls["screamhole"], self.cred, channels=[DISCORD_CHANNEL], poll_interval_s=0.1
        )
        self.squawkie = SquawkieBackend(
            self.urls["squawkie"], ids_for=lambda d: self.ids.get(d, [])
        )
        sources: dict[str, Source] = {
            MM: self.d.backend,
            "discord": self.discord,
            "squawkie": self.squawkie,
        }
        # deaf_alarm_s=0: IT-12 asserts the down/up pair at once, not the 10-minute alarm (#202).
        self.doorbell = Doorbell(sources, CursorStore(self.root / "state"), deaf_alarm_s=0)
        self.d.store.adapters.insert(0, self.doorbell)

        async def no_register(args: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("not used")

        handlers = build_doorbell_handlers(self.d.registry, self.doorbell, no_register)
        self.server = DaemonServer(handlers, hangup_cancels=frozenset({"subscribe"}))

    async def start(self) -> None:
        self.build()
        await self.server.start(self.sock, group=None)
        self.doorbell.attach(self.dev, [MM, "discord", "squawkie"])

    async def subscribe(self, timeout: float = 20) -> dict[str, Any]:
        """One `subscribe` over the socket, as the one-shot sends it."""
        reply = await asyncio.wait_for(
            asyncio.to_thread(call, str(self.sock), "subscribe", {"dev_name": self.dev}, timeout),
            timeout + 5,
        )
        assert reply["ok"] is True, reply
        result: dict[str, Any] = reply["result"]
        return result

    async def ring(self, timeout: float = 20) -> dict[str, Any]:
        """One run of the real one-shot, `crewchat-doorbell`, as an agent's background task
        runs it: it must print exactly one JSON line and exit 0 (R-26, CT-08)."""
        repo = self.root / "agent-repo"
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "agent-identity.json").write_text(json.dumps({"dev_name": self.dev}))
        script = Path(sys.executable).parent / "crewchat-doorbell"
        assert script.exists(), f"{script} is not installed: run uv sync"
        proc = await asyncio.create_subprocess_exec(
            str(script),
            "--socket",
            str(self.sock),
            "--repo-root",
            str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its seen set (#109) under the rig, not in the runner's home.
            env={**os.environ, "XDG_STATE_HOME": str(self.root / "agent-state")},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        assert proc.returncode == 0, err.decode()
        lines = out.decode().splitlines()
        assert len(lines) == 1, out
        event: dict[str, Any] = json.loads(lines[0])
        return event

    async def mm_connected(self) -> None:
        async with asyncio.timeout(20):
            while True:
                agent = self.d.backend._agents.get(self.dev)
                if agent is not None and agent.ws is not None and agent.hello:
                    return
                await asyncio.sleep(0.05)

    async def new_mattermost_adapter(self) -> None:
        """A restarted daemon's adapter: nothing of the old one's state survives."""
        old = self.d.backend
        self.d.store.adapters.remove(old)
        await old.aclose()
        self.d.backend = MattermostBackend(
            SID, self.mattermost.url, self.d.store.token_for, refresher=self.d.store
        )
        self.d.store.adapters.append(self.d.backend)

    async def stop(self) -> None:
        await self.server.aclose()  # first, as `serve` does: no op waits on what closes next
        self.d.store.adapters.remove(self.doorbell)
        await self.doorbell.aclose()
        await self.d.backend.close_agent(self.dev)
        await self.discord.aclose()
        await self.squawkie.aclose()


@pytest.fixture
def fakes() -> Iterator[tuple[FakeDiscord, FakeSquawkie, dict[str, str]]]:
    discord = FakeDiscord()
    discord.add_channel("general", channel_id=DISCORD_CHANNEL)
    sq = FakeSquawkie()
    with discord.serve() as sh_url, sq.serve() as sq_url:
        yield discord, sq, {"screamhole": sh_url, "squawkie": sq_url}


def message_text(event: dict[str, Any]) -> str:
    assert event["type"] == "message", event
    return str(event["message"]["text"])


def test_it11_three_adapters_merge_one_event_per_subscribe(
    mattermost: Any, setup: dict[str, str], tmp_path: Path, short_dir: Path, fakes: Any
) -> None:
    """IT-11 (R-26, CT-08): Mattermost, Discord and squawkie events merge onto the agent's one
    queue, and each run of the real one-shot prints exactly one of them and exits 0."""
    discord, sq, urls = fakes
    needs = sq.add_list("bj:needs")
    squawk = sq.add_squawk(needs, "ship it?")

    async def scenario() -> list[dict[str, Any]]:
        rig = Rig(mattermost, setup, tmp_path, short_dir / "d.sock", urls, "bell11")
        await rig.d.register(rig.dev)
        await rig.d.register("talk11")
        rig.ids[rig.dev] = [str(squawk["id"])]
        await rig.start()
        try:
            await rig.mm_connected()
            async with asyncio.timeout(20):
                while not sq.open_streams():
                    await asyncio.sleep(0.05)
            await rig.d.backend.send("talk11", setup["channel_id"], "it-11 mattermost")
            discord.add_message(DISCORD_CHANNEL, "it-11 discord", author=HUMAN)
            sq.update(squawk["id"], text="ship it? → yes")
            got = [await rig.ring() for _ in range(3)]
            assert rig.doorbell.queue(rig.dev).empty()
            return got
        finally:
            await rig.stop()
            await rig.d.backend.aclose()

    got = asyncio.run(scenario())
    assert sorted(e["backend"] for e in got) == sorted([MM, "discord", "squawkie"])
    by = {e["backend"]: e for e in got}
    assert message_text(by[MM]) == "it-11 mattermost"
    assert message_text(by["discord"]).startswith("it-11 discord")
    assert by["squawkie"]["type"] == "squawk.updated"
    assert by["squawkie"]["squawk"]["id"] == squawk["id"]


def test_it12_one_backend_down_others_flow(
    mattermost: Any, setup: dict[str, str], tmp_path: Path, short_dir: Path, fakes: Any
) -> None:
    """IT-12 (R-27): killing one adapter's connection enqueues exactly one `backend.down`,
    and the other backends keep delivering; its recovery enqueues one `backend.up`."""
    discord, sq, urls = fakes

    async def scenario() -> list[dict[str, Any]]:
        rig = Rig(mattermost, setup, tmp_path, short_dir / "d.sock", urls, "bell12")
        await rig.d.register(rig.dev)
        await rig.d.register("talk12")
        await rig.start()
        try:
            await rig.mm_connected()
            async with asyncio.timeout(20):
                while not sq.open_streams():
                    await asyncio.sleep(0.05)
            # squawkie's connection is killed, and it refuses to come back for a while.
            sq.refuse_streams = 3
            sq.drop_streams()
            got = [await rig.subscribe()]
            await rig.d.backend.send("talk12", setup["channel_id"], "it-12 mattermost")
            discord.add_message(DISCORD_CHANNEL, "it-12 discord", author=HUMAN)
            got += [await rig.subscribe(), await rig.subscribe()]
            got.append(await rig.subscribe(timeout=60))  # squawkie back: up
            # Killing Mattermost's websocket: one down for it, then up on the reconnect.
            ws = rig.d.backend._agents[rig.dev].ws
            assert ws is not None
            await ws.close()
            got += [await rig.subscribe(), await rig.subscribe(timeout=60)]
            await asyncio.sleep(1)
            assert rig.doorbell.queue(rig.dev).empty()
            return got
        finally:
            await rig.stop()
            await rig.d.backend.aclose()

    got = asyncio.run(scenario())
    kinds = [(e["backend"], e["type"]) for e in got]
    assert kinds[0] == ("squawkie", "backend.down")
    assert sorted(kinds[1:3]) == [("discord", "message"), (MM, "message")]
    assert kinds[3] == ("squawkie", "backend.up")
    assert kinds[4] == (MM, "backend.down")
    assert kinds[5] == (MM, "backend.up")
    assert kinds.count(("squawkie", "backend.down")) == 1
    assert kinds.count((MM, "backend.down")) == 1


def test_it13_reconnect_replays_from_the_cursor_exactly_once(
    mattermost: Any, setup: dict[str, str], tmp_path: Path, short_dir: Path, fakes: Any
) -> None:
    """IT-13 (R-28): posts made while the agent's stream is gone, across a dropped websocket
    and across a daemon restart, are delivered from the saved cursor, each exactly once."""
    _discord, _sq, urls = fakes
    channel = setup["channel_id"]

    async def scenario() -> tuple[list[str], list[str]]:
        rig = Rig(mattermost, setup, tmp_path, short_dir / "d.sock", urls, "bell13")
        await rig.d.register(rig.dev)
        await rig.d.register("talk13")
        await rig.start()
        texts: list[str] = []
        try:
            await rig.mm_connected()
            for n in (1, 2):
                await rig.d.backend.send("talk13", channel, f"it-13 event {n}")
                texts.append(message_text(await rig.subscribe()))
            # The websocket drops; a post lands before the reconnect (a 1 s backoff).
            ws = rig.d.backend._agents[rig.dev].ws
            assert ws is not None
            await ws.close()
            await rig.d.backend.send("talk13", channel, "it-13 event 3")
            seen_health = []
            while len(texts) < 3:
                event = await rig.subscribe(timeout=60)
                if event["type"] == "message":
                    texts.append(message_text(event))
                else:
                    seen_health.append(event["type"])
            await rig.d.backend.send("talk13", channel, "it-13 event 4")
            event = await rig.subscribe(timeout=60)
            while event["type"] != "message":
                seen_health.append(event["type"])
                event = await rig.subscribe(timeout=60)
            texts.append(message_text(event))
            assert seen_health.count("backend.down") <= 1
        finally:
            await rig.stop()

        # The daemon stops; two posts land; it starts again on the same cursors.
        await rig.d.backend.send("talk13", channel, "it-13 event 5")
        await rig.d.backend.send("talk13", channel, "it-13 event 6")
        after: list[str] = []
        await rig.new_mattermost_adapter()
        await rig.start()
        try:
            for _ in range(2):
                after.append(message_text(await rig.subscribe(timeout=60)))
            await rig.d.backend.send("talk13", channel, "it-13 event 7")
            after.append(message_text(await rig.subscribe(timeout=60)))
            await asyncio.sleep(1)
            assert rig.doorbell.queue(rig.dev).empty()
            return texts, after
        finally:
            await rig.stop()
            await rig.d.backend.aclose()

    texts, after = asyncio.run(scenario())
    assert texts == [f"it-13 event {n}" for n in (1, 2, 3, 4)]
    assert after == [f"it-13 event {n}" for n in (5, 6, 7)]
