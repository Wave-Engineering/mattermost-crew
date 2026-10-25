"""The Mattermost adapter (Dev Spec §4.8, §5.9; Story 2.1; R-01, R-04, R-10, R-14, R-29, CT-07).

`MattermostBackend(server_id, url, token_for)` is one configured Mattermost server (R-10). It
acts as each agent with that agent's own token, through **one upstream client per Dev-Name**
(`mcp_server_mattermost.client.MattermostClient`, D-009), so two agents in one daemon post as
two distinct authors (R-01).

- `send` marks every post `props.crew_proto = PROTO`; `read` and `thread` take `proto` from
  `props.crew_proto`, 0 when it is absent (R-14).
- `stream(dev_name)` holds that agent's `/api/v4/websocket`. The client pings every 30 s and a
  lost connection is retried with exponential backoff, capped at 60 s (CT-07). A `posted`
  event becomes an `InboundMessage` whose `mentions` are the event's own `mentions` field,
  never parsed from the text (R-29). The agent's own posts are dropped by user ID, whatever
  their text says (R-04). Its `dm` is true when the event's channel is a direct-message
  channel (`channel_type` `D`) whose two members are the author and the agent (R-19).
- `on_token_rotated(dev_name)` closes the agent's websocket when its token changed, and the
  stream reopens it at once with the new token: revoking a token doesn't close an open
  websocket, it only stops its events (§4.8). The reconnect asks the server to resume the
  connection (`connection_id`, `sequence_number`), so events in the gap are still delivered
  while the server holds them; the doorbell's cursor replay covers the rest (R-28).
- A 401 on a request is refreshed through the token store's `call_with_refresh` (`remint`,
  never `mint`; R-30), and a 401 on the websocket handshake through its `refresh`.
- `is_bot` comes from the author's user object (`GET /users/{id}`, cached per author ID, as a
  `posted` event carries no user object), never from post props; `kiosk` is
  `props.crew_kiosk == 1` (§5.9). There is no `owner_id`: ownership is the brokers'
  `crew_info.minted_bot_ids` (D-100).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast
from urllib.parse import urlencode

import httpx
import websockets.asyncio.client
from mcp_server_mattermost.client import MattermostClient, create_http_client
from mcp_server_mattermost.config import AuthMode, Settings
from mcp_server_mattermost.exceptions import (
    AuthenticationError,
    MattermostAPIError,
    NotFoundError,
)
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from crewchat.adapters.base import Attachment, Embed, InboundMessage, NotFound
from crewchat.protocol import PROTO
from crewchat.tokens import Unauthorized

PING_INTERVAL_S = 30.0  # CT-07
PING_TIMEOUT_S = 30.0
BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 60.0  # CT-07
DEFAULT_READ_LIMIT = 60
USER_ID_RE = re.compile(r"^[a-z0-9]{26}$")

log = logging.getLogger("crewchat.adapters.mattermost")

T = TypeVar("T")
TokenFor = Callable[[str, str], str | None]


class TokenRefresher(Protocol):
    """The token store's 401 path (`crewchat.tokens.TokenStore` provides it)."""

    async def call_with_refresh(
        self, dev_name: str, server_id: str, request: Callable[[str], Awaitable[T]]
    ) -> T: ...

    async def refresh(self, dev_name: str, server_id: str, failed_token: str) -> None: ...


class WebSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class Connect(Protocol):
    def __call__(
        self, uri: str, *, headers: Mapping[str, str], ping_interval: float
    ) -> Awaitable[WebSocket]: ...


async def ws_connect(uri: str, *, headers: Mapping[str, str], ping_interval: float) -> WebSocket:
    """Open a websocket with the `websockets` library, pinging every `ping_interval` seconds."""
    conn = await websockets.asyncio.client.connect(
        uri,
        additional_headers=dict(headers),
        ping_interval=ping_interval,
        ping_timeout=PING_TIMEOUT_S,
        proxy=None,
    )
    return cast(WebSocket, conn)


def backoff_delays(first: float = BACKOFF_FIRST_S, cap: float = BACKOFF_MAX_S) -> Iterator[float]:
    """1, 2, 4, ... seconds, never more than `cap` (CT-07)."""
    delay = first
    while True:
        yield min(delay, cap)
        delay = min(delay * 2, cap)


def _int_prop(value: Any) -> int | None:
    """A numeric post prop as an int. JSON may hand back 1 or 1.0; `true` is not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def post_proto(props: Any) -> int:
    """`props.crew_proto`, or 0 for a post without a valid marker (R-14)."""
    if not isinstance(props, dict):
        return 0
    proto = _int_prop(props.get("crew_proto"))
    return proto if proto is not None and proto >= 0 else 0


def post_kiosk(props: Any) -> bool:
    return isinstance(props, dict) and _int_prop(props.get("crew_kiosk")) == 1


def embed_attachment(embed: Embed) -> dict[str, str]:
    """Today's Discord embed as a Mattermost message attachment, which both editions render
    (`props.attachments: [{title, text}]`, CT-01)."""
    return {"title": embed.title, "text": embed.body}


def event_mentions(data: Mapping[str, Any]) -> list[str]:
    """The `posted` event's `mentions`: a JSON-encoded list of user IDs, absent when none."""
    raw = data.get("mentions")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, str)]


def event_dm(data: Mapping[str, Any], author_id: str, me: str | None) -> bool:
    """A `posted` event in a DM channel whose members are `author_id` and the agent `me` (R-19).

    A DM channel's name is its two members' user IDs joined by `__`.
    """
    if me is None or not author_id or data.get("channel_type") != "D":
        return False
    members = str(data.get("channel_name", "")).split("__")
    return len(members) == 2 and sorted(members) == sorted([author_id, me])


@dataclass
class _Client:
    token: str
    client: MattermostClient
    stack: AsyncExitStack


@dataclass
class _Agent:
    """One agent's connection state on this server."""

    user_id: str | None = None
    ws: WebSocket | None = None
    ws_token: str | None = None
    rotated: bool = False  # the open websocket was closed for a new token: reconnect now
    closed: bool = False  # `close_agent`: end the stream
    connection_id: str = ""  # from `hello`, to resume the connection on reconnect
    next_seq: int = 0
    hello: bool = False  # the current connection has delivered a frame
    connects: int = 0


class MattermostBackend:
    """One configured Mattermost server, acting as each agent with its own token."""

    def __init__(
        self,
        server_id: str,
        url: str,
        token_for: TokenFor,
        *,
        refresher: TokenRefresher | None = None,
        http_client: httpx.AsyncClient | None = None,
        connect: Connect = ws_connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ping_interval_s: float = PING_INTERVAL_S,
        max_retries: int = 3,
    ) -> None:
        self.server_id = server_id
        self.url = url.rstrip("/")
        self.name = f"mattermost:{server_id}"
        self._token_for = token_for
        self.refresher = refresher
        self._connect = connect
        self._sleep = sleep
        self.ping_interval_s = ping_interval_s
        # `client_token` mode: the settings carry no token; each client gets the agent's own.
        self._settings = Settings(
            url=self.url,
            auth_mode=AuthMode.CLIENT_TOKEN,
            max_retries=max_retries,
            _env_file=None,  # type: ignore[call-arg]
        )
        self._http = http_client
        self._owns_http = http_client is None
        self._clients: dict[str, _Client] = {}
        self._agents: dict[str, _Agent] = {}
        self._users: dict[str, dict[str, Any]] = {}  # author ID -> user object, cached

    # -- clients and tokens -----------------------------------------------------------------
    def token(self, dev_name: str) -> str:
        token = self._token_for(dev_name, self.server_id)
        if not token:
            raise KeyError(f"{dev_name} holds no token on {self.server_id}")
        return token

    def _shared_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = create_http_client(self._settings)
        return self._http

    async def client_for(self, dev_name: str, token: str) -> MattermostClient:
        """The agent's one upstream client, rebuilt when its token has changed."""
        held = self._clients.get(dev_name)
        if held is not None and held.token == token:
            return held.client
        if held is not None:
            await held.stack.aclose()
        client = MattermostClient(self._settings, token=token, http_client=self._shared_http())
        stack = AsyncExitStack()
        await stack.enter_async_context(client.lifespan())
        self._clients[dev_name] = _Client(token, client, stack)
        return client

    async def _call(
        self, dev_name: str, request: Callable[[MattermostClient], Awaitable[T]]
    ) -> T:
        """Run `request` as `dev_name`; a 401 goes through the store's refresh (R-30)."""

        async def with_token(token: str) -> T:
            try:
                return await request(await self.client_for(dev_name, token))
            except AuthenticationError as exc:
                raise Unauthorized(f"{dev_name} on {self.server_id}") from exc

        if self.refresher is None:
            return await with_token(self.token(dev_name))
        return await self.refresher.call_with_refresh(dev_name, self.server_id, with_token)

    def _agent(self, dev_name: str) -> _Agent:
        return self._agents.setdefault(dev_name, _Agent())

    async def user_id(self, dev_name: str) -> str:
        """The agent's own user ID, for the echo filter (R-04)."""
        agent = self._agent(dev_name)
        if agent.user_id is None:
            me = await self._call(dev_name, lambda c: c.get_me())
            agent.user_id = str(me["id"])
        return agent.user_id

    async def _user(self, client: MattermostClient, user_id: str) -> dict[str, Any]:
        """The author's user object, cached by ID; `{}` when it can't be read."""
        if not user_id:
            return {}
        cached = self._users.get(user_id)
        if cached is not None:
            return cached
        try:
            user = await client.get_user(user_id)
        except AuthenticationError:
            raise
        except MattermostAPIError as exc:
            log.warning("user %s on %s: %s", user_id, self.server_id, exc)
            return {}
        self._users[user_id] = user
        return user

    async def message(
        self,
        client: MattermostClient,
        post: Mapping[str, Any],
        mentions: list[str] | None = None,
        *,
        dm: bool = False,
    ) -> InboundMessage:
        """Map a Mattermost post object to an `InboundMessage`."""
        author_id = str(post.get("user_id", ""))
        user = await self._user(client, author_id)
        props = post.get("props")
        create_at = post.get("create_at")
        return InboundMessage(
            backend=self.name,
            channel=str(post.get("channel_id", "")),
            post_id=str(post.get("id", "")),
            root_id=str(post.get("root_id") or ""),
            author_id=author_id,
            author_name=str(user.get("username", "")),
            text=str(post.get("message", "")),
            mentions=list(mentions or []),
            proto=post_proto(props),
            ts=create_at / 1000.0 if isinstance(create_at, int | float) else 0.0,
            is_bot=user.get("is_bot") is True,
            kiosk=post_kiosk(props),
            dm=dm,
        )

    async def _messages(
        self, client: MattermostClient, result: Mapping[str, Any]
    ) -> list[InboundMessage]:
        """A `{posts, order}` list, oldest first."""
        posts = result.get("posts") or {}
        if not isinstance(posts, dict):
            return []
        ordered = sorted(posts.values(), key=lambda p: (p.get("create_at", 0), p.get("id", "")))
        return [await self.message(client, p) for p in ordered]

    # -- the contract -----------------------------------------------------------------------
    async def _post(
        self,
        client: MattermostClient,
        channel: str,
        text: str,
        thread: str | None,
        attachments: Sequence[Attachment],
        embed: Embed | None,
    ) -> InboundMessage:
        """Upload `attachments` with the agent's own token (`POST /api/v4/files`), then post
        with their `file_ids`; `embed` is a message attachment (CT-01)."""
        file_ids: list[str] = []
        for att in attachments:
            uploaded = await client.upload_file(channel, str(att.path), filename=att.name)
            infos = uploaded.get("file_infos") or []
            if not infos or not isinstance(infos[0], dict) or "id" not in infos[0]:
                raise MattermostAPIError(f"upload of {att.name} returned no file id")
            file_ids.append(str(infos[0]["id"]))
        props: dict[str, Any] = {"crew_proto": PROTO}
        if embed is not None:
            props["attachments"] = [embed_attachment(embed)]
        post = await client.create_post(
            channel, text, root_id=thread, file_ids=file_ids or None, props=props
        )
        if file_ids and len(post.get("file_ids") or []) < len(file_ids):
            raise MattermostAPIError("the post was made without its attachments")
        return await self.message(client, post)

    async def send(
        self,
        dev_name: str,
        channel: str,
        text: str,
        thread: str | None = None,
        *,
        attachments: Sequence[Attachment] = (),
        embed: Embed | None = None,
    ) -> InboundMessage:
        async def request(client: MattermostClient) -> InboundMessage:
            return await self._post(client, channel, text, thread, attachments, embed)

        return await self._call(dev_name, request)

    async def read_one(self, dev_name: str, channel: str, post_id: str) -> InboundMessage:
        """One post, `GET /api/v4/posts/{id}`; one in another channel is not found."""

        async def request(client: MattermostClient) -> InboundMessage:
            try:
                post = await client.get_post(post_id)
            except NotFoundError as exc:
                raise NotFound(f"post {post_id}") from exc
            if channel and str(post.get("channel_id", "")) != channel:
                raise NotFound(f"post {post_id} in {channel}")
            return await self.message(client, post)

        return await self._call(dev_name, request)

    async def has_post(self, dev_name: str, post_id: str) -> bool:
        """`GET /api/v4/posts/{id}` as the agent answers 200 (R-12's probe)."""

        async def request(client: MattermostClient) -> bool:
            try:
                post = await client.get_post(post_id)
            except AuthenticationError:
                raise  # the store's refresh path
            except MattermostAPIError:
                return False  # 404, or a 403 on a channel the agent can't see
            return str(post.get("id", "")) == post_id

        try:
            return await self._call(dev_name, request)
        except (KeyError, Unauthorized, MattermostAPIError, httpx.HTTPError) as exc:
            log.warning("thread probe %s on %s: %s", post_id, self.server_id, exc)
            return False

    async def read(
        self, dev_name: str, channel: str, since: float | None = None, limit: int | None = None
    ) -> list[InboundMessage]:
        async def request(client: MattermostClient) -> list[InboundMessage]:
            if since is not None:
                result = await client.get_posts_since(channel, int(since * 1000))
            else:
                result = await client.get_posts(channel, per_page=limit or DEFAULT_READ_LIMIT)
            return await self._messages(client, result)

        messages = await self._call(dev_name, request)
        return messages[-limit:] if limit else messages

    async def thread(self, dev_name: str, post_id: str) -> list[InboundMessage]:
        async def request(client: MattermostClient) -> list[InboundMessage]:
            return await self._messages(client, await client.get_thread(post_id))

        return await self._call(dev_name, request)

    async def dm(
        self,
        dev_name: str,
        to: str,
        text: str,
        *,
        attachments: Sequence[Attachment] = (),
        embed: Embed | None = None,
    ) -> InboundMessage:
        """`to` is a username (with or without `@`) or a user ID."""
        me = await self.user_id(dev_name)
        name = to.removeprefix("@")

        async def request(client: MattermostClient) -> InboundMessage:
            if USER_ID_RE.match(name):
                other = name
            else:
                other = str((await client.get_user_by_username(name))["id"])
            channel = await client.create_direct_channel([me, other])
            return await self._post(client, str(channel["id"]), text, None, attachments, embed)

        return await self._call(dev_name, request)

    async def react(self, dev_name: str, post_id: str, emoji: str) -> None:
        async def request(client: MattermostClient) -> None:
            await client.add_reaction(post_id, emoji.strip(":"))

        await self._call(dev_name, request)

    # -- the stream -------------------------------------------------------------------------
    def ws_uri(self, agent: _Agent) -> str:
        base = re.sub(r"^http", "ws", self.url) + "/api/v4/websocket"
        if not agent.connection_id:
            return base
        query = {"connection_id": agent.connection_id, "sequence_number": agent.next_seq}
        return f"{base}?{urlencode(query)}"

    async def _open(self, dev_name: str, agent: _Agent) -> WebSocket | None:
        """Connect with the agent's current token. None: a failure the caller backs off from."""
        try:
            token = self.token(dev_name)
        except KeyError as exc:
            log.warning("stream %s: %s", dev_name, exc)
            return None
        try:
            ws = await self._connect(
                self.ws_uri(agent),
                headers={"Authorization": f"Bearer {token}"},
                ping_interval=self.ping_interval_s,
            )
        except InvalidStatus as exc:
            if exc.response.status_code == 401 and self.refresher is not None:
                log.info("websocket 401 for %s on %s: refreshing", dev_name, self.server_id)
                with suppress(Exception):
                    await self.refresher.refresh(dev_name, self.server_id, token)
            else:
                log.warning("websocket %s on %s: %s", dev_name, self.server_id, exc)
            return None
        except (OSError, TimeoutError, WebSocketException) as exc:
            log.warning("websocket %s on %s: %s", dev_name, self.server_id, exc)
            return None
        agent.ws, agent.ws_token = ws, token
        agent.connects += 1
        agent.hello = False
        return ws

    async def _event(self, dev_name: str, agent: _Agent, raw: str | bytes) -> InboundMessage | None:
        try:
            event = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        seq = event.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and "event" in event:
            agent.next_seq = seq + 1
        kind = event.get("event")
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        if kind == "hello":
            conn_id = data.get("connection_id")
            if isinstance(conn_id, str):
                agent.connection_id = conn_id
            return None
        if kind != "posted":
            return None
        try:
            post = json.loads(str(data.get("post", "")))
        except ValueError:
            return None
        if not isinstance(post, dict):
            return None
        if agent.user_id is not None and post.get("user_id") == agent.user_id:
            return None  # echo, by user ID only (R-04)
        mentions = event_mentions(data)
        dm = event_dm(data, str(post.get("user_id", "")), agent.user_id)

        async def request(client: MattermostClient) -> InboundMessage:
            return await self.message(client, post, mentions, dm=dm)

        try:
            return await self._call(dev_name, request)
        except (KeyError, MattermostAPIError, Unauthorized, httpx.HTTPError) as exc:
            log.warning("posted event for %s: %s", dev_name, exc)
            return None

    async def stream(self, dev_name: str) -> AsyncIterator[InboundMessage]:
        """`dev_name`'s inbound posts, reconnecting until `close_agent` (CT-07)."""
        agent = self._agent(dev_name)
        agent.closed = False
        delays = backoff_delays()
        while not agent.closed:
            if agent.user_id is None:
                try:
                    await self.user_id(dev_name)
                except (KeyError, MattermostAPIError, Unauthorized, httpx.HTTPError) as exc:
                    log.warning("stream %s: who am I: %s", dev_name, exc)
                    await self._sleep(next(delays))
                    continue
            ws = await self._open(dev_name, agent)
            if ws is None:
                await self._sleep(next(delays))
                continue
            try:
                while True:
                    # A dead connection is the library's to notice: its pings time out.
                    raw = await ws.recv()
                    if not agent.hello:
                        delays = backoff_delays()  # connected: the next loss starts at 1 s
                    msg = await self._event(dev_name, agent, raw)
                    agent.hello = True
                    if msg is not None:
                        yield msg
            except (ConnectionClosed, TimeoutError, OSError) as exc:
                if not agent.rotated and not agent.closed:
                    log.warning("websocket %s on %s lost: %s", dev_name, self.server_id, exc)
            finally:
                agent.ws = None
                with suppress(Exception):
                    await ws.close()
            if agent.closed:
                break
            if agent.rotated:
                agent.rotated = False  # a new token: reconnect at once
                continue
            await self._sleep(next(delays))

    # -- token hooks (crewchat.tokens.Adapter) ----------------------------------------------
    async def on_token_rotated(self, dev_name: str) -> None:
        """Reopen `dev_name`'s websocket if its token on this server changed (§4.8)."""
        agent = self._agents.get(dev_name)
        if agent is None or agent.ws is None:
            return
        if agent.ws_token == self._token_for(dev_name, self.server_id):
            return  # another server's token rotated
        agent.rotated = True
        await agent.ws.close()

    async def close_agent(self, dev_name: str) -> None:
        """`dev_name` left rotation: end its stream and drop its client."""
        agent = self._agents.get(dev_name)
        if agent is not None:
            agent.closed = True
            if agent.ws is not None:
                await agent.ws.close()
        held = self._clients.pop(dev_name, None)
        if held is not None:
            await held.stack.aclose()

    async def aclose(self) -> None:
        for dev_name in list(self._agents):
            await self.close_agent(dev_name)
        for dev_name in list(self._clients):
            await self._clients.pop(dev_name).stack.aclose()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
