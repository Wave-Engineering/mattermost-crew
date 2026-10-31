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
  channel (`channel_type` `D`) whose two members are the author and the agent (R-19). On
  pull (`read`, `thread`, `read_one`), `dm` is true for a post in a DM channel one of whose
  two members is the reading agent, its own posts included: `GET /channels/{id}`, cached per
  (agent, channel), since a channel's type and members never change (#155).
- `on_token_rotated(dev_name)` closes the agent's websocket when its token changed, and the
  stream reopens it at once with the new token: revoking a token doesn't close an open
  websocket, it only stops its events (§4.8). The reconnect asks the server to resume the
  connection (`connection_id`, `sequence_number`), so events in the gap are still delivered
  while the server holds them; the doorbell's cursor replay covers the rest (R-28).
- A token retired on one server (the allowlist, #117, or a deactivated account, #127) closes
  that agent's doorbell task and stream on that server, and on no other (`close_server`,
  #155). A websocket the server closes before its first frame sends the stream to
  `GET /users/me`, whose 401 is what reaches a deactivated account's remint.
- **Cursor replay** (R-28, Story 3.2). `stream(dev_name, cursor=...)` keeps the agent's position:
  the cursor's time at start, then the newest post it yielded. On every connect after the
  first, and on the first when a cursor was given, it reads the posts created since that
  position in each of the agent's channels (`GET /users/me/teams`, then
  `/users/me/teams/{team}/channels` and `/channels/{id}/posts?since=`), oldest first, and
  yields those it hasn't yielded yet, before the live events. A post is never yielded twice:
  the stream remembers the IDs it yielded (seeded from `cursor.seen`). A replayed post has no
  `mentions` (a REST post carries none, and the text is never parsed, R-29). With no cursor,
  the first connect starts at now, less `REPLAY_MARGIN_S` for clock skew.
- **Status** (R-27). `on_status(True, "")` when a connection delivers its first frame and its
  replay has succeeded; `on_status(False, reason)` when a connect is refused (`unauthorized`
  on a 401), when the agent's own user can't be read, when a connection is lost, or when a
  replay fails (`replay failed: <the call>: <error>`). A failed replay yields nothing from that
  connection, which is closed and retried after the backoff with the position unmoved, so a
  gap is never skipped (#99). A rotation's reconnect is not an outage and reports nothing.
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
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
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

from crewchat.adapters.base import (
    NO_STATUS,
    Attachment,
    Embed,
    Forbidden,
    InboundMessage,
    NotFound,
    StatusSink,
    StreamCursor,
)
from crewchat.protocol import PROTO
from crewchat.tokens import BrokerError, Unauthorized

PING_INTERVAL_S = 30.0  # CT-07
PING_TIMEOUT_S = 30.0
BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 60.0  # CT-07
DEFAULT_READ_LIMIT = 60
REPLAY_MARGIN_S = 60.0  # a first connect's replay position: now, less this, for clock skew
RECENT_IDS = 1000  # the post IDs a stream remembers yielding, so a replay never repeats one
# Mattermost IDs and usernames, used with `fullmatch`. The client library interpolates its
# arguments into REST paths unchecked, and each path carries the agent's bot token, which the
# agent never holds (R-08). So every caller-supplied ID is shape-checked first (#62).
MM_ID_RE = re.compile(r"[a-z0-9]{26}")
MM_USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def is_mm_id(value: str) -> bool:
    return isinstance(value, str) and bool(MM_ID_RE.fullmatch(value))


def require_mm_id(value: str, what: str) -> str:
    """`value` if it is a Mattermost ID, else a `ValueError` naming `what` (`bad_request`)."""
    if not is_mm_id(value):
        raise ValueError(f"{what} must be a 26-character Mattermost ID, not {value!r}")
    return value


# A channel name `send`, `read` and `resolve` accept for an ID (#145). The URL-name test is the
# broker's (`crewbroker.membership.SLUG_RE`): only a name it matches is ever put in a REST path.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Any other name is only ever compared with display names, never put in a path, so a display
# name such as `Q&A / Support` is fine; one with a control character, or past Mattermost's
# 64-character limit, is refused before any request (#62).
CHANNEL_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
CHANNEL_TYPES = {"O", "P"}  # public and private; never a DM or group message, as the broker
CHANNEL_CACHE_S = 600.0


def keep_channel(item: Mapping[str, Any]) -> bool:
    """A public or private channel that isn't archived, as the broker's `keep_channel`."""
    return item.get("type") in CHANNEL_TYPES and not item.get("delete_at")


async def match_channel(
    name: str,
    by_slug: Callable[[], Awaitable[dict[str, Any]]],
    listing: Callable[[], Awaitable[list[dict[str, Any]]]],
) -> str:
    """The ID of the one channel whose URL name, else display name, is `name`.

    An async copy of the broker's rule (`crewbroker.membership._match`), kept here because
    the daemon never imports the broker: the URL name first, when it has the URL-name shape;
    then a casefolded display-name match over the agent's own public and private channels.
    `tests/unit/daemon/test_channel_names.py` pins both copies to the same cases. One
    difference: Mattermost's own 403 on the URL name (a private channel the agent was removed
    from) also falls through to the listing, where the broker reports an error (#148)."""
    if SLUG_RE.fullmatch(name):
        try:
            return str((await by_slug())["id"])
        except NotFoundError:
            pass  # not a URL name: maybe a display name
        except MattermostAPIError as exc:
            # A URL name the agent can't see is no different from none; anything else raises.
            if not unreadable(exc):
                raise
    wanted = name.casefold()
    found: dict[str, dict[str, Any]] = {}
    for item in await listing():
        if keep_channel(item) and str(item.get("display_name", "")).casefold() == wanted:
            found[str(item["id"])] = item
    if len(found) == 1:
        return next(iter(found))
    if not found:
        raise NotFound(f"no channel has the URL name or display name {name!r}")
    candidates = ", ".join(
        f"{c.get('display_name')!r} ({c.get('name')}, {i})" for i, c in sorted(found.items())
    )
    raise ValueError(f"{name!r} matches {len(found)} channels: {candidates}")


def require_channel_name(value: str) -> str:
    """A channel ID unchanged, else the name without one leading `#`; a `ValueError`
    (`bad_request`) for an empty name, a control character or more than 64 characters,
    before any request is made (#62, #145)."""
    if is_mm_id(value):
        return value
    name = value.removeprefix("#") if isinstance(value, str) else ""
    if not CHANNEL_NAME_RE.fullmatch(name) or not name.strip():
        raise ValueError(f"channel must be a Mattermost ID or a channel name, not {value!r}")
    return name


def require_mm_username(value: str) -> str:
    if not isinstance(value, str) or not MM_USERNAME_RE.fullmatch(value.lower()):
        raise ValueError(f"{value!r} is not a Mattermost username")
    return value.lower()

log = logging.getLogger("crewchat.adapters.mattermost")

T = TypeVar("T")
TokenFor = Callable[[str, str], str | None]


class ReplayFailed(Exception):
    """A reconnect's replay failed; the message names the call (#99)."""


class AllUnreadable(ReplayFailed):
    """Every team, or every channel, in a replay was unreadable (#103)."""


UNREADABLE = (403, 404)  # a replay scope this account can't read yet: skipped, not an outage
UNREADABLE_RETRY_S = 2.0  # the wait before retrying a replay that could read nothing


def unreadable(exc: Exception) -> bool:
    """Whether `exc` is Mattermost saying the account can't read that scope (#103), so the
    replay skips it rather than going down. Mattermost's own errors always carry an `id`; a
    proxy's 403 or 404 (an HTML page, `error_id` None) is an outage, not a scope."""
    return (
        isinstance(exc, MattermostAPIError)
        and exc.status_code in UNREADABLE
        and exc.error_id is not None
    )


def stale_channel(exc: Exception) -> bool:
    """Whether a cached channel ID may be gone (#148): Mattermost's own 403 or 404, as for a
    replay scope. A proxy's page is an outage, never a reason to resolve again."""
    return unreadable(exc)


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


def post_kiosk_answers(props: Any) -> str:
    """A kiosk answer's `props.crew_kiosk_answers`: the question's post ID, or "" (#26)."""
    if not post_kiosk(props):
        return ""
    value = props.get("crew_kiosk_answers")
    return value if isinstance(value, str) and is_mm_id(value) else ""


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
    last_error: str = ""  # why the last connect failed, for `on_status`
    position_ms: int | None = None  # replay from here on a reconnect (R-28)
    recent: OrderedDict[str, None] = field(default_factory=OrderedDict)  # IDs yielded


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
        monotonic: Callable[[], float] = time.monotonic,
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
        # (Dev-Name, channel ID) -> a DM channel with that agent (#155). A channel's type and
        # members never change, so it never expires.
        self._dm_channels: dict[tuple[str, str], bool] = {}
        # (dev_name, name as given) -> (channel ID, expiry): per agent, as each resolves as
        # itself, so one agent never learns a private channel's ID from another's lookup (#145).
        self._channels: dict[tuple[str, str], tuple[str, float]] = {}
        self._monotonic = monotonic

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
            kiosk_answers=post_kiosk_answers(props),
            dm=dm,
            author_resolved=bool(user),
        )

    async def _pull_dm(
        self, dev_name: str, client: MattermostClient, channel_id: str, me: str
    ) -> bool:
        """Is `channel_id` a DM channel one of whose two members is the agent `me` (#155)? On
        pull, its own posts there count too. A failed lookup is False, and asked again."""
        key = (dev_name, channel_id)
        held = self._dm_channels.get(key)
        if held is not None:
            return held
        try:
            channel = await client.get_channel(channel_id)
        except AuthenticationError:
            raise  # the store's refresh path
        except MattermostAPIError as exc:
            log.warning("channel %s on %s: %s", channel_id, self.server_id, exc)
            return False
        members = str(channel.get("name", "")).split("__")
        dm = channel.get("type") == "D" and len(members) == 2 and me in members
        self._dm_channels[key] = dm
        return dm

    async def _messages(
        self, client: MattermostClient, result: Mapping[str, Any], *, dev_name: str, me: str
    ) -> list[InboundMessage]:
        """A `{posts, order}` list, oldest first, each with its pull `dm` (#155)."""
        posts = result.get("posts") or {}
        if not isinstance(posts, dict):
            return []
        ordered = sorted(posts.values(), key=lambda p: (p.get("create_at", 0), p.get("id", "")))
        return [
            await self.message(
                client,
                p,
                dm=await self._pull_dm(dev_name, client, str(p.get("channel_id", "")), me),
            )
            for p in ordered
        ]

    # -- channel names (#145) ---------------------------------------------------------------
    async def _channel_id(self, dev_name: str, client: MattermostClient, channel: str) -> str:
        """`channel` as an ID: an ID unchanged, with no request; else a URL name or display
        name, with or without `#`, resolved as the agent in its one team on this server."""
        if is_mm_id(channel):
            return channel
        name = require_channel_name(channel)
        # Keyed on the name as given: `standup` (a URL name) and `Standup` (a display name)
        # can be two channels.
        key = (dev_name, name)
        held = self._channels.get(key)
        if held is not None and held[1] > self._monotonic():
            return held[0]
        teams = await client.get_teams()
        if not teams:
            raise NotFound(f"the agent is on no team on {self.server_id}")
        if len(teams) > 1:
            # No crew_info team to choose by (#145): the caller names the channel by ID.
            names = ", ".join(sorted(str(t.get("name", t.get("id"))) for t in teams))
            raise ValueError(
                f"the agent is on {len(teams)} teams on {self.server_id}: {names}; "
                "pass the channel ID"
            )
        team_id = str(teams[0]["id"])
        found = await match_channel(
            name,
            lambda: client.get_channel_by_name(team_id, name),
            lambda: client.get_my_channels(team_id),
        )
        self._channels[key] = (found, self._monotonic() + CHANNEL_CACHE_S)
        return found

    def _forget_channel(self, dev_name: str, channel: str, channel_id: str) -> None:
        """A resolved channel's ID that 404s, or doesn't hold the post asked for: drop it, so
        the next call resolves again."""
        if channel_id != channel:
            self._channels.pop((dev_name, channel.removeprefix("#")), None)

    async def _on_channel(
        self,
        dev_name: str,
        client: MattermostClient,
        channel: str,
        op: Callable[[str], Awaitable[T]],
    ) -> T:
        """`op` on `channel` as an ID. When a name's ID answers Mattermost's own 403 or 404, the
        entry is dropped and the name resolved once more. A name that no longer resolves (a
        channel deleted, or a private one the agent left) is `NotFound`. One that resolves to a
        new ID is retried there once, which is safe because the refused request made nothing.
        One that resolves to the same ID was refused for another reason, such as a deleted
        thread root or a channel the agent may not post in, so that error is raised as it came.
        An ID the caller gave is passed through, its errors raised as they came."""
        channel_id = await self._channel_id(dev_name, client, channel)
        try:
            return await op(channel_id)
        except MattermostAPIError as exc:
            if channel_id == channel or not stale_channel(exc):
                raise
            refused = exc
        self._forget_channel(dev_name, channel, channel_id)
        fresh = await self._channel_id(dev_name, client, channel)  # NotFound when it's gone
        if fresh == channel_id:
            raise refused
        try:
            return await op(fresh)
        except MattermostAPIError as exc:
            if stale_channel(exc):
                self._forget_channel(dev_name, channel, fresh)
            raise

    async def resolve(
        self, dev_name: str, name: str, type: str = "channel", guild_id: str | None = None
    ) -> dict[str, Any]:
        """`resolve` on Mattermost: channels only, and no guild (#145). `chat.resolve` answers
        `unsupported` for any other type before it gets here."""
        if type != "channel":
            raise ValueError(f"{self.name} resolves only channels, not {type!r}")
        if guild_id is not None:
            raise ValueError("guild_id is Discord's; a Mattermost channel resolves in its team")
        require_channel_name(name)

        async def request(client: MattermostClient) -> dict[str, Any]:
            channel_id = await self._channel_id(dev_name, client, name)
            return {"id": channel_id, "name": name.removeprefix("#"), "type": "channel"}

        return await self._call(dev_name, request)

    # -- the contract -----------------------------------------------------------------------
    async def _post(
        self,
        client: MattermostClient,
        channel: str,
        text: str,
        thread: str | None,
        attachments: Sequence[Attachment],
        embed: Embed | None,
        *,
        kiosk: bool = False,
        answers: str = "",
    ) -> InboundMessage:
        """Upload `attachments` with the agent's own token (`POST /api/v4/files`), then post
        with their `file_ids`; `embed` is a message attachment (CT-01). A kiosk answer names
        the question it `answers` (#26)."""
        file_ids: list[str] = []
        for att in attachments:
            uploaded = await client.upload_file(channel, str(att.path), filename=att.name)
            infos = uploaded.get("file_infos") or []
            if not infos or not isinstance(infos[0], dict) or "id" not in infos[0]:
                raise MattermostAPIError(f"upload of {att.name} returned no file id")
            file_ids.append(str(infos[0]["id"]))
        props: dict[str, Any] = {"crew_proto": PROTO}
        if kiosk:
            props["crew_kiosk"] = 1  # outsider content to every admit (§5.9, §5.11)
            if answers:
                props["crew_kiosk_answers"] = answers
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
        require_channel_name(channel)  # before any request (#62)

        async def request(client: MattermostClient) -> InboundMessage:
            return await self._on_channel(
                dev_name,
                client,
                channel,
                lambda cid: self._post(client, cid, text, thread, attachments, embed),
            )

        return await self._call(dev_name, request)

    async def send_kiosk(
        self, dev_name: str, channel: str, text: str, thread: str, *, answers: str = ""
    ) -> InboundMessage:
        """The kiosk's answer, as `dev_name`, in `thread`: `props.crew_kiosk=1` (Story 3.5),
        and `props.crew_kiosk_answers=<answers>`, the question's post ID (#26)."""
        require_channel_name(channel)

        async def request(client: MattermostClient) -> InboundMessage:
            return await self._on_channel(
                dev_name,
                client,
                channel,
                lambda cid: self._post(
                    client, cid, text, thread, (), None, kiosk=True, answers=answers
                ),
            )

        return await self._call(dev_name, request)

    async def read_one(self, dev_name: str, channel: str, post_id: str) -> InboundMessage:
        """One post, `GET /api/v4/posts/{id}`; one in another channel is not found."""
        require_mm_id(post_id, "post_id")
        if channel:
            require_channel_name(channel)
        me = await self.user_id(dev_name)

        async def request(client: MattermostClient) -> InboundMessage:
            channel_id = await self._channel_id(dev_name, client, channel) if channel else ""
            try:
                post = await client.get_post(post_id)
            except NotFoundError as exc:
                raise NotFound(f"post {post_id}") from exc
            if channel_id and str(post.get("channel_id", "")) != channel_id:
                self._forget_channel(dev_name, channel, channel_id)
                raise NotFound(f"post {post_id} in {channel}")
            dm = await self._pull_dm(dev_name, client, str(post.get("channel_id", "")), me)
            return await self.message(client, post, dm=dm)

        return await self._call(dev_name, request)

    async def has_post(self, dev_name: str, post_id: str) -> bool:
        """`GET /api/v4/posts/{id}` as the agent answers 200 (R-12's probe)."""
        if not is_mm_id(post_id):
            return False  # never a request for an ID of the wrong shape (#62)

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
        require_channel_name(channel)  # before any request (#62)

        async def fetch(client: MattermostClient, channel_id: str) -> dict[str, Any]:
            if since is not None:
                return await client.get_posts_since(channel_id, int(since * 1000))
            return await client.get_posts(channel_id, per_page=limit or DEFAULT_READ_LIMIT)

        me = await self.user_id(dev_name)

        async def request(client: MattermostClient) -> list[InboundMessage]:
            result = await self._on_channel(
                dev_name, client, channel, lambda cid: fetch(client, cid)
            )
            return await self._messages(client, result, dev_name=dev_name, me=me)

        messages = await self._call(dev_name, request)
        return messages[-limit:] if limit else messages

    async def thread(self, dev_name: str, post_id: str) -> list[InboundMessage]:
        require_mm_id(post_id, "thread")
        me = await self.user_id(dev_name)

        async def request(client: MattermostClient) -> list[InboundMessage]:
            result = await client.get_thread(post_id)
            return await self._messages(client, result, dev_name=dev_name, me=me)

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
        name = to.removeprefix("@")
        user_id = name if is_mm_id(name) else None
        username = None if user_id else require_mm_username(name)  # before any request (#62)
        me = await self.user_id(dev_name)

        async def request(client: MattermostClient) -> InboundMessage:
            try:
                other = user_id or str((await client.get_user_by_username(str(username)))["id"])
            except NotFoundError as exc:
                raise NotFound(f"user {username!r}") from exc
            channel = await client.create_direct_channel([me, other])
            return await self._post(client, str(channel["id"]), text, None, attachments, embed)

        return await self._call(dev_name, request)

    async def react(self, dev_name: str, post_id: str, emoji: str) -> None:
        async def request(client: MattermostClient) -> None:
            await client.add_reaction(post_id, emoji.strip(":"))

        await self._call(dev_name, request)

    async def edit(
        self, dev_name: str, post_id: str, text: str, *, embed: Embed | None = None
    ) -> InboundMessage:
        """`PUT /api/v4/posts/{id}/patch` as the agent: its own post only (#149). The server
        enforces the author too; the read first gives `forbidden` its own name. An `embed`
        replaces the post's `props.attachments`, and its other props are kept."""
        require_mm_id(post_id, "post_id")
        me = await self.user_id(dev_name)

        async def request(client: MattermostClient) -> InboundMessage:
            try:
                post = await client.get_post(post_id)
            except NotFoundError as exc:
                raise NotFound(f"post {post_id}") from exc
            except MattermostAPIError as exc:
                if exc.status_code == 403:  # a channel the agent isn't in: not its post
                    raise Forbidden("not your post") from exc
                raise
            if str(post.get("user_id", "")) != me:
                raise Forbidden("not your post")
            body: dict[str, Any] = {"message": text}
            if embed is not None:
                props = post.get("props")
                body["props"] = {
                    **(props if isinstance(props, dict) else {}),
                    "attachments": [embed_attachment(embed)],
                }
            try:
                edited = await client.put(f"/posts/{post_id}/patch", json=body)
            except NotFoundError as exc:
                raise NotFound(f"post {post_id}") from exc
            except MattermostAPIError as exc:
                if exc.status_code == 403:
                    raise Forbidden("not your post") from exc
                raise
            return await self.message(client, edited if isinstance(edited, dict) else post)

        return await self._call(dev_name, request)

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
            agent.last_error = "no token"
            return None
        try:
            ws = await self._connect(
                self.ws_uri(agent),
                headers={"Authorization": f"Bearer {token}"},
                ping_interval=self.ping_interval_s,
            )
        except InvalidStatus as exc:
            if exc.response.status_code == 401:
                agent.last_error = "unauthorized"
                if self.refresher is not None:
                    log.info("websocket 401 for %s on %s: refreshing", dev_name, self.server_id)
                    with suppress(Exception):
                        await self.refresher.refresh(dev_name, self.server_id, token)
            else:
                agent.last_error = f"connect refused: HTTP {exc.response.status_code}"
                log.warning("websocket %s on %s: %s", dev_name, self.server_id, exc)
            return None
        except (OSError, TimeoutError, WebSocketException) as exc:
            log.warning("websocket %s on %s: %s", dev_name, self.server_id, exc)
            agent.last_error = f"connect failed: {exc}"
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

    def _fresh(self, agent: _Agent, msg: InboundMessage) -> bool:
        """True the first time the stream sees `msg` (and remember it); False for a repeat."""
        if msg.post_id in agent.recent:
            return False
        agent.recent[msg.post_id] = None
        while len(agent.recent) > RECENT_IDS:
            agent.recent.popitem(last=False)
        ts_ms = int(msg.ts * 1000)
        if agent.position_ms is None or ts_ms > agent.position_ms:
            agent.position_ms = ts_ms
        return True

    async def _replay(self, dev_name: str, agent: _Agent) -> list[InboundMessage]:
        """The posts created since the agent's position in each of its channels, oldest
        first, that the stream hasn't yielded (R-28). A failure raises `ReplayFailed`, naming
        the call that failed, and the position is left where it was (#99). A team or channel
        that Mattermost says is unreadable (403 or 404) is skipped with a warning instead. When
        every team, or every channel, is skipped, the replay is retried once and then raises
        `AllUnreadable` (#103)."""
        since = agent.position_ms
        if since is None:
            return []
        me = agent.user_id
        step = "GET /users/me/teams"

        async def scoped(call: Awaitable[T], skipped: list[str]) -> T | None:
            try:
                return await call
            except MattermostAPIError as exc:
                if not unreadable(exc):
                    raise
                log.warning("replay for %s skips %s: %s", dev_name, step, exc)
                skipped.append(f"{step} {exc}")
                return None

        def all_skipped(tried: int, skipped: list[str]) -> None:
            if tried and len(skipped) == tried:
                raise AllUnreadable(f"every replay scope unreadable: {'; '.join(skipped)}")

        async def request(client: MattermostClient) -> list[InboundMessage]:
            nonlocal step
            step = "GET /users/me/teams"
            channels: dict[str, dict[str, Any]] = {}
            teams_tried = 0
            teams_skipped: list[str] = []
            for team in await client.get_teams():
                team_id = str(team.get("id", ""))
                if not is_mm_id(team_id):
                    continue
                step = f"GET /users/me/teams/{team_id}/channels"
                teams_tried += 1
                team_channels = await scoped(client.get_my_channels(team_id), teams_skipped)
                for channel in team_channels or []:
                    channel_id = str(channel.get("id", ""))
                    if is_mm_id(channel_id):
                        channels[channel_id] = channel
            all_skipped(teams_tried, teams_skipped)
            found: list[InboundMessage] = []
            chans_skipped: list[str] = []
            for channel_id, chan in channels.items():
                # `since` is `update_at > since`, so one millisecond back keeps a post made
                # at the position itself; the IDs already yielded are left out below.
                step = f"GET /channels/{channel_id}/posts?since="
                result = await scoped(client.get_posts_since(channel_id, since - 1), chans_skipped)
                if result is None:
                    continue
                posts = result.get("posts") or {}
                if not isinstance(posts, dict):
                    continue
                for post in posts.values():
                    if not isinstance(post, dict):
                        continue
                    create_at = post.get("create_at")
                    if not isinstance(create_at, int | float) or create_at < since:
                        continue  # an older post, edited or replied to since
                    if post.get("delete_at") or str(post.get("id", "")) in agent.recent:
                        continue
                    author = str(post.get("user_id", ""))
                    if me is not None and author == me:
                        continue  # echo, by user ID only (R-04)
                    data = {
                        "channel_type": chan.get("type"),
                        "channel_name": chan.get("name", ""),
                    }
                    dm = event_dm(data, author, me)
                    step = f"GET /users/{author}"
                    found.append(await self.message(client, post, [], dm=dm))
            all_skipped(len(channels), chans_skipped)
            found.sort(key=lambda m: (m.ts, m.post_id))
            return found

        async def attempt() -> list[InboundMessage]:
            try:
                return await self._call(dev_name, request)
            except (KeyError, MattermostAPIError, Unauthorized, httpx.HTTPError) as exc:
                log.error("replay for %s on %s: %s: %s", dev_name, self.server_id, step, exc)
                raise ReplayFailed(f"{step}: {exc}") from exc

        # Every scope unreadable is an account problem, or a new account whose membership
        # hasn't reached the server yet: one retry tells the two apart.
        try:
            return await attempt()
        except AllUnreadable as exc:
            log.warning("replay for %s on %s: %s; retrying", dev_name, self.server_id, exc)
        await self._sleep(UNREADABLE_RETRY_S)
        try:
            return await attempt()
        except AllUnreadable as exc:
            log.error("replay for %s on %s: %s", dev_name, self.server_id, exc)
            raise

    async def stream(
        self,
        dev_name: str,
        *,
        cursor: StreamCursor | None = None,
        on_status: StatusSink = NO_STATUS,
    ) -> AsyncIterator[InboundMessage]:
        """`dev_name`'s inbound posts, reconnecting until `close_agent` (CT-07), replayed
        from `cursor` on each reconnect (R-28), its connection state sent to `on_status`."""
        agent = self._agent(dev_name)
        agent.closed = False
        if cursor is not None:
            agent.recent = OrderedDict.fromkeys(sorted(cursor.seen | {cursor.id} - {""}))
            agent.position_ms = int(cursor.ts * 1000) if cursor.ts > 0 else None
        delays = backoff_delays()
        while not agent.closed:
            replay_failed = False
            if agent.user_id is None:
                try:
                    await self.user_id(dev_name)
                except (
                    KeyError,
                    MattermostAPIError,
                    Unauthorized,
                    BrokerError,
                    httpx.HTTPError,
                ) as exc:
                    log.warning("stream %s: who am I: %s", dev_name, exc)
                    reason = "unauthorized" if isinstance(exc, Unauthorized) else str(exc)
                    on_status(False, f"cannot read the agent's user: {reason}")
                    if agent.closed:  # a `deactivated` remint retired the token (#155)
                        break
                    await self._sleep(next(delays))
                    continue
            ws = await self._open(dev_name, agent)
            if ws is None:
                on_status(False, agent.last_error or "connect failed")
                if agent.closed:  # a 401's remint retired the token (#155)
                    break
                await self._sleep(next(delays))
                continue
            try:
                while True:
                    # A dead connection is the library's to notice: its pings time out.
                    raw = await ws.recv()
                    first = not agent.hello
                    msg = await self._event(dev_name, agent, raw)
                    agent.hello = True
                    if first:
                        if agent.position_ms is None:
                            agent.position_ms = int((time.time() - REPLAY_MARGIN_S) * 1000)
                            missed_posts: list[InboundMessage] = []
                        else:
                            # Caught up before it counts as up, as squawkie's catch-up is. A
                            # failed replay yields nothing from this connection, not even
                            # `msg`, so the position stays before the gap (R-27, R-28, #99).
                            try:
                                missed_posts = await self._replay(dev_name, agent)
                            except ReplayFailed as exc:
                                on_status(False, f"replay failed: {exc}")
                                replay_failed = True
                                break
                        delays = backoff_delays()  # connected: the next loss starts at 1 s
                        on_status(True, "")
                        for missed in missed_posts:
                            if self._fresh(agent, missed):
                                yield missed
                    if msg is not None and self._fresh(agent, msg):
                        yield msg
            except (ConnectionClosed, TimeoutError, OSError) as exc:
                if not agent.rotated and not agent.closed:
                    log.warning("websocket %s on %s lost: %s", dev_name, self.server_id, exc)
                    on_status(False, f"connection lost: {exc}")
                    if not agent.hello:
                        # Closed before its first frame, as for a deactivated account: the
                        # next pass asks `GET /users/me`, whose 401 refreshes, and a
                        # `deactivated` remint retires the token and closes this stream (#155).
                        agent.user_id = None
            finally:
                agent.ws = None
                with suppress(Exception):
                    await ws.close()
            if agent.closed:
                break
            if agent.rotated:
                agent.rotated = False  # a new token: reconnect at once
                if not replay_failed:  # a replay that failed waits out its backoff
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

    async def close_server(self, dev_name: str, server_id: str) -> None:
        """`dev_name`'s token on `server_id` was retired (#155). One backend is one server, so
        on this one it is `close_agent`; the store never closes another server's stream."""
        if server_id == self.server_id:
            await self.close_agent(dev_name)

    async def aclose(self) -> None:
        for dev_name in list(self._agents):
            await self.close_agent(dev_name)
        for dev_name in list(self._clients):
            await self._clients.pop(dev_name).stack.aclose()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
