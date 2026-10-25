"""Story 3.2: the doorbell core (R-26, R-27, R-28).

The three adapters are the real ones, over their fakes: Mattermost (`FakeMattermost`, in
conftest), Discord (`tests/fakes/screamhole.py`) and squawkie (`tests/fakes/squawkie.py`).
"""

from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from crewchat.adapters.base import NO_STATUS, InboundMessage, StatusSink, StreamCursor
from crewchat.adapters.discord import DiscordBackend
from crewchat.adapters.mattermost import MattermostBackend
from crewchat.adapters.squawkie import SquawkieBackend
from crewchat.doorbell import (
    CursorStore,
    Doorbell,
    DoorbellEvent,
    Source,
    build_doorbell_handlers,
)
from crewchat.server import DaemonServer, OpError, Reply

from tests.fakes.screamhole import BOT_TOKEN, DISCORD_API, SCREAMHOLE_URL, FakeDiscord
from tests.fakes.squawkie import URL as SQUAWKIE_URL
from tests.fakes.squawkie import FakeSquawkie

DEV = "babel"
SID = "dev"
MM = f"mattermost:{SID}"
HUMAN = {"id": "500000000000000001", "username": "bj", "global_name": "BJ", "bot": False}
DISCORD_CHANNEL = "700000000000000901"


async def fast_sleep(_seconds: float) -> None:
    await asyncio.sleep(0.001)


class Tokens:
    def __init__(self, mm: Any) -> None:
        self.mm = mm
        self.held: dict[str, str] = {}

    def add(self, dev: str) -> str:
        user_id = str(self.mm.add_user(dev, is_bot=True))
        self.held[dev] = self.mm.add_token(user_id)
        return user_id

    def token_for(self, dev: str, server_id: str) -> str | None:
        return self.held.get(dev)

    async def refresh(self, dev: str, server_id: str, failed_token: str) -> None:
        return None

    async def call_with_refresh(self, dev: str, server_id: str, request: Any) -> Any:
        return await request(self.held[dev])


class StubRegistry:
    def __init__(self, *devs: str) -> None:
        self.devs = set(devs)

    def get(self, dev_name: str) -> Any:
        return object() if dev_name in self.devs else None


class World:
    """The three backends over their fakes, and a doorbell on them."""

    def __init__(self, mm: Any, root: Path) -> None:
        self.mm = mm
        self.root = root
        self.tokens = Tokens(mm)
        self.me = self.tokens.add(DEV)
        self.sender = mm.add_user("bj")
        self.team = mm._id("t")
        self.chan = mm._id("c")
        mm.teams[self.team] = [{"id": self.chan, "type": "O", "name": "crew"}]
        self.discord = FakeDiscord()
        self.discord.add_channel("general", channel_id=DISCORD_CHANNEL)
        cred = root / "discord.credential"
        cred.write_text(BOT_TOKEN + "\n")
        cred.chmod(0o600)
        self.cred = cred
        self.sq = FakeSquawkie()
        self.needs = self.sq.add_list("bj:needs")
        self.squawk = self.sq.add_squawk(self.needs, "deploy now?")
        self.ids = {DEV: [str(self.squawk["id"])]}
        self.build()

    def build(self) -> None:
        self.mattermost = MattermostBackend(
            SID,
            self.mm.URL,
            self.tokens.token_for,
            refresher=self.tokens,
            http_client=self.mm.http_client(),
            connect=self.mm.connect,
            sleep=fast_sleep,
            max_retries=0,
        )
        self.disc = DiscordBackend(
            SCREAMHOLE_URL,
            self.cred,
            discord_api=DISCORD_API,
            http_client=self.discord.http_client(),
            channels=[DISCORD_CHANNEL],
            sleep=fast_sleep,
            poll_interval_s=0.005,
        )
        self.squawkie = SquawkieBackend(
            SQUAWKIE_URL,
            ids_for=lambda d: self.ids.get(d, []),
            http_client=self.sq.http_client(),
            sleep=fast_sleep,
        )
        sources: dict[str, Source] = {
            MM: self.mattermost,
            "discord": self.disc,
            "squawkie": self.squawkie,
        }
        self.doorbell = Doorbell(sources, CursorStore(self.root / "state"), sleep=fast_sleep)

    async def connected(self) -> None:
        async with asyncio.timeout(3):
            while not (
                self.mm.open_sockets(self.me)
                and self.mattermost._agents[DEV].hello
                and self.sq.open_streams()
                and any(
                    s == "scream-hole" and "/messages" in p for s, _, p in self.discord.requests
                )
            ):
                await asyncio.sleep(0.001)

    def mm_post(self, text: str, *, broadcast: bool = True) -> dict[str, Any]:
        post: dict[str, Any] = self.mm.add_post(self.sender, self.chan, text)
        if broadcast:
            self.mm.broadcast_posted(post)
        return post

    async def next(self, timeout: float = 3) -> DoorbellEvent:
        return await asyncio.wait_for(self.doorbell.next_event(DEV), timeout)

    async def close(self) -> None:
        await self.doorbell.aclose()
        await self.mattermost.aclose()
        await self.disc.aclose()
        await self.squawkie.aclose()


@pytest.fixture
def world(mm: Any, tmp_path: Path) -> World:
    return World(mm, tmp_path)


def kind(event: DoorbellEvent) -> tuple[str, str]:
    return (event.backend, str(event.payload["type"]))


def text_of(event: DoorbellEvent) -> str:
    return str(event.payload["message"]["text"])


def test_three_adapters_one_queue(world: World, run: Any) -> None:
    """R-26: Mattermost, Discord and squawkie events arrive on the agent's one queue."""

    async def scenario() -> list[DoorbellEvent]:
        assert world.doorbell.attach(DEV, [MM, "discord", "squawkie", "nope"]) == [
            MM,
            "discord",
            "squawkie",
        ]
        assert world.doorbell.attach(DEV, [MM]) == []  # already running
        await world.connected()
        world.mm_post("from mattermost")
        world.discord.add_message(DISCORD_CHANNEL, "from discord", author=HUMAN)
        world.sq.update(world.squawk["id"], text="deploy now? → yes")
        got = [await world.next() for _ in range(3)]
        assert world.doorbell.queue(DEV).empty()
        await world.close()
        return got

    got = run(scenario())
    by_backend = {e.backend: e for e in got}
    assert set(by_backend) == {MM, "discord", "squawkie"}
    assert text_of(by_backend[MM]) == "from mattermost"
    assert by_backend[MM].payload["backend"] == MM
    assert text_of(by_backend["discord"]).startswith("from discord")
    assert by_backend["squawkie"].payload["type"] == "squawk.updated"
    assert by_backend["squawkie"].payload["backend"] == "squawkie"
    json.dumps([e.payload for e in got])  # each is a JSON line for the one-shot


def test_subscribe_returns_one_event(world: World, run: Any) -> None:
    """One `subscribe` returns exactly one event; an unregistered agent is refused."""

    async def registered(args: dict[str, Any]) -> dict[str, Any]:
        return {"account": f"{args['dev_name']}.oaw"}

    handlers = build_doorbell_handlers(
        StubRegistry(DEV),  # type: ignore[arg-type]
        world.doorbell,
        registered,
        extra_backends=["squawkie"],
    )

    async def scenario() -> tuple[Reply, Reply, Reply, int]:
        # `register` attaches the registration's backends and squawkie.
        await handlers["register"]({"dev_name": "stranger"})
        assert world.doorbell.attached("stranger") == []  # no registration, nothing attached
        await world.doorbell.offer(DEV, MM, DoorbellEvent(MM, {"type": "message", "n": 1}, ("a",)))
        await world.doorbell.offer(DEV, MM, DoorbellEvent(MM, {"type": "message", "n": 2}, ("b",)))
        first = await handlers["subscribe"]({"dev_name": DEV})
        left = world.doorbell.queue(DEV).qsize()
        # Not yet sent: the next subscribe gets the same event again (#99).
        again = await handlers["subscribe"]({"dev_name": DEV})
        assert isinstance(first, Reply) and isinstance(again, Reply)
        again.on_sent()
        second = await handlers["subscribe"]({"dev_name": DEV})
        with pytest.raises(OpError, match="not_registered"):
            await handlers["subscribe"]({"dev_name": "stranger"})
        with pytest.raises(OpError, match="bad_request"):
            await handlers["subscribe"]({})
        await world.close()
        assert isinstance(second, Reply)
        return first, again, second, left

    first, again, second, left = run(scenario())
    assert first.result == {"type": "message", "n": 1, "event_id": f"{MM}:a"}
    assert again.result == first.result
    assert left == 1
    assert second.result == {"type": "message", "n": 2, "event_id": f"{MM}:b"}


def test_event_moves_the_cursor_only_once_sent(world: World, run: Any) -> None:
    """#99: at least once. The cursor moves when the reply is sent, not when it is taken; two
    overlapping subscribes get the one held event, and one acknowledgement moves it once."""
    handlers = build_doorbell_handlers(
        StubRegistry(DEV),  # type: ignore[arg-type]
        world.doorbell,
        lambda args: asyncio.sleep(0, {}),
    )
    cursors = world.doorbell.cursors

    async def scenario() -> None:
        a = asyncio.ensure_future(handlers["subscribe"]({"dev_name": DEV}))
        b = asyncio.ensure_future(handlers["subscribe"]({"dev_name": DEV}))
        await asyncio.sleep(0.01)
        assert not a.done() and not b.done()  # both wait on the empty queue
        for n, key in ((1, "a"), (2, "b")):
            event = DoorbellEvent(MM, {"type": "message", "n": n}, (key,), float(n))
            await world.doorbell.offer(DEV, MM, event)
        first, second = await asyncio.wait_for(asyncio.gather(a, b), 2)
        assert first.result == second.result  # the same held event, not the next one
        assert first.result["n"] == 1
        assert cursors.load(DEV, MM) is None  # taken, not sent: the cursor hasn't moved
        first.on_sent()
        moved = cursors.load(DEV, MM)
        assert moved is not None and moved.id == "a"
        second.on_sent()  # already acknowledged: nothing more
        assert cursors.load(DEV, MM) == moved
        third = await handlers["subscribe"]({"dev_name": DEV})
        assert third.result["n"] == 2
        await world.close()

    run(scenario())


def test_handover_runs_once(world: World, run: Any) -> None:
    """An event's `on_handover` runs when it is acknowledged, and only once."""
    calls: list[str] = []

    async def scenario() -> None:
        event = DoorbellEvent(MM, {"type": "kiosk.review"}, on_handover=lambda: calls.append("x"))
        assert world.doorbell.deliver(DEV, event)
        held = await world.doorbell.take(DEV)
        assert held is event and calls == []
        assert await world.doorbell.take(DEV) is event  # still held
        world.doorbell.ack(DEV, event)
        world.doorbell.ack(DEV, event)
        assert calls == ["x"]
        # A keyless event's ID is made once, so a repeat of it carries the same one.
        assert event.event_id == f"{MM}:{event.nonce}" == event.reply()["event_id"]
        await world.close()

    run(scenario())


def test_register_attaches_the_registrations_backends(world: World, run: Any) -> None:
    class Reg:
        dev_name = DEV
        backends = (MM, "discord")

    class Registry:
        def get(self, dev_name: str) -> Any:
            return Reg() if dev_name == DEV else None

    async def register(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    handlers = build_doorbell_handlers(
        Registry(),  # type: ignore[arg-type]
        world.doorbell,
        register,
        extra_backends=["squawkie"],
    )

    async def scenario() -> list[str]:
        assert await handlers["register"]({"dev_name": DEV}) == {"ok": True}
        attached = world.doorbell.attached(DEV)
        await world.close()
        return attached

    assert run(scenario()) == sorted([MM, "discord", "squawkie"])


def test_subscribe_hangup_keeps_the_event(world: World, short_dir: Path, run: Any) -> None:
    """A one-shot that hangs up while `subscribe` waits takes nothing off the queue."""
    handlers = build_doorbell_handlers(
        StubRegistry(DEV),  # type: ignore[arg-type]
        world.doorbell,
        _never,
    )
    server = DaemonServer(handlers, hangup_cancels=frozenset({"subscribe"}))
    sock = short_dir / "d.sock"
    frame = b'{"proto":1,"id":"1","op":"subscribe","args":{"dev_name":"babel"}}\n'

    async def scenario() -> tuple[int, dict[str, Any]]:
        listener = await server.start(sock, group=None)
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame)
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()  # the one-shot hung up (a timeout, say)
        await writer.wait_closed()
        await asyncio.sleep(0.05)
        await world.doorbell.offer(DEV, MM, DoorbellEvent(MM, {"type": "message", "n": 1}, ("a",)))
        await asyncio.sleep(0.01)
        queued = world.doorbell.queue(DEV).qsize()
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame)
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
        writer.close()
        listener.close()
        await listener.wait_closed()
        await world.close()
        return queued, reply

    queued, reply = run(scenario())
    assert queued == 1
    assert reply["ok"] is True
    assert reply["result"] == {"type": "message", "n": 1, "event_id": f"{MM}:a"}


def test_on_sent_runs_only_after_the_reply_is_written(run: Any, short_dir: Path) -> None:
    """#99: a `Reply`'s `on_sent` runs once its frame is written, and never for a client that
    hung up before the reply was ready."""
    sent: list[str] = []
    release = asyncio.Event()

    async def slow(args: dict[str, Any]) -> Reply:
        await release.wait()
        return Reply({"n": args["n"]}, lambda: sent.append(str(args["n"])))

    server = DaemonServer({"subscribe": slow}, hangup_cancels=frozenset({"subscribe"}))
    sock = short_dir / "s.sock"

    def frame(n: int) -> bytes:
        return json.dumps({"proto": 1, "id": str(n), "op": "subscribe", "args": {"n": n}}).encode()

    async def scenario() -> dict[str, Any]:
        listener = await server.start(sock, group=None)
        _, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame(1) + b"\n")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()  # gone before the reply was ready
        await writer.wait_closed()
        await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(frame(2) + b"\n")
        await writer.drain()
        await asyncio.sleep(0.01)
        release.set()
        reply: dict[str, Any] = json.loads(await asyncio.wait_for(reader.readline(), 2))
        await asyncio.sleep(0.01)
        writer.close()
        listener.close()
        await listener.wait_closed()
        return reply

    reply = run(scenario())
    assert reply["result"] == {"n": 2}
    assert sent == ["2"]


async def _never(args: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("not called")


def test_cursor_replay_exactly_once(world: World, run: Any) -> None:
    """R-28: a disconnect after event N replays from N+1, with no duplicates, and so does a
    daemon restart, from the saved cursor."""

    async def scenario() -> tuple[list[str], list[tuple[str, str]], list[str]]:
        world.doorbell.attach(DEV, [MM])
        async with asyncio.timeout(3):
            while not (world.mm.open_sockets(world.me) and world.mattermost._agents[DEV].hello):
                await asyncio.sleep(0.001)
        delivered = []
        for n in (1, 2, 3):
            world.mm_post(f"event {n}")
            delivered.append(text_of(await world.next()))
        # The connection drops after event 3; two posts land while it is down.
        world.mm.fail_connects = 2
        await world.mm.open_sockets(world.me)[0].close()
        down = await world.next()
        world.mm_post("event 4", broadcast=False)
        p5 = world.mm_post("event 5", broadcast=False)
        up = await world.next()
        delivered += [text_of(await world.next()), text_of(await world.next())]
        # The server also resends event 5 live: not delivered twice.
        world.mm.broadcast_posted(p5)
        world.mm_post("event 6")
        delivered.append(text_of(await world.next()))
        await asyncio.sleep(0.05)
        assert world.doorbell.queue(DEV).empty()
        health = [kind(down), kind(up)]

        # A daemon restart: new adapters, a new doorbell, the cursors from disk.
        await world.close()
        world.mm_post("event 7", broadcast=False)
        world.build()
        world.doorbell.attach(DEV, [MM])
        after = [text_of(await world.next())]
        world.mm_post("event 8")
        after.append(text_of(await world.next()))
        await asyncio.sleep(0.05)
        assert world.doorbell.queue(DEV).empty()
        await world.close()
        return delivered, health, after

    delivered, health, after = run(scenario())
    assert delivered == [f"event {n}" for n in (1, 2, 3, 4, 5, 6)]
    assert health == [(MM, "backend.down"), (MM, "backend.up")]
    assert after == ["event 7", "event 8"]


def test_failed_replay_is_loud_and_loses_nothing(world: World, run: Any) -> None:
    """R-27, R-28, #99: a reconnect whose replay fails is a `backend.down` naming the failing
    call, yields nothing, and keeps the position, so the next replay delivers the gap."""

    async def scenario() -> tuple[list[DoorbellEvent], list[str]]:
        world.doorbell.attach(DEV, [MM])
        async with asyncio.timeout(3):
            while not (world.mm.open_sockets(world.me) and world.mattermost._agents[DEV].hello):
                await asyncio.sleep(0.001)
        world.mm_post("event 1")
        got = [text_of(await world.next())]
        # A daemon restart; a post lands meanwhile, and the first replay gets a 503.
        await world.close()
        world.mm_post("event 2", broadcast=False)
        world.mm.errors[f"/channels/{world.chan}/posts"] = 1
        world.build()
        world.doorbell.attach(DEV, [MM])
        health = [await world.next(), await world.next()]
        got.append(text_of(await world.next()))
        world.mm_post("event 3")
        got.append(text_of(await world.next()))
        await asyncio.sleep(0.05)
        assert world.doorbell.queue(DEV).empty()
        await world.close()
        return health, got

    health, got = run(scenario())
    assert [kind(e) for e in health] == [(MM, "backend.down"), (MM, "backend.up")]
    reason = str(health[0].payload["reason"])
    assert reason.startswith(f"replay failed: GET /channels/{world.chan}/posts")
    assert "503" in reason
    assert got == ["event 1", "event 2", "event 3"]


class Scripted:
    """A source that plays one script per (re)start: each item is a message ID to yield,
    `"down"`/`"up"` for a status report, or `"raise"`."""

    def __init__(self, *runs: list[str]) -> None:
        self.runs = list(runs)
        self.cursors: list[StreamCursor | None] = []

    async def stream(
        self,
        dev_name: str,
        *,
        cursor: StreamCursor | None = None,
        on_status: StatusSink = NO_STATUS,
    ) -> AsyncIterator[InboundMessage]:
        self.cursors.append(cursor)
        script = self.runs.pop(0) if self.runs else []
        for step in script:
            if step == "raise":
                raise ConnectionError("gone")
            if step in ("down", "up"):
                on_status(step == "up", "flap" if step == "down" else "")
                continue
            yield InboundMessage("x", "c", step, "", "u", "u", f"text {step}", ts=float(len(step)))
        await asyncio.Event().wait()  # then nothing more


def test_cursor_replay_dedups_an_overlap(tmp_path: Path, run: Any) -> None:
    """A replay that overlaps what was queued delivers each event once; the restarted
    stream is handed a cursor that covers every ID already queued."""
    src = Scripted(["p1", "p2", "p3", "raise"], ["up", "p2", "p3", "p4"])
    bell = Doorbell({"x": src}, CursorStore(tmp_path), sleep=fast_sleep)

    async def scenario() -> list[str]:
        bell.attach(DEV, ["x"])
        got = []
        for _ in range(6):
            e = await asyncio.wait_for(bell.next_event(DEV), 2)
            got.append(e.keys[0] if e.keys else str(e.payload["type"]))
        await asyncio.sleep(0.02)
        assert bell.queue(DEV).empty()
        await bell.aclose()
        return got

    assert run(scenario()) == ["p1", "p2", "p3", "backend.down", "backend.up", "p4"]
    assert src.cursors[0] is None
    restart = src.cursors[1]
    assert restart is not None and {"p1", "p2", "p3"} <= restart.seen


def test_backend_down_once_others_flow(world: World, run: Any) -> None:
    """R-27: one backend's outage enqueues exactly one `backend.down`, however many times it
    fails, while the other backends keep delivering; its recovery, one `backend.up`."""

    async def scenario() -> list[DoorbellEvent]:
        world.doorbell.attach(DEV, [MM, "discord", "squawkie"])
        await world.connected()
        # squawkie goes away: its stream ends, and every reconnect is refused.
        world.sq.refuse_streams = 10**9
        world.sq.drop_streams()
        got = [await world.next()]
        async with asyncio.timeout(3):
            while sum(1 for m, p in world.sq.requests if p == "/api/stream") < 10:
                await asyncio.sleep(0.001)
        world.mm_post("mattermost still flows")
        world.discord.add_message(DISCORD_CHANNEL, "discord still flows", author=HUMAN)
        got += [await world.next(), await world.next()]
        # It comes back.
        world.sq.refuse_streams = 0
        got.append(await world.next())
        # Killing Mattermost's connection: one down for it, and squawkie flows.
        world.mm.fail_connects = 5
        await world.mm.open_sockets(world.me)[0].close()
        got.append(await world.next())
        world.sq.update(world.squawk["id"], state="confirmed")
        got.append(await world.next())
        got.append(await world.next())  # Mattermost back
        await asyncio.sleep(0.05)
        while not world.doorbell.queue(DEV).empty():
            got.append(world.doorbell.queue(DEV).get_nowait())
        await world.close()
        return got

    got = run(scenario())
    kinds = [kind(e) for e in got]
    assert kinds[0] == ("squawkie", "backend.down")
    assert got[0].payload["reason"] in ("stream ended", "stream refused: HTTP 503")
    assert sorted(kinds[1:3]) == [("discord", "message"), (MM, "message")]
    assert kinds[3] == ("squawkie", "backend.up")
    assert kinds[4] == (MM, "backend.down")
    assert kinds[5] == ("squawkie", "squawk.updated")
    assert kinds[6] == (MM, "backend.up")
    for backend in (MM, "squawkie", "discord"):
        assert kinds.count((backend, "backend.down")) == (0 if backend == "discord" else 1)
    assert len(kinds) == 7


def test_status_is_once_per_outage(tmp_path: Path, run: Any) -> None:
    bell = Doorbell({}, CursorStore(None))

    async def scenario() -> list[dict[str, Any]]:
        bell.status(DEV, "x", True)  # up while never down: nothing
        for reason in ("a", "b", "c"):
            bell.status(DEV, "x", False, reason)
        assert bell.is_down(DEV, "x")
        bell.status(DEV, "y", False, "other")  # another backend's outage is its own
        bell.status(DEV, "x", True)
        bell.status(DEV, "x", True)
        q = bell.queue(DEV)
        return [q.get_nowait().payload for _ in range(q.qsize())]

    assert run(scenario()) == [
        {"type": "backend.down", "backend": "x", "reason": "a"},
        {"type": "backend.down", "backend": "y", "reason": "other"},
        {"type": "backend.up", "backend": "x"},
    ]


def test_filter_hook_and_full_queue(tmp_path: Path, run: Any) -> None:
    refused: list[str] = []

    def admit(dev: str, event: DoorbellEvent) -> bool:
        if event.keys == ("no",):
            refused.append(dev)
            return False
        return True

    bell = Doorbell({}, CursorStore(None), admit=admit, queue_max=2)

    async def scenario() -> int:
        assert await bell.offer(DEV, "x", DoorbellEvent("x", {"n": 0}, ("no",))) is False
        assert await bell.offer(DEV, "x", DoorbellEvent("x", {"n": 1}, ("a",))) is True
        assert await bell.offer(DEV, "x", DoorbellEvent("x", {"n": 1}, ("a",))) is False  # repeat
        await bell.offer(DEV, "x", DoorbellEvent("x", {"n": 2}, ("b",)))
        await bell.offer(DEV, "x", DoorbellEvent("x", {"n": 3}, ("c",)))  # dropped: full
        return bell.queue(DEV).qsize()

    assert run(scenario()) == 2
    assert refused == [DEV]


# -- the cursor store -----------------------------------------------------------------------
def test_cursor_store_files(tmp_path: Path) -> None:
    store = CursorStore(tmp_path, limit=3)
    assert store.load(DEV, MM) is None
    for n in range(5):
        store.advance(DEV, MM, DoorbellEvent(MM, {}, (f"p{n}",), float(n)))
    store.advance(DEV, MM, DoorbellEvent(MM, {"type": "backend.down"}))  # no keys: no move
    path = tmp_path / "cursors" / DEV / f"{MM}.json"
    assert json.loads(path.read_text()) == {"id": "p4", "ts": 4.0, "seen": ["p2", "p3", "p4"]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    again = CursorStore(tmp_path, limit=3).load(DEV, MM)
    assert again == StreamCursor("p4", 4.0, frozenset({"p2", "p3", "p4"}))


@pytest.mark.parametrize("content", ["not json", '{"id": 1}', '{"id":"a","ts":"x","seen":[]}'])
def test_cursor_store_corrupt_file_reads_as_empty(tmp_path: Path, content: str) -> None:
    path = tmp_path / "cursors" / DEV / "discord.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert CursorStore(tmp_path).load(DEV, "discord") is None


def test_cursor_store_refuses_unsafe_names(tmp_path: Path) -> None:
    store = CursorStore(tmp_path)
    assert store.path("../x", MM) is None
    assert store.path(DEV, "../x") is None
    assert store.path(DEV, ".hidden") is None
    store.advance("../x", MM, DoorbellEvent(MM, {}, ("p",)))
    assert not (tmp_path / "cursors").exists() or not any((tmp_path / "cursors").iterdir())
