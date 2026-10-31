"""The contract every chat backend adapter implements (Dev Spec §5.4, §5.9; Stories 2.1, 2.4c).

One daemon holds every agent on its host, so each operation names the agent it acts as
(`dev_name`). The adapter turns that into the agent's own credential for its backend: on
Mattermost, the token the store holds for (dev_name, server_id).

`InboundMessage` is what every backend hands the daemon, from a pull (`read`, `thread`) or the
push stream (`stream`). Its `proto` is the sender's protocol marker, 0 when the message has
none (R-14). `dm` feeds addressing (R-19, Story 2.3). `is_bot` and `kiosk` feed §5.9's
membership rule. Ownership is not here: a bot token can't read another bot's owner, so
ownership comes from the brokers' `crew_info.minted_bot_ids` (D-100).

`send` and `dm` take `attachments` (files the shim streamed through `upload`, held by the
daemon) and `embed` (today's `"title:body"`, parsed by `parse_embed`). Neither is ever
silently dropped (R-15): a backend delivers both or raises.

Two optional capabilities sit beside the contract: `GuildBackend` (Discord's channel listing,
name resolution and creation, Story 2.4c) and `PostProbe` (a Mattermost server answering
whether it holds a post, for `thread_backend`, R-12).

**The doorbell's stream hooks** (Story 3.2; R-27, R-28). `stream` takes two keyword-only
arguments from the doorbell:

- `cursor`: a `StreamCursor`, the last event the agent was handed from this backend. On every
  (re)connect the stream replays what it missed after that point, and leaves out the IDs in
  `cursor.seen` (R-28). With no cursor the stream starts at now.
- `on_status`: a `StatusSink`, called `(True, "")` when the stream is connected and
  `(False, reason)` when it is not: a lost connection, a refused connect, or an auth error
  (R-27). The stream keeps retrying on its own; it reports, the doorbell decides what is loud.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

EMBED_TITLE_MAX = 256  # Discord's embed title limit
EMBED_BODY_MAX = 4096  # Discord's embed description limit


@dataclass(frozen=True)
class InboundMessage:
    backend: str  # e.g. "mattermost:<server_id>" or "discord"
    channel: str
    post_id: str
    root_id: str  # "" when the message is not a reply
    author_id: str
    author_name: str
    text: str
    mentions: list[str] = field(default_factory=list)  # the server's own markers (R-29)
    proto: int = 0  # the sender's protocol marker; 0 when absent (R-14)
    ts: float = 0.0  # creation time, seconds since the epoch
    is_bot: bool = False  # the author's user object `is_bot`, never a post prop
    kiosk: bool = False  # the post carries `props.crew_kiosk == 1` (§5.9)
    # A kiosk answer's `props.crew_kiosk_answers`: the question's post ID (#26).
    kiosk_answers: str = ""
    # On push, a DM between the author and the receiving agent (R-19). On a Mattermost pull
    # (`read`, `thread`), a post in a DM channel one of whose two members is the reading
    # agent, its own posts included (#155). Always false on Discord. Only push addressing
    # reads it (R-21).
    dm: bool = False
    # The server resolved the author's user object. False when it couldn't, and the filters
    # then drop the message as unresolvable (R-22).
    author_resolved: bool = True


@dataclass(frozen=True)
class StreamCursor:
    """Where a backend's stream resumes for one agent (R-28): the last event handed to the
    agent (`id`, at `ts` seconds since the epoch) and the IDs recently handed over (`seen`),
    which a replay never repeats."""

    id: str = ""
    ts: float = 0.0
    seen: frozenset[str] = frozenset()


# `(True, "")` when the stream is connected, `(False, reason)` when it is not (R-27).
StatusSink = Callable[[bool, str], None]


def _no_status(up: bool, reason: str) -> None:
    """The default `on_status`: nobody is listening."""


NO_STATUS: StatusSink = _no_status


class BackendUnavailable(Exception):
    """The backend can't serve this call now (`unavailable: <why>` to the caller), e.g. a
    Discord read while scream-hole is unhealthy (D-106)."""


class NotFound(Exception):
    """What the call names doesn't exist on the backend (`not_found: <what>` to the caller)."""


class Forbidden(Exception):
    """The agent may not do this to what the call names, e.g. edit another agent's post
    (`forbidden: <why>` to the caller, #149)."""


@dataclass(frozen=True)
class Attachment:
    """A file the daemon holds for one post: its name as posted, and the daemon's own copy."""

    name: str
    path: Path
    size: int

    def read(self) -> bytes:
        return self.path.read_bytes()


@dataclass(frozen=True)
class Embed:
    """Today's `disc_send` embed: `"title:body"`, split on the first colon."""

    title: str
    body: str


def parse_embed(raw: Any) -> Embed:
    """`"title:body"` split on the first colon; no colon is a title alone, as today.

    Malformed (a `ValueError`, so `bad_request`): not a string, an empty title, or a part
    longer than Discord's embed limits.
    """
    if not isinstance(raw, str):
        raise ValueError("embed is a string, title:body")
    title, _, body = raw.partition(":")
    title, body = title.strip(), body.strip()
    if not title:
        raise ValueError(f"embed {raw[:40]!r} has no title (title:body)")
    if len(title) > EMBED_TITLE_MAX or len(body) > EMBED_BODY_MAX:
        raise ValueError(
            f"embed title is at most {EMBED_TITLE_MAX} characters, body {EMBED_BODY_MAX}"
        )
    return Embed(title=title, body=body)


class ChatBackend(Protocol):
    """send, read, thread, dm, react and the push stream, acting as one agent at a time."""

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
        """Post `text` in `channel` (a reply when `thread` names a root post)."""

    async def read(
        self, dev_name: str, channel: str, since: float | None = None, limit: int | None = None
    ) -> list[InboundMessage]:
        """Recent messages in `channel`, oldest first."""

    async def read_one(self, dev_name: str, channel: str, post_id: str) -> InboundMessage:
        """The one message `post_id` in `channel`."""

    async def thread(self, dev_name: str, post_id: str) -> list[InboundMessage]:
        """Every message in the thread `post_id` belongs to, oldest first."""

    async def dm(
        self,
        dev_name: str,
        to: str,
        text: str,
        *,
        attachments: Sequence[Attachment] = (),
        embed: Embed | None = None,
    ) -> InboundMessage:
        """A direct message to the user `to`."""

    async def react(self, dev_name: str, post_id: str, emoji: str) -> None:
        """Add `emoji` to `post_id`."""

    async def edit(
        self, dev_name: str, post_id: str, text: str, *, embed: Embed | None = None
    ) -> InboundMessage:
        """Rewrite `dev_name`'s own post `post_id` to `text` (and `embed`, when given). Another
        agent's post is `Forbidden`, a gone one `NotFound` (#149)."""

    def stream(
        self,
        dev_name: str,
        *,
        cursor: StreamCursor | None = None,
        on_status: StatusSink = NO_STATUS,
    ) -> AsyncIterator[InboundMessage]:
        """Messages as they arrive for `dev_name`, its own posts already dropped (R-04);
        replayed from `cursor` on every (re)connect, with the connection's state reported to
        `on_status` (R-27, R-28)."""
        ...


@runtime_checkable
class GuildBackend(Protocol):
    """Discord's guild operations, today's `disc_list`, `disc_resolve`, `disc_create_channel`
    and `disc_create_thread` (Story 2.4c). `guild_id`, when given, must be the configured one."""

    async def list_channels(
        self, dev_name: str, type: str = "text", guild_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def resolve(
        self, dev_name: str, name: str, type: str = "channel", guild_id: str | None = None
    ) -> dict[str, Any]: ...

    async def create_channel(
        self, dev_name: str, name: str, type: str = "text", guild_id: str | None = None
    ) -> dict[str, Any]: ...

    async def create_thread(self, dev_name: str, channel: str, name: str) -> dict[str, Any]: ...


@runtime_checkable
class ChannelResolver(Protocol):
    """A backend that resolves a channel name and has no other guild op: Mattermost (#145)."""

    async def resolve(
        self, dev_name: str, name: str, type: str = "channel", guild_id: str | None = None
    ) -> dict[str, Any]: ...


@runtime_checkable
class PostProbe(Protocol):
    """A Mattermost server answering whether it holds a post, as the agent (R-12)."""

    async def has_post(self, dev_name: str, post_id: str) -> bool: ...
