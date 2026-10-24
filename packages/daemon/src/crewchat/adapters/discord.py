"""The Discord adapter (Dev Spec §5.2, §5.7; Story 2.2; R-14, R-15, D-013, D-059).

`DiscordBackend(screamhole_url, credential_path)` gives the daemon today's Discord behaviour.

- **Reads go through scream-hole, never Discord** (D-013). `read`, `thread` and `stream` use
  scream-hole's read API the way mcp-server-discord-watcher's poll core does:
  `GET /api/v10/channels/{id}/messages?after=<snowflake>` with no `limit` (scream-hole answers
  `after`+`limit` with the newest matches only, watcher #92), and
  `GET /api/v10/guilds/{guild}/channels` for the text channels to poll. Every read URL is built
  by `_screamhole_get`, which only ever targets the scream-hole base.
- **Writes go to the Discord REST API** (`send`, `dm`, `react`), with the shared bot token in
  `credential_path`. The file must belong to the daemon's own user (`crewchat`) and carry no
  group or other permission bits (mode `0600`); anything else is refused, never read.
- **The signature is the identity.** Discord has one shared bot, so each agent signs its posts
  `— **<dev-name>** <avatar> (<team>) ·p1` (D-059, spike 1.2 / D-095). `send` appends it to
  every chunk of a long message, so each chunk can be echo-filtered on its own.
- **Inbound parsing** takes the author from the signature, as today's watcher does (`— **name**`,
  markdown code stripped first). The ` ·p<N>` suffix is optional: without it `proto` is 0 (R-14).
- **Echo filter:** `stream` drops a message whose signature name is the receiving agent's
  Dev-Name, case-insensitively. There is no per-agent user ID on Discord (R-15).
- **Threads are channels.** On Discord a thread is a channel whose ID is its starter message's;
  `send(..., thread=T)` posts into thread `T`, and `thread(T)` reads it.
- `react` needs the message's channel. It is remembered for every message the adapter has seen;
  a caller may also name it directly as `<channel_id>/<message_id>`.
- Discord messages are crew-equivalent (§5.9): `is_bot` is the author's `bot` flag, and `kiosk`
  is always False.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from crewchat.adapters.base import InboundMessage
from crewchat.protocol import PROTO

DISCORD_API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1_420_070_400_000
MAX_CHUNK = 2000  # Discord's message length limit, as disc_send splits today
POLL_INTERVAL_S = 15.0  # the watcher's POLL_INTERVAL_MS
CHANNEL_REFRESH_S = 300.0  # the watcher's CHANNEL_REFRESH_MS
DEFAULT_READ_LIMIT = 60
RETRY_AFTER_MAX_S = 60.0
TEXT_CHANNEL = 0  # the watcher polls type-0 (GUILD_TEXT) channels only
SNOWFLAKE_RE = re.compile(r"^\d+$")

# `— **<name>**`, then the rest of that line: `<avatar> (<team>) ·p<N>`, each part optional.
SIGNATURE_RE = re.compile(r"— \*\*(?P<name>[^*\n]+)\*\*(?P<rest>[^\n]*)")
TEAM_RE = re.compile(r"\((?P<team>[^()\n]*)\)")
PROTO_SUFFIX_RE = re.compile(r"(?:^|\s)·p(?P<proto>\d+)\s*$")
FENCED_RE = re.compile(r"```[\s\S]*?```")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
USER_MENTION_RE = re.compile(r"^<@!?(\d+)>$")

log = logging.getLogger("crewchat.adapters.discord")


class DiscordError(Exception):
    """A Discord or scream-hole request that did not succeed."""

    def __init__(self, service: str, status: int, detail: str = "") -> None:
        self.service = service
        self.status = status
        self.detail = detail
        super().__init__(f"{service} HTTP {status}{': ' + detail if detail else ''}")


class CredentialError(Exception):
    """The Discord credential is missing, empty, or not owned `crewchat`/`0600`."""


@dataclass(frozen=True)
class Signature:
    name: str
    avatar: str
    team: str
    proto: int


@dataclass(frozen=True)
class Identity:
    """What an agent's signature carries besides its Dev-Name."""

    avatar: str = ""
    team: str = ""


IdentityFor = Callable[[str], Identity | None]


def strip_markdown_code(text: str) -> str:
    """Drop fenced blocks and inline spans, so a quoted signature example never matches."""
    return INLINE_CODE_RE.sub("", FENCED_RE.sub("", text))


def parse_signature(text: str) -> Signature | None:
    """The message's last signature line, or None when it has none.

    Accepts today's `— **name** avatar (team)` and the `·p1` form alike; `proto` is the suffix's
    number, 0 when there is no suffix (R-14).
    """
    matches = list(SIGNATURE_RE.finditer(strip_markdown_code(text)))
    if not matches:
        return None
    last = matches[-1]
    name = last.group("name").strip()
    if not name:
        return None
    rest = last.group("rest")
    proto_m = PROTO_SUFFIX_RE.search(rest)
    proto = int(proto_m.group("proto")) if proto_m else 0
    if proto_m:
        rest = rest[: proto_m.start()]
    team_m = TEAM_RE.search(rest)
    team = team_m.group("team").strip() if team_m else ""
    avatar = (rest[: team_m.start()] if team_m else rest).strip()
    return Signature(name=name, avatar=avatar, team=team, proto=proto)


def format_signature(dev_name: str, identity: Identity | None = None) -> str:
    """`— **<dev-name>** <avatar> (<team>) ·p1`, leaving out an empty avatar or team."""
    ident = identity or Identity()
    parts = [f"— **{dev_name}**"]
    if ident.avatar:
        parts.append(ident.avatar)
    if ident.team:
        parts.append(f"({ident.team})")
    parts.append(f"·p{PROTO}")
    return " ".join(parts)


def is_own_signature(text: str, dev_name: str) -> bool:
    sig = parse_signature(text)
    return sig is not None and sig.name.lower() == dev_name.lower()


def snowflake_at(seconds: float) -> str:
    """The smallest snowflake for a time: a cursor that `after` compares against."""
    ms = max(int(seconds * 1000) - DISCORD_EPOCH_MS, 0)
    return str(ms << 22)


def snowflake_time(snowflake: str) -> float:
    if not SNOWFLAKE_RE.match(snowflake):
        return 0.0
    return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000.0


def _snowflake_key(msg: Mapping[str, Any]) -> int:
    msg_id = str(msg.get("id", ""))
    return int(msg_id) if SNOWFLAKE_RE.match(msg_id) else 0


def _timestamp(msg: Mapping[str, Any]) -> float:
    raw = msg.get("timestamp")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            pass
    return snowflake_time(str(msg.get("id", "")))


def split_message(text: str, max_len: int) -> list[str]:
    """disc_send's split: on the last space within `max_len`, else a hard split."""
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        at = remaining.rfind(" ", 0, max_len + 1)
        if at <= 0:
            at = max_len
        chunks.append(remaining[:at])
        remaining = remaining[at:].removeprefix(" ")
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def ensure_api_v10(url: str) -> str:
    """scream-hole's Discord-compatible API lives under `/api/v10`, as the watcher assumes."""
    base = url.rstrip("/")
    return base if base.endswith("/api/v10") else f"{base}/api/v10"


def read_credential(path: Path) -> str:
    """The bot token, from a file owned by this process's user with mode `0600` or tighter."""
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise CredentialError(f"{path}: no Discord credential") from exc
    if not stat.S_ISREG(st.st_mode):
        raise CredentialError(f"{path}: not a regular file")
    if st.st_uid != os.geteuid():
        raise CredentialError(f"{path}: not owned by the daemon's user")
    if st.st_mode & 0o077:
        raise CredentialError(f"{path}: mode {stat.S_IMODE(st.st_mode):04o}, want 0600")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise CredentialError(f"{path}: empty")
    return token


@dataclass
class _Agent:
    closed: bool = False


class DiscordBackend:
    """Discord, read through scream-hole and written through the REST API with the bot token."""

    name = "discord"

    def __init__(
        self,
        screamhole_url: str,
        credential_path: str | Path,
        *,
        guild_id: str | None = None,
        channels: Sequence[str] | None = None,
        identity_for: IdentityFor | None = None,
        discord_api: str = DISCORD_API,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.time,
        poll_interval_s: float = POLL_INTERVAL_S,
        channel_refresh_s: float = CHANNEL_REFRESH_S,
        max_retries: int = 3,
    ) -> None:
        self.screamhole = ensure_api_v10(screamhole_url)
        self.discord_api = discord_api.rstrip("/")
        self.credential_path = Path(credential_path)
        self.guild_id = guild_id
        self.static_channels = list(channels) if channels is not None else None
        self._identity_for = identity_for
        self._identities: dict[str, Identity] = {}
        self._http = http_client
        self._owns_http = http_client is None
        self._sleep = sleep
        self._now = now
        self.poll_interval_s = poll_interval_s
        self.channel_refresh_s = channel_refresh_s
        self.max_retries = max_retries
        self._agents: dict[str, _Agent] = {}
        self._where: dict[str, str] = {}  # message ID -> channel ID, for `react`
        self._channels: list[str] = []
        self._channels_at: float | None = None

    # -- identity ---------------------------------------------------------------------------
    def set_identity(self, dev_name: str, *, avatar: str = "", team: str = "") -> None:
        """Record what `dev_name`'s signature carries (`identity_for`, when given, wins)."""
        self._identities[dev_name] = Identity(avatar=avatar, team=team)

    def identity(self, dev_name: str) -> Identity | None:
        if self._identity_for is not None:
            found = self._identity_for(dev_name)
            if found is not None:
                return found
        return self._identities.get(dev_name)

    def signature(self, dev_name: str) -> str:
        return format_signature(dev_name, self.identity(dev_name))

    def signed(self, dev_name: str, text: str) -> list[str]:
        """`text` in Discord-sized chunks, each ending with the agent's `·p1` signature.

        A signature of this agent's that already ends the text is replaced, not doubled.
        """
        body = text.rstrip()
        lines = body.split("\n")
        if lines and is_own_signature(lines[-1], dev_name) and lines[-1].lstrip().startswith("—"):
            body = "\n".join(lines[:-1]).rstrip()
        sig = self.signature(dev_name)
        room = MAX_CHUNK - len(sig) - 2
        return [f"{chunk}\n\n{sig}" if chunk else sig for chunk in split_message(body, room)]

    # -- HTTP -------------------------------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def _screamhole_get(self, path: str, params: Mapping[str, str]) -> Any:
        """Every read the adapter makes: scream-hole's base, never Discord's (D-013)."""
        resp = await self._client().get(f"{self.screamhole}{path}", params=dict(params))
        if resp.status_code != 200:
            raise DiscordError("scream-hole", resp.status_code, resp.text[:200])
        return resp.json()

    async def _discord_write(self, method: str, path: str, body: Any | None = None) -> Any:
        """A write to the Discord REST API as the bot; a 429 waits its `retry_after`."""
        if method == "GET":
            raise ValueError("Discord reads go through scream-hole (D-013)")
        token = read_credential(self.credential_path)
        headers = {"Authorization": f"Bot {token}"}
        url = f"{self.discord_api}{path}"
        for attempt in range(self.max_retries + 1):
            resp = await self._client().request(method, url, headers=headers, json=body)
            if resp.status_code == 429 and attempt < self.max_retries:
                try:
                    wait = float(resp.json().get("retry_after", 1.0))
                except (ValueError, AttributeError):
                    wait = 1.0
                await self._sleep(min(max(wait, 0.0), RETRY_AFTER_MAX_S))
                continue
            if resp.status_code >= 400:
                raise DiscordError("discord", resp.status_code, resp.text[:200])
            return resp.json() if resp.content else None
        raise DiscordError("discord", 429, "rate limited")  # pragma: no cover

    # -- mapping ----------------------------------------------------------------------------
    def message(self, msg: Mapping[str, Any], root_id: str = "") -> InboundMessage:
        """Map a Discord message object to an `InboundMessage`."""
        content = str(msg.get("content") or "")
        author_raw = msg.get("author")
        author: Mapping[str, Any] = author_raw if isinstance(author_raw, dict) else {}
        sig = parse_signature(content)
        if sig is not None:
            author_name = sig.name
        else:
            author_name = str(author.get("global_name") or author.get("username") or "")
        ref = msg.get("message_reference")
        if not root_id and isinstance(ref, dict) and msg.get("type") == 19:
            root_id = str(ref.get("message_id") or "")
        mentions_raw = msg.get("mentions")
        mentions = [
            str(m["id"])
            for m in (mentions_raw if isinstance(mentions_raw, list) else [])
            if isinstance(m, dict) and "id" in m
        ]
        post_id = str(msg.get("id", ""))
        channel = str(msg.get("channel_id", ""))
        if post_id and channel:
            self._where[post_id] = channel
        return InboundMessage(
            backend=self.name,
            channel=channel,
            post_id=post_id,
            root_id=root_id,
            author_id=str(author.get("id", "")),
            author_name=author_name,
            text=content,
            mentions=mentions,
            proto=sig.proto if sig is not None else 0,
            ts=_timestamp(msg),
            is_bot=author.get("bot") is True,
            kiosk=False,
        )

    async def _messages_after(self, channel: str, after: str) -> list[dict[str, Any]]:
        data = await self._screamhole_get(f"/channels/{channel}/messages", {"after": after})
        if not isinstance(data, list):
            return []
        msgs = [m for m in data if isinstance(m, dict)]
        for m in msgs:
            m.setdefault("channel_id", channel)
        return sorted(msgs, key=_snowflake_key)

    # -- the contract -----------------------------------------------------------------------
    async def send(
        self, dev_name: str, channel: str, text: str, thread: str | None = None
    ) -> InboundMessage:
        """Post `text`, signed, in `channel`; or in the thread `thread` (a channel on Discord)."""
        target = thread or channel
        last: Any = None
        for chunk in self.signed(dev_name, text):
            last = await self._discord_write(
                "POST", f"/channels/{target}/messages", {"content": chunk}
            )
        if not isinstance(last, dict):
            raise DiscordError("discord", 0, "send returned no message")
        last.setdefault("channel_id", target)
        return self.message(last, root_id=thread or "")

    async def read(
        self, dev_name: str, channel: str, since: float | None = None, limit: int | None = None
    ) -> list[InboundMessage]:
        """Messages in `channel` from scream-hole's cache, oldest first."""
        after = snowflake_at(since) if since is not None else "0"
        msgs = [self.message(m) for m in await self._messages_after(channel, after)]
        if limit is None and since is None:
            limit = DEFAULT_READ_LIMIT
        return msgs[-limit:] if limit else msgs

    async def thread(self, dev_name: str, post_id: str) -> list[InboundMessage]:
        """Every message in the thread `post_id` (the thread channel's ID), oldest first."""
        return [self.message(m, root_id=post_id) for m in await self._messages_after(post_id, "0")]

    async def dm(self, dev_name: str, to: str, text: str) -> InboundMessage:
        """A DM from the bot to the user `to`: a snowflake, `@<id>` or `<@id>`, signed."""
        user = to.strip()
        mention = USER_MENTION_RE.match(user)
        user = mention.group(1) if mention else user.removeprefix("@")
        if not SNOWFLAKE_RE.match(user):
            raise ValueError(f"a Discord DM needs a user ID, not {to!r}")
        channel = await self._discord_write("POST", "/users/@me/channels", {"recipient_id": user})
        if not isinstance(channel, dict) or "id" not in channel:
            raise DiscordError("discord", 0, "no DM channel")
        return await self.send(dev_name, str(channel["id"]), text)

    async def react(self, dev_name: str, post_id: str, emoji: str) -> None:
        """Add `emoji` (a unicode emoji, or `name:id` for a custom one) to `post_id`."""
        channel, _, message = post_id.rpartition("/")
        if not channel:
            message = post_id
            channel = self._where.get(post_id, "")
        if not channel:
            raise KeyError(f"no channel known for message {post_id}; pass <channel>/<message>")
        name = emoji.strip()
        if len(name) > 2 and name.startswith(":") and name.endswith(":"):
            name = name[1:-1]
        await self._discord_write(
            "PUT", f"/channels/{channel}/messages/{message}/reactions/{quote(name, safe='')}/@me"
        )

    # -- the stream -------------------------------------------------------------------------
    async def poll_channels(self) -> list[str]:
        """The channels `stream` polls: the configured list, else the guild's text channels."""
        if self.static_channels is not None:
            return self.static_channels
        if self.guild_id is None:
            return []
        now = self._now()
        fresh = self._channels_at is not None and now - self._channels_at < self.channel_refresh_s
        if fresh:
            return self._channels
        try:
            data = await self._screamhole_get(f"/guilds/{self.guild_id}/channels", {})
        except (DiscordError, httpx.HTTPError, ValueError) as exc:
            log.warning("scream-hole channel list: %s", exc)
            return self._channels
        if isinstance(data, list):
            self._channels = [
                str(c["id"])
                for c in data
                if isinstance(c, dict) and c.get("type") == TEXT_CHANNEL and "id" in c
            ]
            self._channels_at = now
        return self._channels

    async def stream(self, dev_name: str) -> AsyncIterator[InboundMessage]:
        """New messages for `dev_name`, polled from scream-hole, its own signed posts dropped."""
        agent = self._agents.setdefault(dev_name, _Agent())
        agent.closed = False
        start = snowflake_at(self._now())
        cursors: dict[str, str] = {}
        warned = False
        while not agent.closed:
            channels = await self.poll_channels()
            if not channels and not warned:
                log.warning("discord stream %s: no channels to poll", dev_name)
                warned = True
            for channel in channels:
                if agent.closed:
                    break
                cursor = cursors.setdefault(channel, start)
                try:
                    raw = await self._messages_after(channel, cursor)
                except (DiscordError, httpx.HTTPError, ValueError) as exc:
                    log.warning("discord poll %s for %s: %s", channel, dev_name, exc)
                    continue
                for m in raw:
                    if _snowflake_key(m) <= int(cursors[channel]):
                        continue
                    cursors[channel] = str(m["id"])
                    msg = self.message(m)
                    if is_own_signature(msg.text, dev_name):
                        continue  # echo, by signature (R-15)
                    yield msg
            if agent.closed:
                break
            await self._sleep(self.poll_interval_s)

    # -- token hooks (crewchat.tokens.Adapter) ----------------------------------------------
    async def on_token_rotated(self, dev_name: str) -> None:
        """Nothing to do: Discord has one shared bot credential, not per-agent tokens."""

    async def close_agent(self, dev_name: str) -> None:
        """`dev_name` left rotation: end its stream after the current poll."""
        agent = self._agents.get(dev_name)
        if agent is not None:
            agent.closed = True

    async def aclose(self) -> None:
        for dev_name in list(self._agents):
            await self.close_agent(dev_name)
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
