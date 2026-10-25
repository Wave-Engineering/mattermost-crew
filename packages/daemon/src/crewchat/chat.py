"""The chat ops: `send`, `dm`, `read`, `thread` and `react`; Discord's guild ops
`list_channels`, `resolve`, `create_channel` and `create_thread`; `upload` and
`thread_backend`; plus the shim's pushes `kiosk_brief` and `squawk_ids` (Dev Spec §5.4;
Stories 2.4, 2.4c).

Every chat frame names the agent it acts as (`dev_name`) and the backend it goes to
(`backend`). A backend has **one name form** on the socket and in every file (§5.4):
`mattermost:<server_id>` for a Mattermost server, and `discord` for the host's one guild
(`discord:<guild_id>` is reserved). A bare server id is `bad_request`; nothing translates
between forms. The thin MCP shim resolves the backend before it sends the frame (R-11); the
daemon does no default resolution. A frame the shim marks `off_default` (the call named a
backend other than the agent's default) is logged as `off-default: <dev> <backend>` (R-13).

- **Attachments** (R-15). The daemon never reads an agent's path (§5.3): the shim streams the
  file through `upload` (`crewchat.uploads`), and `send`/`dm` name the result in
  `attachments: [upload_id]`. Each upload is deleted once its post was tried.
- **Embeds** (R-15). `send`/`dm` take `embed`, today's `"title:body"`; a malformed one is
  `bad_request`. Neither an attachment nor an embed is ever silently dropped.
- **`thread_backend`** (R-12) answers which backend a post's thread is on, so a reply follows
  it after a restart of either side: the ID's shape decides the kind (a 17-20 digit snowflake
  is `discord`, a 26-character lowercase id is Mattermost); for Mattermost, the agent's
  persisted thread set picks the server, and otherwise a `GET /api/v4/posts/{id}` as the agent
  on **only** the servers it is registered on. Nothing found is `not_found`.
- The guild ops are Discord's (`GuildBackend`); a Mattermost backend answers `unsupported`.

The daemon never reads a file on the agent's side (§5.3). What it knows about the agent's seed
brief and its squawks is what the shim pushed: `AgentContext` keeps it for the doorbell and the
kiosk (Phase 3).

The pull path's switch filter (R-21) arrives with Story 3.3; until then `read` and `thread`
return what the adapter returned.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from crewchat.adapters.base import (
    Attachment,
    BackendUnavailable,
    ChatBackend,
    Embed,
    GuildBackend,
    InboundMessage,
    NotFound,
    PostProbe,
    parse_embed,
)
from crewchat.registry import Registration, Registry
from crewchat.server import Handler, OpError
from crewchat.threads import MyThreads
from crewchat.uploads import UploadError, UploadStore

MAX_BRIEF_CHARS = 32 * 1024
MAX_SQUAWK_IDS = 1000
MATTERMOST_PREFIX = "mattermost:"
DISCORD = "discord"
BACKEND_NAME_RE = re.compile(r"^(discord|mattermost:[a-z0-9._-]+)$")
DISCORD_ID_RE = re.compile(r"^\d{17,20}$")  # a snowflake (D-106)
MATTERMOST_ID_RE = re.compile(r"^[a-z0-9]{26}$")  # Mattermost's 26-character base32 id

log = logging.getLogger("crewchat.chat")


@dataclass
class AgentContext:
    """What the shims pushed, by Dev-Name: the `## Kiosk` brief and the squawk ids."""

    briefs: dict[str, str] = field(default_factory=dict)
    squawk_ids: dict[str, list[str]] = field(default_factory=dict)


def check_backend_name(name: str) -> str:
    """`name` in the one form (§5.4), or `bad_request` saying why not."""
    if BACKEND_NAME_RE.match(name):
        return name
    if name.startswith("discord:"):
        raise OpError(
            "bad_request", f"backend {name!r}: discord:<guild_id> is reserved; use discord"
        )
    raise OpError(
        "bad_request",
        f"backend {name!r} is not mattermost:<server_id> or discord (a bare server id is "
        "not a backend name)",
    )


def post_kind(post_id: str) -> str:
    """`discord` or `mattermost` by the ID's shape (D-106); any other shape is `bad_request`."""
    if DISCORD_ID_RE.match(post_id):
        return DISCORD
    if MATTERMOST_ID_RE.match(post_id):
        return "mattermost"
    raise OpError(
        "bad_request",
        f"post id {post_id!r} is neither a Discord snowflake nor a Mattermost id",
    )


def message_json(msg: InboundMessage) -> dict[str, Any]:
    return dataclasses.asdict(msg)


def _str(args: Mapping[str, Any], key: str, *, required: bool = True) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise OpError("bad_request", f"{key} is a required string")
    return value


def _text(args: Mapping[str, Any]) -> str:
    value = args.get("text")
    if not isinstance(value, str) or not value.strip():
        raise OpError("bad_request", "text is a required non-empty string")
    return value


def _embed(args: Mapping[str, Any]) -> Embed | None:
    raw = args.get("embed")
    if raw is None:
        return None
    try:
        return parse_embed(raw)
    except ValueError as exc:
        raise OpError("bad_request", str(exc)) from exc


def build_chat_handlers(
    registry: Registry,
    backends: Mapping[str, ChatBackend],
    threads: MyThreads,
    context: AgentContext,
    uploads: UploadStore | None = None,
) -> dict[str, Handler]:
    """The chat ops over `backends`, keyed by backend name. Without `uploads`, `upload` and
    `attachments` are refused (`unsupported`), never dropped."""

    def registered(args: Mapping[str, Any]) -> tuple[str, Registration]:
        dev_name = _str(args, "dev_name")
        assert dev_name is not None
        reg = registry.get(dev_name)
        if reg is None:
            raise OpError("not_registered", dev_name)
        return dev_name, reg

    def agent(args: Mapping[str, Any]) -> tuple[str, Registration, str, ChatBackend]:
        backend_name = _str(args, "backend")
        assert backend_name is not None
        check_backend_name(backend_name)
        dev_name, reg = registered(args)
        backend = backends.get(backend_name)
        if backend is None:
            raise OpError("unknown_backend", backend_name)
        if backend_name not in reg.backends:
            raise OpError("not_on_backend", f"{dev_name} is not registered on {backend_name}")
        if args.get("off_default") is True:
            log.info("off-default: %s %s", dev_name, backend_name)
        return dev_name, reg, backend_name, backend

    def guild(backend_name: str, backend: ChatBackend, op: str) -> GuildBackend:
        if not isinstance(backend, GuildBackend):
            raise OpError("unsupported", f"{backend_name} has no {op}")
        return backend

    def backend_error(backend_name: str, op: str, exc: Exception) -> OpError:
        if isinstance(exc, OpError):
            return exc
        if isinstance(exc, BackendUnavailable):
            return OpError("unavailable", str(exc))
        if isinstance(exc, NotFound):
            return OpError("not_found", f"{backend_name}: {exc}")
        if isinstance(exc, UploadError):
            return OpError(exc.code, exc.detail)
        if isinstance(exc, ValueError | KeyError):
            return OpError("bad_request", f"{backend_name}: {exc}")
        log.error("%s on %s failed: %s: %s", op, backend_name, type(exc).__name__, exc)
        return OpError("backend_error", f"{backend_name}: {type(exc).__name__}")

    def take_attachments(dev_name: str, args: Mapping[str, Any]) -> list[Attachment]:
        ids = args.get("attachments")
        if ids is None:
            return []
        if uploads is None:
            raise OpError("unsupported", "this daemon holds no uploads; attachments refused")
        try:
            return uploads.take(dev_name, ids)
        except UploadError as exc:
            raise OpError(exc.code, exc.detail) from exc

    def rich(attachments: list[Attachment], embed: Embed | None) -> dict[str, Any]:
        """Only what the call carries, so a backend without either keeps its plain call."""
        extra: dict[str, Any] = {}
        if attachments:
            extra["attachments"] = attachments
        if embed is not None:
            extra["embed"] = embed
        return extra

    def release(attachments: list[Attachment]) -> None:
        if uploads is not None and attachments:
            uploads.release(attachments)

    async def send(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        to, text = _str(args, "to"), _text(args)
        thread = _str(args, "thread", required=False)
        assert to is not None
        embed = _embed(args)
        attachments = take_attachments(dev_name, args)
        try:
            msg = await backend.send(dev_name, to, text, thread, **rich(attachments, embed))
        except Exception as exc:
            raise backend_error(backend_name, "send", exc) from exc
        finally:
            release(attachments)
        root = threads.record(dev_name, msg)
        return {"backend": backend_name, "post_id": msg.post_id, "thread": root}

    async def dm(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        to, text = _str(args, "to"), _text(args)
        assert to is not None
        embed = _embed(args)
        attachments = take_attachments(dev_name, args)
        try:
            msg = await backend.dm(dev_name, to, text, **rich(attachments, embed))
        except Exception as exc:
            raise backend_error(backend_name, "dm", exc) from exc
        finally:
            release(attachments)
        threads.record(dev_name, msg)
        return {"backend": backend_name, "post_id": msg.post_id, "channel": msg.channel}

    async def read(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        channel = _str(args, "channel")
        post_id = _str(args, "post_id", required=False)
        assert channel is not None
        since, limit = args.get("since"), args.get("limit")
        if since is not None and (isinstance(since, bool) or not isinstance(since, int | float)):
            raise OpError("bad_request", "since is a number of seconds since the epoch")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
            raise OpError("bad_request", "limit is an integer")
        if limit is not None and limit < 1:
            raise OpError("bad_request", "limit is at least 1")
        try:
            if post_id is not None:
                msgs = [await backend.read_one(dev_name, channel, post_id)]
            else:
                msgs = await backend.read(
                    dev_name, channel, float(since) if since is not None else None, limit
                )
        except Exception as exc:
            raise backend_error(backend_name, "read", exc) from exc
        return {"backend": backend_name, "messages": [message_json(m) for m in msgs]}

    async def thread(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        post_id = _str(args, "post_id")
        assert post_id is not None
        try:
            msgs = await backend.thread(dev_name, post_id)
        except Exception as exc:
            raise backend_error(backend_name, "thread", exc) from exc
        return {"backend": backend_name, "messages": [message_json(m) for m in msgs]}

    async def react(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        post_id, emoji = _str(args, "post_id"), _str(args, "emoji")
        assert post_id is not None and emoji is not None
        try:
            await backend.react(dev_name, post_id, emoji)
        except Exception as exc:
            raise backend_error(backend_name, "react", exc) from exc
        return {}

    # -- Discord's guild ops (today's disc_list, disc_resolve, disc_create_*) ----------------
    async def list_channels(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        g = guild(backend_name, backend, "list_channels")
        kind = _str(args, "type", required=False) or "text"
        guild_id = _str(args, "guild_id", required=False)
        try:
            channels = await g.list_channels(dev_name, kind, guild_id)
        except Exception as exc:
            raise backend_error(backend_name, "list_channels", exc) from exc
        return {"backend": backend_name, "channels": channels}

    async def resolve(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        g = guild(backend_name, backend, "resolve")
        name = _str(args, "name")
        kind = _str(args, "type", required=False) or "channel"
        guild_id = _str(args, "guild_id", required=False)
        assert name is not None
        try:
            found = await g.resolve(dev_name, name, kind, guild_id)
        except Exception as exc:
            raise backend_error(backend_name, "resolve", exc) from exc
        return {"backend": backend_name, **found}

    async def create_channel(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        g = guild(backend_name, backend, "create_channel")
        name = _str(args, "name")
        kind = _str(args, "type", required=False) or "text"
        guild_id = _str(args, "guild_id", required=False)
        assert name is not None
        try:
            made = await g.create_channel(dev_name, name, kind, guild_id)
        except Exception as exc:
            raise backend_error(backend_name, "create_channel", exc) from exc
        return {"backend": backend_name, **made}

    async def create_thread(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        g = guild(backend_name, backend, "create_thread")
        channel, name = _str(args, "channel"), _str(args, "name")
        message = args.get("message")
        if message is not None and (not isinstance(message, str) or not message.strip()):
            raise OpError("bad_request", "message is a non-empty string")
        assert channel is not None and name is not None
        try:
            made = await g.create_thread(dev_name, channel, name)
        except Exception as exc:
            raise backend_error(backend_name, "create_thread", exc) from exc
        result: dict[str, Any] = {"backend": backend_name, **made}
        if message is not None:
            try:
                msg = await backend.send(dev_name, made["channel"], message, made["id"])
            except Exception as exc:
                raise backend_error(backend_name, "create_thread", exc) from exc
            threads.record(dev_name, msg)
            result["post_id"] = msg.post_id
        return result

    # -- uploads and thread_backend ---------------------------------------------------------
    async def upload(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg = registered(args)
        if uploads is None:
            raise OpError("unsupported", "this daemon holds no uploads")
        try:
            return uploads.chunk(
                dev_name, args.get("name"), args.get("seq"), args.get("data_b64"), args.get("last")
            )
        except UploadError as exc:
            raise OpError(exc.code, exc.detail) from exc

    async def thread_backend(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, reg = registered(args)
        post_id = _str(args, "post_id")
        assert post_id is not None
        if post_kind(post_id) == DISCORD:
            if DISCORD in reg.backends:
                return {"backend": DISCORD}
            raise OpError("not_found", f"{post_id}: {dev_name} is not registered on discord")
        mine = [b for b in reg.backends if b.startswith(MATTERMOST_PREFIX)]
        known = threads.backend_of(dev_name, post_id)
        if known is not None and known in mine:
            return {"backend": known}
        for name in mine:
            probe = backends.get(name)
            if isinstance(probe, PostProbe) and await probe.has_post(dev_name, post_id):
                return {"backend": name}
        servers = ", ".join(mine) or "none"
        raise OpError(
            "not_found", f"{post_id} is on none of {dev_name}'s Mattermost servers ({servers})"
        )

    # -- the shim's pushes ------------------------------------------------------------------
    async def kiosk_brief(args: dict[str, Any]) -> dict[str, Any]:
        dev_name = _str(args, "dev_name")
        text = args.get("text")
        assert dev_name is not None
        if not isinstance(text, str):
            raise OpError("bad_request", "text is a string (empty when there is no brief)")
        if len(text) > MAX_BRIEF_CHARS:
            raise OpError("bad_request", f"the brief is longer than {MAX_BRIEF_CHARS} characters")
        context.briefs[dev_name] = text
        return {}

    async def squawk_ids(args: dict[str, Any]) -> dict[str, Any]:
        dev_name = _str(args, "dev_name")
        ids = args.get("ids")
        assert dev_name is not None
        if not isinstance(ids, list) or len(ids) > MAX_SQUAWK_IDS:
            raise OpError("bad_request", f"ids is a list of at most {MAX_SQUAWK_IDS} ids")
        out: list[str] = []
        for item in ids:
            if isinstance(item, bool) or not isinstance(item, int | str) or item == "":
                raise OpError("bad_request", "each id is an integer or a non-empty string")
            out.append(str(item))
        context.squawk_ids[dev_name] = out
        return {}

    return {
        "send": send,
        "dm": dm,
        "read": read,
        "thread": thread,
        "react": react,
        "list_channels": list_channels,
        "resolve": resolve,
        "create_channel": create_channel,
        "create_thread": create_thread,
        "upload": upload,
        "thread_backend": thread_backend,
        "kiosk_brief": kiosk_brief,
        "squawk_ids": squawk_ids,
    }
