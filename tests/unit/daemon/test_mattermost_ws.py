"""Story 2.1: the Mattermost websocket: pings, backoff, reconnect and token rotation (CT-07, §4.8).

`FakeMattermost.connect` (conftest) stands in for the server's `/api/v4/websocket`.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import websockets.asyncio.client
from crewchat.adapters import mattermost as mm_adapter
from crewchat.adapters.base import InboundMessage, StreamCursor
from crewchat.adapters.mattermost import (
    BACKOFF_MAX_S,
    PING_INTERVAL_S,
    MattermostBackend,
    backoff_delays,
)
from crewchat.tokens import BrokerError, Unauthorized

SID = "dev"
DEV = "babel.oaw"


class Tokens:
    def __init__(self, mm: Any) -> None:
        self.mm = mm
        self.held: dict[str, str] = {}
        self.users: dict[str, str] = {}
        self.refreshes = 0

    def add(self, dev: str) -> str:
        user_id = str(self.mm.add_user(dev, is_bot=True))
        self.users[dev] = user_id
        self.held[dev] = self.mm.add_token(user_id)
        return user_id

    def rotate(self, dev: str) -> str:
        """Remint: a new token for the same user. The old one stays valid until revoked."""
        self.held[dev] = self.mm.add_token(self.users[dev])
        return self.held[dev]

    def token_for(self, dev: str, server_id: str) -> str | None:
        return self.held.get(dev)

    async def refresh(self, dev: str, server_id: str, failed_token: str) -> None:
        self.refreshes += 1
        self.rotate(dev)

    async def call_with_refresh(self, dev: str, server_id: str, request: Any) -> Any:
        return await request(self.held[dev])


def make(mm: Any, tokens: Tokens, sleep: Any) -> MattermostBackend:
    return MattermostBackend(
        SID,
        mm.URL,
        tokens.token_for,
        refresher=tokens,
        http_client=mm.http_client(),
        connect=mm.connect,
        sleep=sleep,
        max_retries=0,
    )


async def until(cond: Callable[[], Any]) -> None:
    for _ in range(2000):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


async def recv(gen: AsyncIterator[InboundMessage]) -> InboundMessage:
    return await asyncio.wait_for(anext(gen), 2)


def test_backoff_delays_capped_at_60s() -> None:
    assert list(itertools.islice(backoff_delays(), 9)) == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert BACKOFF_MAX_S == 60


def test_ping_interval_and_backoff(mm: Any, recording_sleep: Any, run: Any) -> None:
    """CT-07: every connect pings every 30 s; failed connects back off 1, 2, 4 ... 60 s; a
    connection that reached `hello` starts the next backoff at 1 s again."""
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    sender = mm.add_user("bj")
    mm.fail_connects = 8
    b = make(mm, tokens, recording_sleep)

    async def scenario() -> list[float]:
        gen = b.stream(DEV)
        first = asyncio.ensure_future(recv(gen))
        await until(lambda: mm.open_sockets(me))
        mm.broadcast_posted(mm.add_post(sender, "chan", "one"))
        assert (await first).text == "one"
        before = list(recording_sleep.waits)
        # The server drops the connection: the next attempt waits 1 s, not 60.
        mm.fail_connects = 1
        await mm.open_sockets(me)[0].close()
        second = asyncio.ensure_future(recv(gen))
        await until(lambda: mm.open_sockets(me))
        mm.broadcast_posted(mm.add_post(sender, "chan", "two"))
        assert (await second).text == "two"
        await b.close_agent(DEV)
        return [w for w in recording_sleep.waits[len(before) :]]

    after = run(scenario())
    assert recording_sleep.waits[:8] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert after == [1, 2]
    assert PING_INTERVAL_S == 30
    assert {c["ping_interval"] for c in mm.connects} == {30}


def test_reconnect_after_forced_close(mm: Any, recording_sleep: Any, run: Any) -> None:
    """CT-07: a connection closed under the stream is reopened, resuming by connection ID and
    sequence number, and events keep arriving on the new one."""
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    sender = mm.add_user("bj")
    b = make(mm, tokens, recording_sleep)

    async def scenario() -> list[str]:
        gen = b.stream(DEV)
        got = []
        first = asyncio.ensure_future(recv(gen))
        await until(lambda: mm.open_sockets(me))
        mm.broadcast_posted(mm.add_post(sender, "chan", "before"))
        got.append((await first).text)
        old = mm.open_sockets(me)[0]
        await old.close()  # forced close
        nxt = asyncio.ensure_future(recv(gen))
        await until(lambda: mm.open_sockets(me))
        assert mm.open_sockets(me)[0] is not old
        mm.broadcast_posted(mm.add_post(sender, "chan", "after"))
        got.append((await nxt).text)
        await b.close_agent(DEV)
        return got

    assert run(scenario()) == ["before", "after"]
    assert len(mm.connects) == 2
    first_uri, second_uri = (urlparse(c["uri"]) for c in mm.connects)
    assert first_uri.scheme == "ws" and first_uri.path == "/api/v4/websocket"
    assert first_uri.query == ""
    resume = parse_qs(second_uri.query)
    assert resume == {"connection_id": ["conn-1"], "sequence_number": ["2"]}
    assert recording_sleep.waits == [1]


def test_token_rotation_reconnects_with_new_token(mm: Any, recording_sleep: Any, run: Any) -> None:
    """§4.8: a new token closes the open websocket, which reopens at once on the new token."""
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    sender = mm.add_user("bj")
    b = make(mm, tokens, recording_sleep)

    async def scenario() -> list[str]:
        gen = b.stream(DEV)
        first = asyncio.ensure_future(recv(gen))
        await until(lambda: mm.open_sockets(me))
        old_ws = mm.open_sockets(me)[0]
        # Another server's rotation (same token here) leaves the websocket alone.
        await b.on_token_rotated(DEV)
        assert not old_ws.closed
        new = tokens.rotate(DEV)
        await b.on_token_rotated(DEV)
        assert old_ws.closed
        await until(lambda: mm.open_sockets(me))
        assert mm.open_sockets(me)[0].token == new
        mm.broadcast_posted(mm.add_post(sender, "chan", "after rotation"))
        got = [(await first).text]
        await b.close_agent(DEV)
        return got

    assert run(scenario()) == ["after rotation"]
    assert [c["token"] for c in mm.connects] == [
        mm.connects[0]["token"],
        tokens.held[DEV],
    ]
    assert recording_sleep.waits == []  # a rotation reconnects without backing off


def test_websocket_401_refreshes_the_token(mm: Any, recording_sleep: Any, run: Any) -> None:
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    b = make(mm, tokens, recording_sleep)

    async def scenario() -> None:
        await b.user_id(DEV)
        mm.tokens.pop(tokens.held[DEV])  # expired on the server
        gen = b.stream(DEV)
        task = asyncio.ensure_future(recv(gen))
        await until(lambda: mm.open_sockets(me))
        await b.close_agent(DEV)
        with pytest.raises(StopAsyncIteration):
            await task

    run(scenario())
    assert tokens.refreshes == 1
    assert mm.open_sockets(me) == []


def test_close_agent_ends_the_stream(mm: Any, recording_sleep: Any, run: Any) -> None:
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    b = make(mm, tokens, recording_sleep)

    async def scenario() -> None:
        gen = b.stream(DEV)
        task = asyncio.ensure_future(recv(gen))
        await until(lambda: mm.open_sockets(me))
        await b.close_agent(DEV)
        with pytest.raises(StopAsyncIteration):
            await task
        await b.aclose()

    run(scenario())
    assert len(mm.connects) == 1


def test_close_server_ignores_another_server(mm: Any, recording_sleep: Any, run: Any) -> None:
    """#155: a retire on another server leaves this backend's stream open; its own ends it."""
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    b = make(mm, tokens, recording_sleep)

    async def scenario() -> None:
        gen = b.stream(DEV)
        task = asyncio.ensure_future(recv(gen))
        await until(lambda: mm.open_sockets(me))
        await b.close_server(DEV, "other")
        await asyncio.sleep(0.01)
        assert mm.open_sockets(me) and not task.done()
        await b.close_server(DEV, SID)
        with pytest.raises(StopAsyncIteration):
            await task
        await b.aclose()

    run(scenario())
    assert len(mm.connects) == 1


class Deactivating(Tokens):
    """The store's side of a `deactivated` remint: a 401 retires the token on this server,
    which closes the backend's stream there (#127, #155), and the refresh fails."""

    backend: MattermostBackend

    async def call_with_refresh(self, dev: str, server_id: str, request: Any) -> Any:
        try:
            return await request(self.held[dev])
        except Unauthorized:
            self.refreshes += 1
            await self.backend.close_server(dev, server_id)
            raise BrokerError("remint", f"deactivated: {dev} was deactivated") from None

    async def refresh(self, dev: str, server_id: str, failed_token: str) -> None:
        self.refreshes += 1
        await self.backend.close_server(dev, server_id)
        raise BrokerError("remint", f"deactivated: {dev} was deactivated")


def test_close_before_first_frame_probes_and_retires(
    mm: Any, recording_sleep: Any, run: Any
) -> None:
    """#155: a websocket the server closes before `hello` sends the stream to `GET /users/me`;
    its 401 and the `deactivated` remint end the stream, which then never reconnects."""
    tokens = Deactivating(mm)
    tokens.add(DEV)
    b = make(mm, tokens, recording_sleep)
    tokens.backend = b
    status: list[tuple[bool, str]] = []

    async def scenario() -> None:
        await b.user_id(DEV)  # known from before the account was deactivated
        mm.tokens.pop(tokens.held[DEV])
        mm.close_before_hello = 10
        gen = b.stream(DEV, on_status=lambda up, why: status.append((up, why)))
        with pytest.raises(StopAsyncIteration):
            await recv(gen)
        connects = len(mm.connects)
        await asyncio.sleep(0.01)
        assert len(mm.connects) == connects == 1

    run(scenario())
    assert tokens.refreshes == 1
    assert [r[1] for r in mm.requests].count("/users/me") == 2
    assert any("deactivated" in why for up, why in status if not up)


def test_upgrade_401_deactivated_ends_without_backoff(
    mm: Any, recording_sleep: Any, run: Any
) -> None:
    """#155: a 401 at the upgrade whose remint finds the account deactivated ends the stream
    there, with no backoff sleep and no second connect."""
    tokens = Deactivating(mm)
    tokens.add(DEV)
    b = make(mm, tokens, recording_sleep)
    tokens.backend = b

    async def scenario() -> None:
        await b.user_id(DEV)  # known from before, so the upgrade is the first 401
        mm.tokens.pop(tokens.held[DEV])
        gen = b.stream(DEV)
        with pytest.raises(StopAsyncIteration):
            await recv(gen)

    run(scenario())
    assert tokens.refreshes == 1
    assert len(mm.connects) == 1
    assert recording_sleep.waits == []


def test_drop_after_frames_does_not_probe(mm: Any, recording_sleep: Any, run: Any) -> None:
    """#155: a connection that delivered frames and then dropped reconnects with no
    `GET /users/me`."""
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    b = make(mm, tokens, recording_sleep)

    async def scenario() -> None:
        gen = b.stream(DEV)
        task = asyncio.ensure_future(recv(gen))
        await until(lambda: mm.open_sockets(me) and b._agents[DEV].hello)
        asked = [r[1] for r in mm.requests].count("/users/me")
        await mm.open_sockets(me)[0].close()
        await until(lambda: len(mm.connects) == 2 and mm.open_sockets(me))
        assert [r[1] for r in mm.requests].count("/users/me") == asked
        await b.close_agent(DEV)
        task.cancel()
        await b.aclose()

    run(scenario())


def test_default_connect_pings_every_30s(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(uri: str, **kw: Any) -> object:
        seen.update(kw, uri=uri)
        return object()

    monkeypatch.setattr(websockets.asyncio.client, "connect", fake_connect)
    run(mm_adapter.ws_connect("ws://x/api/v4/websocket", headers={"A": "b"}, ping_interval=30))
    assert seen["ping_interval"] == 30
    assert seen["additional_headers"] == {"A": "b"}


# -- Story 3.2: cursor replay (R-28) and status (R-27) ----------------------------------------
def test_replay_from_cursor(mm: Any, recording_sleep: Any, run: Any) -> None:
    """With a cursor, the first connect replays the posts after it in the agent's channels,
    oldest first: not the older ones, not the cursor's own, not the agent's own, and a DM
    with the agent comes back marked `dm`."""
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    sender = mm.add_user("bj")
    chan, dm_chan = mm._id("c"), mm._id("c")
    mm.teams[mm._id("t")] = [
        {"id": chan, "type": "O", "name": "crew"},
        {"id": dm_chan, "type": "D", "name": f"{me}__{sender}"},
        {"id": "not-an-id", "type": "O", "name": "skipped"},
    ]
    mm.add_post(sender, chan, "older")
    last = mm.add_post(sender, chan, "the cursor's own")
    mm.add_post(me, chan, "my own")
    mm.add_post(sender, chan, "missed")
    mm.add_post(sender, dm_chan, "a missed DM")
    cursor = StreamCursor(last["id"], last["create_at"] / 1000, frozenset({last["id"]}))
    b = make(mm, tokens, recording_sleep)
    status: list[tuple[bool, str]] = []

    async def scenario() -> list[InboundMessage]:
        gen = b.stream(DEV, cursor=cursor, on_status=lambda up, why: status.append((up, why)))
        got = [await recv(gen), await recv(gen)]
        # The live feed repeats the missed post: not yielded again.
        missed = got[0]
        mm.broadcast_posted(mm.posts[missed.post_id])
        mm.broadcast_posted(mm.add_post(sender, chan, "live"))
        got.append(await recv(gen))
        await b.close_agent(DEV)
        return got

    got = run(scenario())
    assert [m.text for m in got] == ["missed", "a missed DM", "live"]
    assert [m.dm for m in got] == [False, True, False]
    assert status == [(True, "")]


def test_status_reports_down_and_up(mm: Any, recording_sleep: Any, run: Any) -> None:
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    b = make(mm, tokens, recording_sleep)
    status: list[tuple[bool, str]] = []

    async def scenario() -> None:
        gen = b.stream(DEV, on_status=lambda up, why: status.append((up, why)))
        task = asyncio.ensure_future(recv(gen))
        await until(lambda: status)
        mm.fail_connects = 2
        await mm.open_sockets(me)[0].close()
        await until(lambda: len(status) >= 5)
        # A rotation's reconnect is not an outage.
        tokens.rotate(DEV)
        await b.on_token_rotated(DEV)
        await until(lambda: len(mm.connects) >= 5)
        await until(lambda: mm.open_sockets(me) and b._agents[DEV].hello)
        mm.tokens.clear()  # every token revoked: 401
        await mm.open_sockets(me)[0].close()
        await until(lambda: any(why == "unauthorized" for _, why in status))
        await b.close_agent(DEV)
        task.cancel()

    run(scenario())
    assert status[0] == (True, "")
    assert status[1][0] is False and status[1][1].startswith("connection lost")
    assert status[2] == (False, "connect failed: connection refused")
    assert status[3] == (False, "connect failed: connection refused")
    assert status[4] == (True, "")
    assert status[5] == (True, "")  # after the rotation: no down in between
    assert status[6][1].startswith("connection lost")
    assert (False, "unauthorized") in status[7:]
