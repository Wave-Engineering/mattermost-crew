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
from crewchat.adapters.base import InboundMessage
from crewchat.adapters.mattermost import (
    BACKOFF_MAX_S,
    PING_INTERVAL_S,
    MattermostBackend,
    backoff_delays,
)

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


def test_token_rotation_reconnects_with_new_token(
    mm: Any, recording_sleep: Any, run: Any
) -> None:
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


def test_default_connect_pings_every_30s(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(uri: str, **kw: Any) -> object:
        seen.update(kw, uri=uri)
        return object()

    monkeypatch.setattr(websockets.asyncio.client, "connect", fake_connect)
    run(mm_adapter.ws_connect("ws://x/api/v4/websocket", headers={"A": "b"}, ping_interval=30))
    assert seen["ping_interval"] == 30
    assert seen["additional_headers"] == {"A": "b"}
