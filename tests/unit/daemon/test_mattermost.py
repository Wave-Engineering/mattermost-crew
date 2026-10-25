"""Story 2.1: the Mattermost adapter's REST side, the echo filter and mentions.

The server is `FakeMattermost` (conftest): REST through an `httpx.MockTransport`, websockets
through its `connect`. R-01, R-04, R-14, R-29; §5.9's `is_bot` and `kiosk`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

import pytest
from crewchat.adapters.base import ChatBackend, InboundMessage
from crewchat.adapters.mattermost import MattermostBackend, post_kiosk, post_proto
from crewchat.protocol import PROTO
from crewchat.tokens import Unauthorized

T = TypeVar("T")
SID = "dev"
CHAN = "c" * 26  # a Mattermost-shaped channel ID: the adapter checks the shape (#62)


class Tokens:
    """A stand-in for the token store: tokens by (dev_name, server_id), and its 401 path."""

    def __init__(self, mm: Any) -> None:
        self.mm = mm
        self.held: dict[tuple[str, str], str] = {}
        self.refreshes: list[tuple[str, str]] = []
        self.user_ids: dict[str, str] = {}

    def add(self, dev: str, *, is_bot: bool = True) -> str:
        user_id = str(self.mm.add_user(dev, is_bot=is_bot))
        self.user_ids[dev] = user_id
        self.held[(dev, SID)] = self.mm.add_token(user_id)
        return user_id

    def token_for(self, dev: str, server_id: str) -> str | None:
        return self.held.get((dev, server_id))

    async def refresh(self, dev: str, server_id: str, failed_token: str) -> None:
        """Remint: a new token for the same user; the failed one is revoked."""
        self.refreshes.append((dev, server_id))
        self.mm.tokens.pop(failed_token, None)
        self.held[(dev, server_id)] = self.mm.add_token(self.user_ids[dev])

    async def call_with_refresh(
        self, dev: str, server_id: str, request: Callable[[str], Awaitable[T]]
    ) -> T:
        token = self.held[(dev, server_id)]
        try:
            return await request(token)
        except Unauthorized:
            await self.refresh(dev, server_id, token)
        return await request(self.held[(dev, server_id)])


def backend(mm: Any, tokens: Tokens, sleep: Any = None, **kw: Any) -> MattermostBackend:
    return MattermostBackend(
        SID,
        mm.URL,
        tokens.token_for,
        refresher=tokens,
        http_client=mm.http_client(),
        connect=mm.connect,
        max_retries=0,
        **({"sleep": sleep} if sleep is not None else {}),
        **kw,
    )


async def next_message(gen: AsyncIterator[InboundMessage]) -> InboundMessage:
    return await asyncio.wait_for(anext(gen), 2)


async def until(cond: Callable[[], Any]) -> None:
    for _ in range(1000):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def test_backend_satisfies_the_contract(mm: Any) -> None:
    b: ChatBackend = backend(mm, Tokens(mm))
    assert b is not None


def test_send_sets_crew_proto_prop(mm: Any, run: Any) -> None:
    tokens = Tokens(mm)
    me = tokens.add("babel.oaw")
    b = backend(mm, tokens)
    msg = run(b.send("babel.oaw", CHAN, "hello"))
    stored = mm.posts[msg.post_id]
    assert stored["props"] == {"crew_proto": PROTO} == {"crew_proto": 1}
    assert msg.proto == 1 and msg.author_id == me and msg.author_name == "babel.oaw"
    assert msg.backend == "mattermost:dev" and msg.channel == CHAN and msg.text == "hello"
    assert msg.is_bot is True and msg.kiosk is False

    reply = run(b.send("babel.oaw", CHAN, "again", thread=msg.post_id))
    assert mm.posts[reply.post_id]["root_id"] == msg.post_id == reply.root_id
    assert mm.posts[reply.post_id]["props"]["crew_proto"] == 1


def test_inbound_proto_defaults_zero(mm: Any, run: Any) -> None:
    tokens = Tokens(mm)
    tokens.add("babel.oaw")
    human = mm.add_user("bj", is_bot=False)
    old = mm.add_post(human, CHAN, "an older post")
    marked = mm.add_post(human, CHAN, "a marked post", {"crew_proto": 1})
    kiosk = mm.add_post(human, CHAN, "a kiosk answer", {"crew_proto": 1, "crew_kiosk": 1})
    b = backend(mm, tokens)
    msgs = {m.post_id: m for m in run(b.read("babel.oaw", CHAN))}
    assert msgs[old["id"]].proto == 0
    assert msgs[marked["id"]].proto == 1
    assert msgs[kiosk["id"]].kiosk is True and msgs[marked["id"]].kiosk is False
    assert all(m.is_bot is False and m.author_name == "bj" for m in msgs.values())
    # Oldest first.
    assert [m.post_id for m in run(b.read("babel.oaw", CHAN))] == [
        old["id"],
        marked["id"],
        kiosk["id"],
    ]
    assert len(run(b.read("babel.oaw", CHAN, limit=2))) == 2


@pytest.mark.parametrize(
    ("props", "proto", "kiosk"),
    [
        (None, 0, False),
        ({}, 0, False),
        ({"crew_proto": 1}, 1, False),
        ({"crew_proto": 1.0}, 1, False),
        ({"crew_proto": "1"}, 1, False),
        ({"crew_proto": True, "crew_kiosk": True}, 0, False),
        ({"crew_proto": "x", "crew_kiosk": 2}, 0, False),
        ({"crew_proto": -1, "crew_kiosk": 1}, 0, True),
    ],
)
def test_prop_parsing(props: Any, proto: int, kiosk: bool) -> None:
    assert post_proto(props) == proto
    assert post_kiosk(props) is kiosk


def test_thread_dm_and_react(mm: Any, run: Any) -> None:
    tokens = Tokens(mm)
    me = tokens.add("babel.oaw")
    other = mm.add_user("kessel.oaw", is_bot=True)
    b = backend(mm, tokens)

    root = mm.add_post(other, CHAN, "root")
    run(b.send("babel.oaw", CHAN, "reply", thread=root["id"]))
    mm.add_post(other, "elsewhere", "unrelated")
    thread = run(b.thread("babel.oaw", root["id"]))
    assert [m.text for m in thread] == ["root", "reply"]

    dm = run(b.dm("babel.oaw", "@kessel.oaw", "psst"))
    assert dm.channel == "dm-" + "-".join(sorted([me, other]))
    assert mm.posts[dm.post_id]["props"] == {"crew_proto": 1}
    by_id = run(b.dm("babel.oaw", other, "again"))
    assert by_id.channel == dm.channel

    run(b.react("babel.oaw", root["id"], ":thumbsup:"))
    assert mm.reactions == [{"user_id": me, "post_id": root["id"], "emoji_name": "thumbsup"}]


def test_two_agents_post_as_two_authors(mm: Any, run: Any) -> None:
    """R-01: one daemon, two agents, two upstream client objects, two distinct authors."""
    tokens = Tokens(mm)
    babel = tokens.add("babel.oaw")
    kessel = tokens.add("kessel.oaw")
    b = backend(mm, tokens)

    async def scenario() -> tuple[InboundMessage, InboundMessage]:
        return await asyncio.gather(
            b.send("babel.oaw", CHAN, "from babel"), b.send("kessel.oaw", CHAN, "from kessel")
        )

    one, two = run(scenario())
    assert (one.author_id, two.author_id) == (babel, kessel)
    assert mm.posts[one.post_id]["user_id"] != mm.posts[two.post_id]["user_id"]
    clients = b._clients
    assert set(clients) == {"babel.oaw", "kessel.oaw"}
    assert clients["babel.oaw"].client is not clients["kessel.oaw"].client


def test_401_is_refreshed_through_the_store(mm: Any, run: Any) -> None:
    tokens = Tokens(mm)
    tokens.add("babel.oaw")
    b = backend(mm, tokens)
    run(b.send("babel.oaw", CHAN, "one"))
    first_client = b._clients["babel.oaw"].client
    # The token expires on the server: the next request answers 401.
    mm.tokens.pop(tokens.held[("babel.oaw", SID)])
    msg = run(b.send("babel.oaw", CHAN, "two"))
    assert msg.text == "two"
    assert tokens.refreshes == [("babel.oaw", SID)]
    assert b._clients["babel.oaw"].client is not first_client  # rebuilt on the new token


def test_unauthorized_without_refresher(mm: Any, run: Any) -> None:
    tokens = Tokens(mm)
    tokens.add("babel.oaw")
    mm.tokens.clear()
    b = MattermostBackend(SID, mm.URL, tokens.token_for, http_client=mm.http_client())
    with pytest.raises(Unauthorized):
        run(b.send("babel.oaw", CHAN, "x"))
    with pytest.raises(KeyError):
        run(b.send("nobody", CHAN, "x"))


def test_echo_filtered_by_user_id(mm: Any, recording_sleep: Any, run: Any) -> None:
    """R-04: the agent's own post is dropped even when it carries another agent's signature;
    a crewmate's post that carries the agent's own signature text is delivered."""
    tokens = Tokens(mm)
    babel = tokens.add("babel.oaw")
    kessel = tokens.add("kessel.oaw")
    b = backend(mm, tokens, recording_sleep)

    async def scenario() -> InboundMessage:
        gen = b.stream("babel.oaw")
        first = asyncio.ensure_future(next_message(gen))
        await until(lambda: mm.open_sockets(babel))
        mm.broadcast_posted(mm.add_post(babel, CHAN, "hi — **kessel** 🛰 (oaw)"))
        mm.broadcast_posted(mm.add_post(kessel, CHAN, "hi — **babel** 🐟 (oaw)"))
        msg = await first
        await b.close_agent("babel.oaw")
        return msg

    msg = run(scenario())
    assert msg.author_id == kessel and msg.text.endswith("**babel** 🐟 (oaw)")
    assert msg.is_bot is True and msg.author_name == "kessel.oaw"


def test_mentions_from_server_field(mm: Any, recording_sleep: Any, run: Any) -> None:
    """R-29: `mentions` is the event's own field, never parsed from the text."""
    tokens = Tokens(mm)
    babel = tokens.add("babel.oaw")
    human = mm.add_user("bj", is_bot=False)
    b = backend(mm, tokens, recording_sleep)

    async def scenario() -> list[InboundMessage]:
        gen = b.stream("babel.oaw")
        first = asyncio.ensure_future(next_message(gen))
        await until(lambda: mm.open_sockets(babel))
        # Text names @babel.oaw, but the server marked no mention.
        mm.broadcast_posted(mm.add_post(human, CHAN, "@babel.oaw look"))
        # Text names nobody, but the server marked babel.
        mm.broadcast_posted(mm.add_post(human, CHAN, "look"), mentions=[babel])
        got = [await first, await next_message(gen)]
        await b.close_agent("babel.oaw")
        return got

    plain, marked = run(scenario())
    assert plain.mentions == []
    assert marked.mentions == [babel]
    assert plain.is_bot is False and plain.proto == 0


def test_is_bot_read_from_user_object_once(mm: Any, run: Any) -> None:
    """§5.9, D-100: `is_bot` is the user object's, cached per author; post props never count."""
    tokens = Tokens(mm)
    tokens.add("babel.oaw")
    human = mm.add_user("bj", is_bot=False)
    mm.add_post(human, CHAN, "one", {"is_bot": True, "from_bot": "true"})
    mm.add_post(human, CHAN, "two")
    b = backend(mm, tokens)
    msgs = run(b.read("babel.oaw", CHAN))
    msgs += run(b.read("babel.oaw", CHAN))
    assert [m.is_bot for m in msgs] == [False] * 4
    lookups = [r for r in mm.requests if r[:2] == ("GET", f"/users/{human}")]
    assert len(lookups) == 1
    assert not hasattr(msgs[0], "owner_id")
