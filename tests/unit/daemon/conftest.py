"""Fakes for the daemon's unit tests: brokers, adapters, a clock and a Mattermost server.

`FakeBroker` stands in for one broker socket. Every broker call and adapter hook appends to
one shared `events` list, so a test can assert the order of a rotation across both.

`FakeMattermost` answers the REST calls the Mattermost adapter makes, through an
`httpx.MockTransport`, and serves fake websockets through its `connect`.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from crewchat.config import ServersConfig, parse_config
from crewchat.registry import Registry
from crewchat.tokens import BrokerClient, BrokerError, BrokerKey, SwitchCache, TokenStore
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK, InvalidStatus
from websockets.http11 import Response

from tests.fakes.screamhole import parse_multipart

Event = tuple[Any, ...]

SERVERS_TOML: dict[str, Any] = {
    "default_crew": "oaw",
    "mattermost": [{"id": "dev", "url": "http://mm.invalid"}],
    "discord": {"enabled": True},
    "crews": {
        "oaw": {
            "needs_list": "bj:needs",
            "dev": {"broker_socket": "/run/crewbroker/oaw-dev.sock", "broker_user_id": "b-oaw"},
        },
    },
}


class FakeBroker(BrokerClient):
    """A broker that answers in process. Tokens are `tok-<n>` with id `id-<n>`."""

    _ids = itertools.count(1)

    def __init__(self, crew: str, server_id: str, events: list[Event]) -> None:
        name = f"{crew}-{server_id}"
        super().__init__(f"/run/crewbroker/{name}.sock", 2.0, name=name)
        self.crew = crew
        self.server_id = server_id
        self.events = events
        self.live: dict[str, str] = {}  # token_id -> account
        self.bots: list[str] = []
        self.fail: dict[str, BaseException] = {}
        self.on_revoke: Callable[[str], None] | None = None

    def ops(self, op: str) -> list[dict[str, Any]]:
        return [e[3] for e in self.events if e[0] == "broker" and e[1] == self.name and e[2] == op]

    def _token(self, account: str) -> dict[str, Any]:
        n = next(self._ids)
        self.live[f"id-{n}"] = account
        return {"token": f"tok-{n}", "token_id": f"id-{n}"}

    async def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.events.append(("broker", self.name, op, dict(args)))
        if op in self.fail:
            raise self.fail[op]
        if op == "mint":
            account = f"{args['dev_name']}.{args['crew']}"
            if account not in self.bots:
                self.bots.append(account)
            return {"account": account, **self._token(account)}
        if op == "remint":
            account = f"{args['dev_name']}.{args['crew']}"
            if account not in self.bots:
                raise BrokerError(op, "not_found: never registered")
            return self._token(account)
        if op == "revoke":
            if self.on_revoke is not None:
                self.on_revoke(args["token_id"])
            if self.live.pop(args["token_id"], None) is None:
                raise BrokerError(op, "not_found: no token with that id")
            return {}
        if op == "crew_info":
            return {
                "broker_user_id": f"b-{self.crew}",
                "human_user_ids": ["h1"],
                "trusted": [],
                "minted_bot_ids": [f"bot:{a}" for a in self.bots],
            }
        raise BrokerError(op, "unsupported")


class FakeAdapter:
    def __init__(self, events: list[Event]) -> None:
        self.events = events

    async def on_token_rotated(self, dev_name: str) -> None:
        self.events.append(("hook", dev_name))

    async def close_agent(self, dev_name: str) -> None:
        self.events.append(("close", dev_name))


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class Daemon:
    """A registry, a store and fake brokers, wired as `crewchat serve` wires them."""

    def __init__(self, config: ServersConfig, root: Path, clock: Clock) -> None:
        self.config = config
        self.events: list[Event] = []
        self.brokers: dict[BrokerKey, FakeBroker] = {
            (b.crew, b.server_id): FakeBroker(b.crew, b.server_id, self.events)
            for b in config.broker_entries()
        }
        self.adapter = FakeAdapter(self.events)
        self.store = TokenStore(root, self.brokers, [self.adapter])
        self.cache = SwitchCache(self.brokers)
        self.clock = clock
        self.registry = Registry(config, self.brokers, self.store, self.cache, clock=clock)

    def broker(self, crew: str = "oaw", server_id: str = "dev") -> FakeBroker:
        return self.brokers[(crew, server_id)]


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def servers_toml() -> dict[str, Any]:
    """A fresh copy of the parsed `servers.toml`, safe to change."""
    return copy.deepcopy(SERVERS_TOML)


@pytest.fixture
def servers_config() -> ServersConfig:
    return parse_config(SERVERS_TOML)


@pytest.fixture
def daemon(servers_config: ServersConfig, tmp_path: Path, clock: Clock) -> Daemon:
    return Daemon(servers_config, tmp_path / "tokens", clock)


@pytest.fixture
def make_daemon(tmp_path: Path, clock: Clock) -> Callable[[dict[str, Any]], Daemon]:
    def make(raw: dict[str, Any]) -> Daemon:
        return Daemon(parse_config(raw), tmp_path / "tokens", clock)

    return make


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # A unix socket path is limited to 108 bytes; pytest's tmp_path can be longer.
    with tempfile.TemporaryDirectory(prefix="cc-", dir="/tmp") as d:
        yield Path(d)


# -- a fake Mattermost server ------------------------------------------------------------------
_CLOSE = object()


class FakeWebSocket:
    """One open websocket: `recv` returns queued frames; a close raises `ConnectionClosedOK`."""

    def __init__(self, server: FakeMattermost, user_id: str, token: str, uri: str) -> None:
        self.server = server
        self.user_id = user_id
        self.token = token
        self.uri = uri
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.seq = 0

    def push(self, event: str, data: Mapping[str, Any]) -> None:
        if not self.closed:
            self.queue.put_nowait(json.dumps({"event": event, "data": dict(data), "seq": self.seq}))
            self.seq += 1

    async def recv(self) -> str:
        item = await self.queue.get()
        if item is _CLOSE:
            self.queue.put_nowait(_CLOSE)
            raise ConnectionClosedOK(None, None)
        assert isinstance(item, str)
        return item

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(_CLOSE)


class FakeMattermost:
    """Users, tokens, posts and websockets, in process. IDs are 26 lowercase characters."""

    URL = "http://mm.invalid"

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}  # token -> user_id
        self.posts: dict[str, dict[str, Any]] = {}
        self.reactions: list[dict[str, Any]] = []
        self.files: dict[str, dict[str, Any]] = {}  # file id -> {id, name, data, ...}
        self.hidden_from: dict[str, set[str]] = {}  # post id -> user ids it 404s for
        self.requests: list[tuple[str, str, str]] = []  # (method, path, token)
        self.sockets: list[FakeWebSocket] = []
        self.connects: list[dict[str, Any]] = []
        self.fail_connects = 0
        self._n = itertools.count(1)

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._n):0>{26 - len(prefix)}}"

    def add_user(self, username: str, *, is_bot: bool = False) -> str:
        user_id = self._id("u")
        self.users[user_id] = {"id": user_id, "username": username, "is_bot": is_bot}
        return user_id

    def add_token(self, user_id: str) -> str:
        token = self._id("t")
        self.tokens[token] = user_id
        return token

    def add_post(
        self, user_id: str, channel: str, message: str, props: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        post = {
            "id": self._id("p"),
            "channel_id": channel,
            "root_id": "",
            "user_id": user_id,
            "message": message,
            "create_at": int(time.time() * 1000) + len(self.posts),
            "props": props or {},
        }
        self.posts[post["id"]] = post
        return post

    def broadcast_posted(
        self,
        post: Mapping[str, Any],
        mentions: list[str] | None = None,
        *,
        channel_type: str = "O",
        channel_name: str = "",
    ) -> None:
        """Push a `posted` event to every open websocket, the author's own included."""
        data: dict[str, Any] = {"post": json.dumps(post), "channel_type": channel_type}
        if channel_name:
            data["channel_name"] = channel_name
        if mentions is not None:
            data["mentions"] = json.dumps(mentions)
        for ws in self.sockets:
            ws.push("posted", data)

    def open_sockets(self, user_id: str) -> list[FakeWebSocket]:
        return [ws for ws in self.sockets if ws.user_id == user_id and not ws.closed]

    # -- websockets ------------------------------------------------------------------------
    async def connect(
        self, uri: str, *, headers: Mapping[str, str], ping_interval: float
    ) -> FakeWebSocket:
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        self.connects.append({"uri": uri, "token": token, "ping_interval": ping_interval})
        if self.fail_connects:
            self.fail_connects -= 1
            raise OSError("connection refused")
        user_id = self.tokens.get(token)
        if user_id is None:
            raise InvalidStatus(Response(401, "Unauthorized", Headers()))
        ws = FakeWebSocket(self, user_id, token, uri)
        self.sockets.append(ws)
        ws.push("hello", {"connection_id": f"conn-{len(self.sockets)}"})
        return ws

    # -- REST ------------------------------------------------------------------------------
    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self.URL}/api/v4", transport=httpx.MockTransport(self.handle)
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        path = request.url.path.removeprefix("/api/v4")
        self.requests.append((request.method, path, token))
        me = self.tokens.get(token)
        if me is None:
            return httpx.Response(401, json={"id": "api.context.session_expired.app_error"})
        parts = path.strip("/").split("/")
        multipart = request.headers.get("Content-Type", "").startswith("multipart/")
        body = json.loads(request.content) if request.content and not multipart else None
        if request.method == "GET" and path == "/users/me":
            return httpx.Response(200, json=self.users[me])
        if request.method == "GET" and parts[:2] == ["users", "username"]:
            found = [u for u in self.users.values() if u["username"] == parts[2]]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={})
        if request.method == "GET" and parts[0] == "users" and len(parts) == 2:
            user = self.users.get(parts[1])
            return httpx.Response(200, json=user) if user else httpx.Response(404, json={})
        if request.method == "POST" and path == "/files":
            fields, files = parse_multipart(request.headers["Content-Type"], request.content)
            name, data = files["files"]
            file_id = self._id("f")
            self.files[file_id] = {"id": file_id, "name": name, "data": data, "user_id": me}
            self.files[file_id]["channel_id"] = fields.get("channel_id", "")
            return httpx.Response(201, json={"file_infos": [{"id": file_id, "name": name}]})
        if request.method == "POST" and path == "/posts":
            post = self.add_post(me, body["channel_id"], body["message"], body.get("props"))
            post["root_id"] = body.get("root_id", "")
            if body.get("file_ids"):
                post["file_ids"] = list(body["file_ids"])
            return httpx.Response(201, json=post)
        if request.method == "GET" and parts[0] == "posts" and len(parts) == 2:
            found = self.posts.get(parts[1])
            if found is None or me in self.hidden_from.get(parts[1], set()):
                return httpx.Response(404, json={"id": "app.post.get.app_error"})
            return httpx.Response(200, json=found)
        if request.method == "GET" and parts[0] == "channels" and parts[2:] == ["posts"]:
            posts = {k: p for k, p in self.posts.items() if p["channel_id"] == parts[1]}
            order = sorted(posts, key=lambda k: -posts[k]["create_at"])
            return httpx.Response(200, json={"posts": posts, "order": order})
        if request.method == "GET" and parts[0] == "posts" and parts[2:] == ["thread"]:
            root = parts[1]
            posts = {k: p for k, p in self.posts.items() if root in (k, p["root_id"])}
            return httpx.Response(200, json={"posts": posts, "order": list(posts)})
        if request.method == "POST" and path == "/channels/direct":
            return httpx.Response(201, json={"id": "dm-" + "-".join(sorted(body)), "type": "D"})
        if request.method == "POST" and path == "/reactions":
            self.reactions.append(body)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"message": f"no route {request.method} {path}"})


class RecordingSleep:
    """An injected sleep: records each wait and yields once, without waiting."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        await asyncio.sleep(0)


@pytest.fixture
def mm() -> FakeMattermost:
    return FakeMattermost()


@pytest.fixture
def run() -> Iterator[Callable[[Any], Any]]:
    """`asyncio.run`, but every call in one test shares one event loop, as the daemon does:
    an adapter's clients and websockets live across calls."""
    with asyncio.Runner() as runner:
        yield runner.run


@pytest.fixture
def recording_sleep() -> RecordingSleep:
    return RecordingSleep()
