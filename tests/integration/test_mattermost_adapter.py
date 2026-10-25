"""Story 2.1 against the CI leg's Mattermost: IT-14 and IT-02.

The module creates its own broker account and human partner, gives the broker the patched
role, and drives a real `crewbroker.service.Broker` in process. The daemon side is wired as
`crewchat serve` wires it: a `Registry`, a `TokenStore`, and a `MattermostBackend` that is the
store's adapter. Agents register through the registry, so their bot accounts and tokens are
the broker's own.

- IT-14 (R-04, R-29): an agent's websocket carries the server's own mention markers, and its
  own posts are dropped by user ID, whatever their text.
- IT-02 (R-05, R-07): across a daemon-driven rotation, the agent's websocket reconnects with
  the new token, the old token is revoked, and no event is lost.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from crewbroker import mint
from crewbroker.config import CrewConfig
from crewbroker.mattermost import MattermostClient as BrokerMattermost
from crewbroker.service import Broker, OpError
from crewchat.adapters.base import InboundMessage
from crewchat.adapters.mattermost import MattermostBackend
from crewchat.config import parse_config
from crewchat.filters import SwitchCache
from crewchat.registry import Registry
from crewchat.tokens import BrokerClient, BrokerError, Rotator, TokenStore

from tests.integration.mmhttp import mm_request

ROOT = Path(__file__).resolve().parents[2]
ROLE_PATCH = ROOT / "scripts" / "ci" / "broker-role-patch.sh"
SID = "ci"


def mkuser(mattermost: Any, username: str) -> str:
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
    return str(resp.json()["id"])


class InProcessBroker(BrokerClient):
    """The daemon's broker client, answered by an in-process `Broker` instead of its socket."""

    def __init__(self, broker: Broker) -> None:
        super().__init__("/nonexistent/broker.sock", 15.0, name="it-broker")
        self.broker = broker

    async def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self.broker.ops()[op], dict(args))
        except OpError as exc:
            raise BrokerError(op, str(exc)) from exc


@pytest.fixture(scope="module")
def setup(mattermost: Any) -> Iterator[dict[str, str]]:
    run = secrets.token_hex(3)
    broker_id = mkuser(mattermost, f"crewbroker-ad{run}")
    partner_id = mkuser(mattermost, f"partner-ad{run}")
    env = {**os.environ, "MM_URL": mattermost.url, "MM_ADMIN_TOKEN": mattermost.admin_token}
    subprocess.run([str(ROLE_PATCH), broker_id], env=env, check=True)
    resp = mm_request(mattermost, "POST", f"/users/{broker_id}/tokens", json={"description": "it"})
    assert resp.status_code == 200, resp.text
    team = mm_request(
        mattermost,
        "POST",
        "/teams",
        json={"name": f"crew-ad{run}", "display_name": f"crew ad{run}", "type": "O"},
    )
    assert team.status_code == 201, team.text
    team_id = team.json()["id"]
    channel = mm_request(
        mattermost,
        "POST",
        "/channels",
        json={"team_id": team_id, "name": f"crew-ad{run}", "display_name": "crew", "type": "O"},
    )
    assert channel.status_code == 201, channel.text
    me = mm_request(mattermost, "GET", "/users/me").json()
    yield {
        "crew": f"ad{run}",
        "broker_id": broker_id,
        "partner_id": partner_id,
        "broker_token": resp.json()["token"],
        "team_id": team_id,
        "channel_id": channel.json()["id"],
        "admin_id": me["id"],
    }
    # Not a configured broker: give the patched role back (IT-01 asserts on it, R-06).
    resp = mm_request(mattermost, "PUT", f"/users/{broker_id}/roles", json={"roles": "system_user"})
    assert resp.status_code == 200, resp.text


class Daemon:
    """The daemon's registry, store and Mattermost adapter, over the in-process broker."""

    def __init__(self, mattermost: Any, setup: dict[str, str], root: Path) -> None:
        mint.clear_expiry_cache()
        self.mattermost = mattermost
        self.setup = setup
        crew = setup["crew"]
        self.broker = Broker(
            CrewConfig(
                crew=crew,
                server_id=SID,
                server_url=mattermost.url,
                human_user_ids=[setup["partner_id"]],
                trusted=[],
                broker_user_id=setup["broker_id"],
            ),
            BrokerMattermost(mattermost.url, setup["broker_token"]),
            root / "broker",
        )
        config = parse_config(
            {
                "default_crew": crew,
                "mattermost": [{"id": SID, "url": mattermost.url}],
                "crews": {
                    crew: {
                        "needs_list": "bj:needs",
                        SID: {
                            "broker_socket": "/nonexistent/broker.sock",
                            "broker_user_id": setup["broker_id"],
                        },
                    }
                },
            }
        )
        self.brokers = {(crew, SID): InProcessBroker(self.broker)}
        self.store = TokenStore(root / "tokens", self.brokers)
        self.backend = MattermostBackend(
            SID, mattermost.url, self.store.token_for, refresher=self.store
        )
        self.store.adapters.append(self.backend)
        self.registry = Registry(config, self.brokers, self.store, SwitchCache(self.brokers))

    async def register(self, dev: str) -> str:
        """Register `dev` and seat its bot in the module's team and channel. Its user ID."""
        reg = await self.registry.register(dev, None, "it-session")
        found = mm_request(self.mattermost, "GET", f"/users/username/{reg.account}")
        user_id = str(found.json()["id"])
        resp = mm_request(
            self.mattermost,
            "POST",
            f"/teams/{self.setup['team_id']}/members",
            json={"team_id": self.setup["team_id"], "user_id": user_id},
        )
        assert resp.status_code == 201, resp.text
        resp = mm_request(
            self.mattermost,
            "POST",
            f"/channels/{self.setup['channel_id']}/members",
            json={"user_id": user_id},
        )
        assert resp.status_code == 201, resp.text
        return user_id


class Inbox:
    """One agent's stream, collected into a queue by a background task."""

    def __init__(self, backend: MattermostBackend, dev: str) -> None:
        self.backend = backend
        self.dev = dev
        self.queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.task = asyncio.create_task(self._pump(backend.stream(dev)))

    async def _pump(self, gen: AsyncIterator[InboundMessage]) -> None:
        async for msg in gen:
            await self.queue.put(msg)

    async def connected(self, token: str | None = None) -> None:
        """Wait until the websocket is open and has said `hello` (on `token`, if given)."""
        for _ in range(300):
            agent = self.backend._agents.get(self.dev)
            if (
                agent is not None
                and agent.ws is not None
                and agent.hello
                and (token is None or agent.ws_token == token)
            ):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"{self.dev}'s websocket never connected")

    async def until(self, text: str, timeout: float = 15) -> list[InboundMessage]:
        """Every message up to and including the first whose text is `text`."""
        got: list[InboundMessage] = []
        async with asyncio.timeout(timeout):
            while True:
                msg = await self.queue.get()
                got.append(msg)
                if msg.text == text:
                    return got

    async def close(self) -> None:
        await self.backend.close_agent(self.dev)
        await asyncio.wait_for(self.task, 10)


def as_admin_post(mattermost: Any, channel_id: str, message: str) -> dict[str, Any]:
    resp = mm_request(
        mattermost, "POST", "/posts", json={"channel_id": channel_id, "message": message}
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


def test_it14_server_mentions_and_echo_by_user_id(
    mattermost: Any, setup: dict[str, str], tmp_path: Path
) -> None:
    """IT-14 (R-04, R-29)."""
    channel = setup["channel_id"]
    crew = setup["crew"]

    async def scenario() -> None:
        d = Daemon(mattermost, setup, tmp_path)
        babel = await d.register("babel")
        kessel = await d.register("kessel")
        assert babel != kessel
        b_in, k_in = Inbox(d.backend, "babel"), Inbox(d.backend, "kessel")
        try:
            await b_in.connected()
            await k_in.connected()

            # R-04: babel's own post, signed as kessel, never reaches babel; kessel gets it.
            own = await d.backend.send("babel", channel, "own post — **kessel** (oaw)")
            assert own.proto == 1 and own.author_id == babel
            # A crewmate's post that carries babel's signature text does reach babel.
            mate = await d.backend.send("kessel", channel, "mate post — **babel** (oaw)")
            got = await b_in.until(mate.text)
            assert [m.post_id for m in got] == [mate.post_id]
            assert got[0].author_id == kessel and got[0].is_bot is True
            assert got[0].proto == 1
            k_got = await k_in.until(own.text)
            assert [m.post_id for m in k_got] == [own.post_id]

            # R-29: mentions are the server's markers. A real @mention is marked...
            marked = as_admin_post(mattermost, channel, f"@babel.{crew} over to you")
            got = await b_in.until(marked["message"])
            assert got[-1].mentions == [babel]
            assert got[-1].is_bot is False and got[-1].proto == 0  # a human's unmarked post
            # ...and text that names babel without the server marking it carries none.
            unmarked = as_admin_post(mattermost, channel, f"babel.{crew}, no at sign")
            got = await b_in.until(unmarked["message"])
            assert got[-1].mentions == []
        finally:
            await b_in.close()
            await k_in.close()
            await d.backend.aclose()

    asyncio.run(scenario())


def test_it02_rotation_reconnects_websocket_and_loses_no_event(
    mattermost: Any, setup: dict[str, str], tmp_path: Path
) -> None:
    """IT-02 (R-05, R-07): a daemon-driven rotation swaps the token, reconnects the websocket
    on it, then revokes the old token; events before, during and after it all arrive."""
    channel = setup["channel_id"]

    async def scenario() -> None:
        d = Daemon(mattermost, setup, tmp_path)
        await d.register("rotor")
        await d.register("talker")
        inbox = Inbox(d.backend, "rotor")
        try:
            await inbox.connected()
            agent = d.backend._agents["rotor"]
            old_token = d.store.token_for("rotor", SID)
            assert agent.ws_token == old_token and agent.connects == 1

            before = await d.backend.send("talker", channel, "before rotation")
            await inbox.until(before.text)

            # A post lands while the rotation is under way.
            rotation = asyncio.create_task(Rotator(d.store).run_daily(datetime.now(UTC)))
            during = await d.backend.send("talker", channel, "during rotation")
            report = await rotation
            assert ("rotor", SID) in report.rotated and report.failed == []

            new_token = d.store.token_for("rotor", SID)
            assert new_token is not None and new_token != old_token
            await inbox.connected(new_token)
            assert agent.ws_token == new_token and agent.connects == 2
            # The old token is revoked; the websocket on it would have gone deaf (§4.8).
            gone = mm_request(mattermost, "GET", "/users/me", token=old_token)
            assert gone.status_code == 401

            after = await d.backend.send("talker", channel, "after rotation")
            got = await inbox.until(after.text)
            seen = {m.post_id for m in got}
            assert after.post_id in seen
            if during.post_id not in seen:
                # The server did not resume the connection: the gap is replayed from the
                # last event seen, as the doorbell's cursor does (R-28).
                replay = await d.backend.read("rotor", channel, since=before.ts)
                seen |= {m.post_id for m in replay}
            assert during.post_id in seen
        finally:
            await inbox.close()
            await d.backend.aclose()

    asyncio.run(scenario())
