"""Story 3.3 against the CI leg's Mattermost: IT-08, IT-09, IT-10 and IT-23 (R-20 to R-25, R-34).

The daemon side is wired as `crewchat serve` wires it: a `Registry`, `TokenStore` and
`MattermostBackend` over two in-process `crewbroker.service.Broker`s (our crew's, and a
trusted crew's, each with its own broker account and patched role), the `SwitchCache` and
`Filters` reading both, and a `Doorbell` whose hook is `Filters.admit_event` with a
`KioskQueue` for `KIOSK` verdicts. The pull path is `read` and `thread` through
`build_chat_handlers` with the same filters.

A message the filters drop leaves no trace, so each "dropped" check is followed by a crewmate's
sentinel post addressed to the agent: the next event on its queue must be that sentinel.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import secrets
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from crewbroker import mint
from crewbroker.config import CrewConfig
from crewbroker.mattermost import MattermostClient as BrokerMattermost
from crewbroker.service import Broker
from crewbroker.switch import REFUSAL
from crewchat.adapters.base import InboundMessage
from crewchat.adapters.mattermost import MattermostBackend
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.config import parse_config
from crewchat.doorbell import CursorStore, Doorbell, DoorbellEvent
from crewchat.filters import DELIVER, Filters, Kind, KioskQueue, SwitchCache
from crewchat.registry import Registry
from crewchat.threads import MyThreads
from crewchat.tokens import BrokerUnavailable, TokenStore

from tests.integration.mmhttp import mm_request
from tests.integration.test_mattermost_adapter import ROLE_PATCH, SID, InProcessBroker

MM = f"mattermost:{SID}"
EVENT_TIMEOUT_S = 20.0


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class Switchable(InProcessBroker):
    """An in-process broker the test can stop: while `down`, every call is unavailable."""

    def __init__(self, broker: Broker) -> None:
        super().__init__(broker)
        self.down = False

    async def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.down:
            raise BrokerUnavailable(f"{self.name}: stopped")
        return await super().call(op, args)


def login_user(mattermost: Any, username: str) -> tuple[str, str]:
    """A new user, logged in: (user id, session token)."""
    password = f"It-{secrets.token_hex(8)}-Aa1!"
    resp = mm_request(
        mattermost,
        "POST",
        "/users",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    assert resp.status_code == 201, resp.text
    login = mm_request(
        mattermost, "POST", "/users/login", json={"login_id": username, "password": password}
    )
    assert login.status_code == 200, login.text
    return str(resp.json()["id"]), login.headers["Token"]


def seat(mattermost: Any, team_id: str, channel_id: str, user_id: str) -> None:
    resp = mm_request(
        mattermost,
        "POST",
        f"/teams/{team_id}/members",
        json={"team_id": team_id, "user_id": user_id},
    )
    assert resp.status_code == 201, resp.text
    resp = mm_request(
        mattermost, "POST", f"/channels/{channel_id}/members", json={"user_id": user_id}
    )
    assert resp.status_code == 201, resp.text


def broker_account(mattermost: Any, username: str) -> tuple[str, str]:
    """A broker account with the patched role and a personal access token."""
    resp = mm_request(
        mattermost,
        "POST",
        "/users",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": f"It-{secrets.token_hex(8)}-Aa1!",
        },
    )
    assert resp.status_code == 201, resp.text
    user_id = str(resp.json()["id"])
    env = {**os.environ, "MM_URL": mattermost.url, "MM_ADMIN_TOKEN": mattermost.admin_token}
    subprocess.run([str(ROLE_PATCH), user_id], env=env, check=True)
    tok = mm_request(mattermost, "POST", f"/users/{user_id}/tokens", json={"description": "it"})
    assert tok.status_code == 200, tok.text
    return user_id, str(tok.json()["token"])


def foreign_bot(mattermost: Any, username: str, team_id: str, channel_id: str) -> tuple[str, str]:
    """A bot account the admin owns, seated in the channel, with a token: (user id, token)."""
    resp = mm_request(
        mattermost, "POST", "/bots", json={"username": username, "display_name": username}
    )
    assert resp.status_code == 201, resp.text
    bot_id = str(resp.json()["user_id"])
    seat(mattermost, team_id, channel_id, bot_id)
    tok = mm_request(mattermost, "POST", f"/users/{bot_id}/tokens", json={"description": "it"})
    assert tok.status_code == 200, tok.text
    return bot_id, str(tok.json()["token"])


@pytest.fixture(scope="module")
def world(mattermost: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    run = secrets.token_hex(3)
    crew, ally = f"fl{run}", f"al{run}"
    brokers = {
        crew: broker_account(mattermost, f"crewbroker-fl{run}"),
        ally: broker_account(mattermost, f"crewbroker-al{run}"),
    }
    team = mm_request(
        mattermost,
        "POST",
        "/teams",
        json={"name": f"crew-fl{run}", "display_name": f"crew fl{run}", "type": "O"},
    )
    assert team.status_code == 201, team.text
    team_id = team.json()["id"]
    channel = mm_request(
        mattermost,
        "POST",
        "/channels",
        json={"team_id": team_id, "name": f"crew-fl{run}", "display_name": "crew", "type": "O"},
    )
    assert channel.status_code == 201, channel.text
    channel_id = channel.json()["id"]
    people = {
        name: login_user(mattermost, f"{name}-fl{run}")
        for name in ("partner", "outsider", "allyhuman")
    }
    for user_id, _token in people.values():
        seat(mattermost, team_id, channel_id, user_id)
    yield {
        # The brokers' state outlives each test's daemon, as a real broker's does: a bot
        # it minted in one test is still on its `minted_bot_ids` in the next.
        "state_dir": tmp_path_factory.mktemp("filters-brokers"),
        "run": run,
        "crew": crew,
        "ally": ally,
        "brokers": brokers,
        "team_id": team_id,
        "channel_id": channel_id,
        "people": people,
    }
    # Not configured brokers: give the patched role back (IT-01 asserts on it, R-06).
    for user_id, _token in brokers.values():
        resp = mm_request(
            mattermost, "PUT", f"/users/{user_id}/roles", json={"roles": "system_user"}
        )
        assert resp.status_code == 200, resp.text


class Rig:
    """The daemon, wired as `serve()` wires it, for one test."""

    def __init__(
        self,
        mattermost: Any,
        world: dict[str, Any],
        root: Path,
        *,
        loop_guard_window: int = 20,
        kiosk: bool = True,
    ) -> None:
        mint.clear_expiry_cache()
        self.mattermost = mattermost
        self.world = world
        self.clock = Clock()
        crew, ally = world["crew"], world["ally"]
        self.crew, self.ally = crew, ally
        self.channel = world["channel_id"]
        people = world["people"]
        self.partner_id, self.partner_token = people["partner"]
        self.outsider_id, self.outsider_token = people["outsider"]
        self.ally_human_id, self.ally_human_token = people["allyhuman"]
        humans = {crew: [self.partner_id], ally: [self.ally_human_id]}
        trusted = {crew: [ally], ally: []}
        self.raw_brokers = {
            name: Broker(
                CrewConfig(
                    crew=name,
                    server_id=SID,
                    server_url=mattermost.url,
                    human_user_ids=humans[name],
                    trusted=trusted[name],
                    broker_user_id=world["brokers"][name][0],
                ),
                BrokerMattermost(mattermost.url, world["brokers"][name][1]),
                world["state_dir"] / f"broker-{name}",
                clock=self.clock,
            )
            for name in (crew, ally)
        }
        # Each test starts from the defaults, whatever an earlier one left (R-20, R-34).
        self.flip("crew")
        self.flip("kiosk on")
        config = parse_config(
            {
                "default_crew": crew,
                # The kiosk is off on a server unless `servers.toml` turns it on (#117).
                "mattermost": [{"id": SID, "url": mattermost.url, "kiosk": kiosk}],
                "loop_guard_window": loop_guard_window,
                # The unit tests hold the 2 s floor; here a test's back-to-back cache misses
                # must each get their own round.
                "refetch_min_interval_s": 0.01,
                "crews": {
                    name: {
                        "needs_list": "bj:needs",
                        SID: {
                            "broker_socket": f"/nonexistent/{name}.sock",
                            "broker_user_id": world["brokers"][name][0],
                        },
                    }
                    for name in (crew, ally)
                },
            }
        )
        self.config = config
        self.brokers = {(name, SID): Switchable(b) for name, b in self.raw_brokers.items()}
        self.store = TokenStore(root / "tokens", self.brokers)
        self.backend = MattermostBackend(
            SID, mattermost.url, self.store.token_for, refresher=self.store
        )
        self.store.adapters.append(self.backend)
        self.cache = SwitchCache.from_config(self.brokers, config)
        self.registry = Registry(config, self.brokers, self.store, self.cache)
        self.threads = MyThreads()
        self.filters = Filters.from_config(
            self.registry, self.cache, self.threads, config, clock=self.clock
        )
        self.kiosk = KioskQueue()
        self.doorbell = Doorbell(
            {MM: self.backend}, CursorStore(None), admit=self.filters.admit_event, kiosk=self.kiosk
        )
        self.chat = build_chat_handlers(
            self.registry,
            {MM: self.backend},
            self.threads,
            AgentContext(),
            filters=self.filters,
        )
        self.ids: dict[str, str] = {}
        self.sentinels = 0

    # -- setup ------------------------------------------------------------------------------
    async def register(self, dev: str, crew: str | None = None, registry: Any = None) -> str:
        reg = await (registry or self.registry).register(dev, crew or self.crew, "it-session")
        found = mm_request(self.mattermost, "GET", f"/users/username/{reg.account}")
        user_id = str(found.json()["id"])
        seat(self.mattermost, self.world["team_id"], self.channel, user_id)
        self.ids[dev] = user_id
        return user_id

    async def listen(self, dev: str = "babel") -> None:
        """Attach the doorbell to `dev`'s Mattermost stream from now, and wait until it is
        open. Without a cursor, a first connect replays the last minute (`REPLAY_MARGIN_S`),
        which would hand this test an earlier test's posts."""
        self.doorbell.cursors.advance(dev, MM, DoorbellEvent(MM, {}, ("it-start",), time.time()))
        self.doorbell.attach(dev, [MM])
        for _ in range(300):
            agent = self.backend._agents.get(dev)
            if agent is not None and agent.ws is not None and agent.hello:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"{dev}'s websocket never connected")

    async def aclose(self) -> None:
        await self.doorbell.aclose()
        await self.backend.aclose()

    def flip(self, text: str, sender: str | None = None) -> str:
        """A switch command to our broker, as if DMed (IT-18 covers the DM itself)."""
        return self.raw_brokers[self.crew].handle_dm(sender or self.partner_id, text)

    # -- posting ----------------------------------------------------------------------------
    def post_as(self, token: str, text: str, *, root: str = "", channel: str = "") -> str:
        body: dict[str, Any] = {"channel_id": channel or self.channel, "message": text}
        if root:
            body["root_id"] = root
        resp = mm_request(self.mattermost, "POST", "/posts", token=token, json=body)
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    def dm_channel(self, token: str, user_id: str, other_id: str) -> str:
        resp = mm_request(
            self.mattermost, "POST", "/channels/direct", token=token, json=[user_id, other_id]
        )
        assert resp.status_code in (200, 201), resp.text
        return str(resp.json()["id"])

    async def send(self, dev: str, text: str, thread: str | None = None) -> InboundMessage:
        return await self.backend.send(dev, self.channel, text, thread)

    # -- observing --------------------------------------------------------------------------
    async def next_event(self, dev: str = "babel") -> dict[str, Any]:
        event = await asyncio.wait_for(self.doorbell.next_event(dev), EVENT_TIMEOUT_S)
        return event.payload

    async def delivered(self, post_id: str, dev: str = "babel") -> dict[str, Any]:
        payload = await self.next_event(dev)
        assert payload.get("message", {}).get("post_id") == post_id, payload
        return payload

    async def dropped(self, label: str, dev: str = "babel") -> None:
        """Everything before now was dropped: the next event is a fresh crewmate sentinel."""
        self.sentinels += 1
        sentinel = await self.send("kessel", f"@{dev} sentinel {self.sentinels} after {label}")
        payload = await self.next_event(dev)
        got = payload.get("message", {})
        assert got.get("post_id") == sentinel.post_id, (label, got.get("text"))

    def kiosked(self, dev: str = "babel") -> list[DoorbellEvent]:
        q = self.kiosk.queue(dev)
        out: list[DoorbellEvent] = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    async def read(self, dev: str = "babel", **args: Any) -> list[dict[str, Any]]:
        result = await self.chat["read"](
            {"dev_name": dev, "backend": MM, "channel": self.channel, "limit": 200, **args}
        )
        messages: list[dict[str, Any]] = result["messages"]
        return messages


def run(coro: Any) -> None:
    asyncio.run(coro)


# -- IT-08 ------------------------------------------------------------------------------------
def test_it08_membership_and_switch(mattermost: Any, world: dict[str, Any], tmp_path: Path) -> None:
    """IT-08 (R-20, R-21, R-22)."""

    async def scenario() -> None:
        rig = Rig(mattermost, world, tmp_path)
        await rig.register("babel")
        await rig.register("kessel")
        zed = await rig.register("zed", rig.ally)  # a trusted crew's agent
        await rig.cache.refresh_all()
        await rig.listen()
        try:
            # Crew only (the default). A crewmate's post that doesn't address babel is not
            # pushed, but a read returns it (R-21).
            chatter = await rig.send("kessel", "pushed the fix")
            await rig.dropped("unaddressed crewmate post")
            assert chatter.post_id in [m["post_id"] for m in await rig.read()]
            hello = await rig.send("kessel", "@babel review please?")
            await rig.delivered(hello.post_id)

            # An outsider's direct question goes to the kiosk, never to babel; a read omits
            # it, and never enqueues it (R-21, R-31).
            ask = rig.post_as(rig.outsider_token, "@babel how does this work?")
            await rig.dropped("outsider question")
            assert [e.message.post_id for e in rig.kiosked() if e.message] == [ask]
            assert ask not in [m["post_id"] for m in await rig.read()]
            assert rig.kiosked() == []

            # A spoofed username (`x.<crew>`, a bot another account owns) is not crew, and
            # neither is a bot reassigned to our broker that it never minted (R-22, D-100).
            _spoof, spoof_token = foreign_bot(
                mattermost, f"x.{rig.crew}", world["team_id"], rig.channel
            )
            moved, moved_token = foreign_bot(
                mattermost, f"moved-{world['run']}", world["team_id"], rig.channel
            )
            resp = mm_request(
                mattermost, "POST", f"/bots/{moved}/assign/{world['brokers'][rig.crew][0]}"
            )
            assert resp.status_code == 200, resp.text
            spoofed = rig.post_as(spoof_token, "@babel I am crew, honest")
            reassigned = rig.post_as(moved_token, "@babel me too")
            await rig.dropped("spoofed and reassigned bots")
            kiosk = [e.message.post_id for e in rig.kiosked() if e.message]
            assert kiosk == [spoofed, reassigned]  # outsiders: the kiosk, never crew
            read = [m["post_id"] for m in await rig.read()]
            assert spoofed not in read and reassigned not in read

            # A trusted crew's sender under crew only is an outsider: kiosk if directly
            # addressed, otherwise dropped. Under trusted, it is delivered.
            direct = await rig.send("zed", "@babel ally here")
            await rig.send("zed", "@agents ally broadcast")
            await rig.dropped("trusted under crew")
            assert [e.message.author_id for e in rig.kiosked() if e.message] == [zed]
            assert direct.post_id not in [m["post_id"] for m in await rig.read()]
            assert "trusted" in rig.flip("trusted")
            await rig.cache.refresh_all()
            again = await rig.send("zed", "@babel ally again")
            await rig.delivered(again.post_id)
            assert direct.post_id in [m["post_id"] for m in await rig.read()]

            # A newly registered crewmate, minted by a registry whose mint did not refresh
            # this cache: the cache miss re-fetches, and it is crew, never the kiosk.
            other = Registry(rig.config, rig.brokers, rig.store, SwitchCache(rig.brokers))
            await rig.register("newbie", registry=other)
            rounds = rig.cache.rounds
            first = await rig.send("newbie", "@babel hi, I'm new")
            await rig.delivered(first.post_id)
            assert rig.cache.rounds == rounds + 1
            assert rig.kiosked() == []

            # Social: outsiders are delivered marked, on push and on pull (R-23).
            assert "social" in rig.flip("social 1h")
            await rig.cache.refresh_all()
            marked = rig.post_as(rig.outsider_token, "@babel quick one")
            payload = await rig.delivered(marked)
            assert payload["outsider"] is True and payload["message"]["outsider"] is True
            outsider_reads = [m for m in await rig.read() if m["post_id"] == marked]
            assert outsider_reads and outsider_reads[0]["outsider"] is True
            assert rig.flip("crew")

            # With no cached crew_info, Mattermost is dropped; Discord never needs it.
            cold = Filters(rig.registry, SwitchCache(rig.brokers), rig.threads)
            reg = rig.registry.get("babel")
            assert reg is not None
            post = InboundMessage(
                MM, rig.channel, hello.post_id, "", rig.ids["kessel"], "kessel", "@babel x"
            )
            verdict = await cold.admit(post, reg, rig.clock(), "push")
            assert verdict.kind == Kind.DROP and verdict.reason == "no_cache"
            discord = dataclasses.replace(post, backend="discord", is_bot=False)
            assert await cold.admit(discord, reg, rig.clock(), "push") == DELIVER
        finally:
            await rig.aclose()

    run(scenario())


# -- IT-09 ------------------------------------------------------------------------------------
def test_it09_social_lapses_per_delivery(
    mattermost: Any, world: dict[str, Any], tmp_path: Path
) -> None:
    """IT-09 (R-23, R-24): social lapses on delivery after expiry, even with the broker
    stopped; the partner is told; only the partner's user ID can flip."""

    async def scenario() -> None:
        rig = Rig(mattermost, world, tmp_path)
        await rig.register("babel")
        await rig.register("kessel")
        await rig.cache.refresh_all()
        await rig.listen()
        try:
            # Only the partner flips (R-24): the outsider is refused, nothing changes.
            assert rig.flip("social 1h", sender=rig.outsider_id) == REFUSAL
            await rig.cache.refresh_all()
            state = rig.cache.switch_state(rig.crew, SID)
            assert state is not None and state.mode == "crew"

            assert "social" in rig.flip("social 1h")
            await rig.cache.refresh_all()
            first = rig.post_as(rig.outsider_token, "@babel during social")
            assert (await rig.delivered(first))["outsider"] is True

            # The broker stops. The cached social is past its expiry: the next delivery uses
            # the reverted mode, so a dead broker can't hold social open (R-23).
            for b in rig.brokers.values():
                b.down = True
            rig.clock.now += timedelta(hours=1, seconds=1)
            late = rig.post_as(rig.outsider_token, "@babel after the expiry")
            await rig.dropped("lapsed social, broker down")
            assert [e.message.post_id for e in rig.kiosked() if e.message] == [late]

            # The broker is back: its next switch_state settles the lapse and tells the
            # partner (R-23).
            for b in rig.brokers.values():
                b.down = False
            await rig.cache.refresh_all()
            state = rig.cache.switch_state(rig.crew, SID)
            assert state is not None and state.mode == "crew" and state.expires_at is None
            broker_id = world["brokers"][rig.crew][0]
            dm = rig.dm_channel(rig.partner_token, rig.partner_id, broker_id)
            posts = mm_request(
                mattermost, "GET", f"/channels/{dm}/posts", token=rig.partner_token
            ).json()["posts"]
            assert any("social lapsed" in p["message"] for p in posts.values())
        finally:
            await rig.aclose()

    run(scenario())


# -- IT-10 ------------------------------------------------------------------------------------
def test_it10_loop_guard(mattermost: Any, world: dict[str, Any], tmp_path: Path) -> None:
    """IT-10 (R-25)."""

    async def scenario() -> None:
        rig = Rig(mattermost, world, tmp_path, loop_guard_window=3)
        babel = await rig.register("babel")
        await rig.register("kessel")
        await rig.cache.refresh_all()
        await rig.listen()
        try:
            # A bot's broadcast in a conversation babel never joined: no opt-in.
            await rig.send("kessel", "@agents roll call")
            await rig.dropped("broadcast, not opted in")

            # babel's own thread is opted in; a bot still needs a mention there.
            root = await rig.send("babel", "babel starts a thread")
            rig.threads.record("babel", root)
            await rig.send("kessel", "no mention here", thread=root.post_id)
            await rig.dropped("no mention in babel's thread")
            mention = await rig.send("kessel", "@agents in babel's thread", thread=root.post_id)
            await rig.delivered(mention.post_id)

            # Mentioned into: a human names babel in kessel's thread, and the thread is in.
            other = await rig.send("kessel", "kessel's own thread")
            await rig.dropped("unaddressed root")
            rig.post_as(rig.partner_token, f"@babel.{rig.crew} look", root=other.post_id)
            await rig.next_event()
            crew_token = await rig.send("kessel", f"@{rig.crew} thoughts?", thread=other.post_id)
            await rig.delivered(crew_token.post_id)

            # A DM is opted in.
            dm = rig.dm_channel(rig.partner_token, rig.partner_id, babel)
            kessel_dm = await rig.backend.dm("kessel", babel, "@babel in a DM")
            await rig.delivered(kessel_dm.post_id)

            # The window: past `loop_guard_window` (3 here) bot messages in the
            # conversation, dropped. babel's thread already holds two.
            third = await rig.send("kessel", "@babel three", thread=root.post_id)
            await rig.delivered(third.post_id)
            await rig.send("kessel", "@babel four", thread=root.post_id)
            await rig.dropped("past the window")

            # Humans: addressed is enough, no mention needed.
            first_dm = rig.post_as(rig.partner_token, "are you there?", channel=dm)
            await rig.delivered(first_dm)
            reply = rig.post_as(rig.partner_token, "looks good", root=root.post_id)
            await rig.delivered(reply)
        finally:
            await rig.aclose()

    run(scenario())


# -- IT-23 ------------------------------------------------------------------------------------
def test_it23_kiosk_off_drops_outsiders(
    mattermost: Any, world: dict[str, Any], tmp_path: Path
) -> None:
    """IT-23 (R-34)."""

    async def scenario() -> None:
        rig = Rig(mattermost, world, tmp_path)
        await rig.register("babel")
        await rig.register("kessel")
        assert "kiosk off" in rig.flip("kiosk off")
        await rig.cache.refresh_all()
        await rig.listen()
        try:
            rig.post_as(rig.outsider_token, "@babel anyone home?")
            await rig.dropped("kiosk off")
            assert rig.kiosked() == []
            assert "kiosk on" in rig.flip("kiosk on")
            await rig.cache.refresh_all()
            ask = rig.post_as(rig.outsider_token, "@babel and now?")
            await rig.dropped("kiosk on")
            assert [e.message.post_id for e in rig.kiosked() if e.message] == [ask]
        finally:
            rig.flip("kiosk on")
            await rig.aclose()

    run(scenario())


def test_kiosk_server_flag_over_the_switch(
    mattermost: Any, world: dict[str, Any], tmp_path: Path
) -> None:
    """#117: with `kiosk = false` on the server, an outsider's direct address is never
    kiosked, though the broker's switch has the kiosk on."""

    async def scenario() -> None:
        rig = Rig(mattermost, world, tmp_path, kiosk=False)
        await rig.register("babel")
        await rig.register("kessel")  # the sentinel's sender
        assert "kiosk on" in rig.flip("kiosk on")
        await rig.cache.refresh_all()
        await rig.listen()
        try:
            rig.post_as(rig.outsider_token, "@babel is the kiosk here?")
            await rig.dropped("kiosk server off")
            assert rig.kiosked() == []
        finally:
            await rig.aclose()

    run(scenario())
