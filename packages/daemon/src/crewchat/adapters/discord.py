"""The Discord adapter (Dev Spec §5.2, §5.7; Stories 2.2, 2.4c; R-14, R-15, D-013, D-059, D-106).

`DiscordBackend(screamhole_url, credential_path)` gives the daemon today's Discord behaviour.

- **All Discord REST goes through scream-hole** (D-106). The base for every call, reads and
  writes alike, is `<screamhole_url>/api/v10`, as today's `mcp-server-discord` does
  (`resolveApiBase`); scream-hole proxies every v10 path. `read`, `thread` and `stream` read
  the way mcp-server-discord-watcher's poll core does:
  `GET /api/v10/channels/{id}/messages?after=<snowflake>` with no `limit` (scream-hole answers
  `after`+`limit` with the newest matches only, watcher #92), and
  `GET /api/v10/guilds/{guild}/channels` for the text channels to poll. Every read URL is built
  by `_screamhole_get`, which only ever targets the scream-hole base.
- **While scream-hole is unhealthy** (D-106), reads (polling, `read`, `read_one`, `thread`,
  `list_channels`, `resolve`, channel-name resolution) **never** go to Discord: they raise
  `BackendUnavailable("scream-hole unhealthy")` and `health` reports `degraded`. Writes (`send`,
  `dm`, `react`, `create_channel`, `create_thread`, attachments) fall back to the Discord REST
  API (`discord_api`). Health is `GET <scream-hole>/health`, re-checked when stale: every
  `HEALTHY_RECHECK_S` while healthy, and on a backoff (1 s, doubling, capped at 60 s) while
  not, so every call returns to scream-hole once it recovers.
- **Writes carry the shared bot token** in `credential_path`. The file must belong to the
  daemon's own user (`crewchat`) and carry no group or other permission bits (mode `0600`);
  anything else is refused, never read.
- **Channel names.** Wherever today's tools take a channel name (`agent-ops`, `#agent-ops`),
  the adapter resolves it against the guild's text channels from scream-hole; an unknown name
  is a `ValueError` (so `bad_request`) naming it. One daemon serves one guild
  (`[discord] guild_id`): a `guild_id` other than that one is refused before any request.
- **Attachments and embeds** ride on the last chunk of a `send`: the files as a multipart
  `files[n]`, the embed as `embeds: [{title, description}]`, as today's `disc_send` does.
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
import json
import logging
import os
import re
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from crewchat.adapters.base import (
    Attachment,
    BackendUnavailable,
    Embed,
    InboundMessage,
    NotFound,
)
from crewchat.protocol import PROTO

DISCORD_API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1_420_070_400_000
MAX_CHUNK = 2000  # Discord's message length limit, as disc_send splits today
POLL_INTERVAL_S = 15.0  # the watcher's POLL_INTERVAL_MS
CHANNEL_REFRESH_S = 300.0  # the watcher's CHANNEL_REFRESH_MS
DEFAULT_READ_LIMIT = 60
RETRY_AFTER_MAX_S = 60.0
TEXT_CHANNEL = 0  # the watcher polls type-0 (GUILD_TEXT) channels only
PUBLIC_THREAD = 11
THREAD_ARCHIVE_MINUTES = 1440  # today's disc_create_thread default
SNOWFLAKE_RE = re.compile(r"^\d+$")
# Today's disc_list / disc_create_channel `type` names.
CHANNEL_TYPES = {"text": 0, "voice": 2, "category": 4}
CHANNEL_TYPE_NAMES = {v: k for k, v in CHANNEL_TYPES.items()}
RESOLVE_TYPES = ("channel", "user", "role")
# scream-hole's health (D-106): re-checked on a backoff while it is down, and now and then
# while it is up.
HEALTH_TIMEOUT_S = 2.0  # today's HEALTH_CHECK_TIMEOUT_MS
HEALTH_BACKOFF_FIRST_S = 1.0
HEALTH_BACKOFF_MAX_S = 60.0
HEALTHY_RECHECK_S = 60.0
SCREAMHOLE_UNHEALTHY = "scream-hole unhealthy"
# A scream-hole answer that means scream-hole itself is down, not that the path failed.
SCREAMHOLE_DOWN_STATUSES = frozenset({502, 503, 504})

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


def screamhole_root(url: str) -> str:
    """scream-hole's root, where `/health` lives: the URL without any `/api/v10`."""
    base = url.rstrip("/")
    return base.removesuffix("/api/v10")


def sanitize_channel_name(name: str) -> str:
    """Today's disc_create_channel: no `#`, spaces to `-`, lowercase."""
    return re.sub(r"\s+", "-", name.replace("#", "").strip()).lower()


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
    """Discord through scream-hole, writing with the bot token; writes alone fall back to the
    Discord REST API while scream-hole is unhealthy (D-106)."""

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
        healthy_recheck_s: float = HEALTHY_RECHECK_S,
        health_backoff_max_s: float = HEALTH_BACKOFF_MAX_S,
    ) -> None:
        self.screamhole = ensure_api_v10(screamhole_url)
        self.health_url = f"{screamhole_root(screamhole_url)}/health"
        self.healthy_recheck_s = healthy_recheck_s
        self.health_backoff_max_s = health_backoff_max_s
        self._healthy: bool | None = None  # None: never checked
        self._health_since = 0.0
        self._health_next = 0.0
        self._health_delay = HEALTH_BACKOFF_FIRST_S
        self._guild: list[dict[str, Any]] = []  # every guild channel, for name resolution
        self._guild_at: float | None = None
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

    # -- scream-hole's health (D-106) --------------------------------------------------------
    def _set_health(self, ok: bool) -> None:
        now = self._now()
        if ok:
            if self._healthy is not True:
                if self._healthy is False:
                    log.info("scream-hole recovered: Discord calls go through it again")
                self._health_since = now
            self._healthy = True
            self._health_delay = HEALTH_BACKOFF_FIRST_S
            self._health_next = now + self.healthy_recheck_s
            return
        if self._healthy is not False:
            log.warning(
                "%s: Discord reads are refused, writes fall back to %s",
                SCREAMHOLE_UNHEALTHY,
                self.discord_api,
            )
            self._health_since = now
            self._health_delay = HEALTH_BACKOFF_FIRST_S
        else:
            self._health_delay = min(self._health_delay * 2, self.health_backoff_max_s)
        self._healthy = False
        self._health_next = now + min(self._health_delay, self.health_backoff_max_s)

    async def check_health(self, *, force: bool = False) -> bool:
        """scream-hole's health, re-checked when stale (or `force`)."""
        if not force and self._healthy is not None and self._now() < self._health_next:
            return self._healthy
        try:
            resp = await self._client().get(self.health_url, timeout=HEALTH_TIMEOUT_S)
            ok = resp.status_code == 200
        except (httpx.HTTPError, OSError) as exc:
            log.debug("scream-hole health: %s", exc)
            ok = False
        self._set_health(ok)
        return ok

    async def health(self) -> dict[str, Any]:
        """`health`'s entry for Discord: `ok`, or `degraded` while scream-hole is unhealthy."""
        ok = await self.check_health()
        since = datetime.fromtimestamp(self._health_since, UTC).isoformat()
        if ok:
            return {"state": "ok", "since": since}
        return {
            "state": "degraded",
            "since": since,
            "detail": f"{SCREAMHOLE_UNHEALTHY}: reads refused, writes direct to Discord",
        }

    # -- HTTP -------------------------------------------------------------------------------
    async def _screamhole_get(self, path: str, params: Mapping[str, str]) -> Any:
        """Every read the adapter makes: scream-hole's base, never Discord's (D-013, D-106).
        While scream-hole is unhealthy, a read is refused, not sent anywhere else."""
        if not await self.check_health():
            raise BackendUnavailable(SCREAMHOLE_UNHEALTHY)
        try:
            resp = await self._client().get(f"{self.screamhole}{path}", params=dict(params))
        except httpx.TransportError as exc:
            self._set_health(False)
            raise BackendUnavailable(SCREAMHOLE_UNHEALTHY) from exc
        if resp.status_code in SCREAMHOLE_DOWN_STATUSES:
            self._set_health(False)
            raise BackendUnavailable(SCREAMHOLE_UNHEALTHY)
        if resp.status_code == 404:
            raise NotFound(f"{path}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise DiscordError("scream-hole", resp.status_code, resp.text[:200])
        return resp.json()

    async def _discord_write(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        files: Sequence[tuple[str, tuple[str, bytes]]] = (),
    ) -> Any:
        """A write as the bot: through scream-hole, or straight to the Discord REST API while
        scream-hole is unhealthy (D-106). A 429 waits its `retry_after`. With `files`, the
        request is multipart, `payload_json` carrying `body`."""
        if method == "GET":
            raise ValueError("Discord reads go through scream-hole (D-013)")
        token = read_credential(self.credential_path)
        headers = {"Authorization": f"Bot {token}"}
        direct = not await self.check_health()
        for attempt in range(self.max_retries + 1):
            service = "discord" if direct else "scream-hole"
            url = f"{self.discord_api if direct else self.screamhole}{path}"
            kwargs: dict[str, Any] = {"headers": headers}
            if files:
                kwargs["data"] = {"payload_json": json.dumps(body or {})}
                kwargs["files"] = list(files)
            else:
                kwargs["json"] = body
            try:
                resp = await self._client().request(method, url, **kwargs)
            except httpx.ConnectError:
                if direct:
                    raise
                # scream-hole is down and the write never left: once, straight to Discord.
                self._set_health(False)
                direct = True
                continue
            if not direct and resp.status_code in SCREAMHOLE_DOWN_STATUSES:
                self._set_health(False)
                raise DiscordError(service, resp.status_code, "scream-hole unhealthy mid-write")
            if resp.status_code == 429 and attempt < self.max_retries:
                try:
                    wait = float(resp.json().get("retry_after", 1.0))
                except (ValueError, AttributeError):
                    wait = 1.0
                await self._sleep(min(max(wait, 0.0), RETRY_AFTER_MAX_S))
                continue
            if resp.status_code >= 400:
                raise DiscordError(service, resp.status_code, resp.text[:200])
            return resp.json() if resp.content else None
        raise DiscordError("discord", 429, "rate limited")  # pragma: no cover

    # -- the guild --------------------------------------------------------------------------
    def check_guild(self, guild_id: str | None) -> str:
        """The configured guild; a `guild_id` naming another is refused before any request."""
        if self.guild_id is None:
            raise ValueError("this daemon has no [discord] guild_id configured")
        if guild_id is not None and guild_id != self.guild_id:
            raise ValueError(
                f"guild {guild_id} is not this daemon's guild ({self.guild_id}); "
                "one daemon serves one guild"
            )
        return self.guild_id

    async def guild_channels(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        """Every channel of the guild, from scream-hole, cached for `channel_refresh_s`."""
        guild = self.check_guild(None)
        now = self._now()
        stale = self._guild_at is None or now - self._guild_at >= self.channel_refresh_s
        if fresh or stale:
            data = await self._screamhole_get(f"/guilds/{guild}/channels", {})
            if isinstance(data, list):
                self._guild = [c for c in data if isinstance(c, dict) and "id" in c]
                self._guild_at = now
        return self._guild

    async def channel_id(self, channel: str) -> str:
        """A channel ID as is; a name (`agent-ops`, `#agent-ops`) resolved to the guild's text
        channel of that name. An unknown name is a `ValueError` naming it."""
        raw = channel.strip()
        if SNOWFLAKE_RE.match(raw):
            return raw
        name = raw.removeprefix("#").lower()
        if not name:
            raise ValueError(f"channel {channel!r} is not an ID or a name")
        if self.guild_id is None:
            raise ValueError(f"channel {channel!r}: names need [discord] guild_id configured")
        for fresh in (False, True):
            for c in await self.guild_channels(fresh=fresh):
                if c.get("type") == TEXT_CHANNEL and str(c.get("name", "")).lower() == name:
                    return str(c["id"])
        raise ValueError(f"unknown channel {channel!r} in guild {self.guild_id}")

    async def list_channels(
        self, dev_name: str, type: str = "text", guild_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Today's `disc_list`: the guild's channels of `type`, by position."""
        self.check_guild(guild_id)
        kind = CHANNEL_TYPES.get(type)
        if kind is None:
            raise ValueError(f"type {type!r} is not text, voice or category")
        found = [c for c in await self.guild_channels(fresh=True) if c.get("type") == kind]
        found.sort(key=lambda c: (c.get("position", 0), str(c.get("id"))))
        return [
            {"id": str(c["id"]), "name": str(c.get("name", "")), "type": type} for c in found
        ]

    async def resolve(
        self, dev_name: str, name: str, type: str = "channel", guild_id: str | None = None
    ) -> dict[str, Any]:
        """Today's `disc_resolve`: a channel, user or role name to its ID, case-insensitively."""
        guild = self.check_guild(guild_id)
        if type not in RESOLVE_TYPES:
            raise ValueError(f"type {type!r} is not channel, user or role")
        wanted = name.strip().removeprefix("#" if type == "channel" else "@").lower()
        if not wanted:
            raise ValueError("name is empty")
        if type == "channel":
            for c in await self.guild_channels(fresh=True):
                if str(c.get("name", "")).lower() == wanted:
                    return {"id": str(c["id"]), "name": str(c.get("name", "")), "type": type}
        elif type == "role":
            roles = await self._screamhole_get(f"/guilds/{guild}/roles", {})
            for r in roles if isinstance(roles, list) else []:
                if isinstance(r, dict) and str(r.get("name", "")).lower() == wanted:
                    return {"id": str(r["id"]), "name": str(r.get("name", "")), "type": type}
        else:
            members = await self._screamhole_get(
                f"/guilds/{guild}/members/search", {"query": wanted, "limit": "10"}
            )
            for m in members if isinstance(members, list) else []:
                user = m.get("user") if isinstance(m, dict) else None
                if not isinstance(user, dict):
                    continue
                names = {str(user.get(k) or "").lower() for k in ("username", "global_name")}
                names.add(str(m.get("nick") or "").lower())
                if wanted in names:
                    username = str(user.get("username", ""))
                    return {"id": str(user["id"]), "name": username, "type": type}
        raise NotFound(f"no {type} named {name!r} in guild {guild}")

    async def create_channel(
        self, dev_name: str, name: str, type: str = "text", guild_id: str | None = None
    ) -> dict[str, Any]:
        """Today's `disc_create_channel`: the name sanitized, in the configured guild."""
        guild = self.check_guild(guild_id)
        kind = CHANNEL_TYPES.get(type)
        if kind is None:
            raise ValueError(f"type {type!r} is not text, voice or category")
        clean = sanitize_channel_name(name)
        if not clean:
            raise ValueError(f"channel name {name!r} is empty once sanitized")
        made = await self._discord_write(
            "POST", f"/guilds/{guild}/channels", {"name": clean, "type": kind}
        )
        if not isinstance(made, dict) or "id" not in made:
            raise DiscordError("discord", 0, "create_channel returned no channel")
        self._guild_at = None  # the next name lookup sees it
        return {"id": str(made["id"]), "name": str(made.get("name", clean)), "type": type}

    async def create_thread(self, dev_name: str, channel: str, name: str) -> dict[str, Any]:
        """Today's `disc_create_thread`: a public thread in `channel` (an ID or a name)."""
        if not name.strip():
            raise ValueError("thread name is empty")
        parent = await self.channel_id(channel)
        made = await self._discord_write(
            "POST",
            f"/channels/{parent}/threads",
            {"name": name, "auto_archive_duration": THREAD_ARCHIVE_MINUTES, "type": PUBLIC_THREAD},
        )
        if not isinstance(made, dict) or "id" not in made:
            raise DiscordError("discord", 0, "create_thread returned no thread")
        return {"id": str(made["id"]), "name": str(made.get("name", name)), "channel": parent}

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
        self,
        dev_name: str,
        channel: str,
        text: str,
        thread: str | None = None,
        *,
        attachments: Sequence[Attachment] = (),
        embed: Embed | None = None,
    ) -> InboundMessage:
        """Post `text`, signed, in `channel` (an ID or a name); or in the thread `thread` (a
        channel on Discord). `attachments` and `embed` ride on the last chunk, as today."""
        target = thread or await self.channel_id(channel)
        chunks = self.signed(dev_name, text)
        last: Any = None
        for i, chunk in enumerate(chunks):
            body: dict[str, Any] = {"content": chunk}
            files: list[tuple[str, tuple[str, bytes]]] = []
            if i == len(chunks) - 1:
                if embed is not None:
                    body["embeds"] = [{"title": embed.title, "description": embed.body}]
                if attachments:
                    body["attachments"] = [
                        {"id": n, "filename": a.name} for n, a in enumerate(attachments)
                    ]
                    files = [(f"files[{n}]", (a.name, a.read())) for n, a in enumerate(attachments)]
            last = await self._discord_write(
                "POST", f"/channels/{target}/messages", body, files=files
            )
        if not isinstance(last, dict):
            raise DiscordError("discord", 0, "send returned no message")
        if attachments and len(last.get("attachments") or []) < len(attachments):
            raise DiscordError("discord", 0, "the post was made without its attachments")
        last.setdefault("channel_id", target)
        return self.message(last, root_id=thread or "")

    async def read(
        self, dev_name: str, channel: str, since: float | None = None, limit: int | None = None
    ) -> list[InboundMessage]:
        """Messages in `channel` (an ID or a name) from scream-hole's cache, oldest first."""
        after = snowflake_at(since) if since is not None else "0"
        target = await self.channel_id(channel)
        msgs = [self.message(m) for m in await self._messages_after(target, after)]
        if limit is None and since is None:
            limit = DEFAULT_READ_LIMIT
        return msgs[-limit:] if limit else msgs

    async def read_one(self, dev_name: str, channel: str, post_id: str) -> InboundMessage:
        """Today's `disc_read(message_id)`: one message, from scream-hole."""
        if not SNOWFLAKE_RE.match(post_id):
            raise ValueError(f"message id {post_id!r} is not a snowflake")
        target = await self.channel_id(channel)
        data = await self._screamhole_get(f"/channels/{target}/messages/{post_id}", {})
        if not isinstance(data, dict) or str(data.get("id", "")) != post_id:
            raise NotFound(f"message {post_id} in {target}")
        data.setdefault("channel_id", target)
        return self.message(data)

    async def thread(self, dev_name: str, post_id: str) -> list[InboundMessage]:
        """Every message in the thread `post_id` (the thread channel's ID), oldest first."""
        return [self.message(m, root_id=post_id) for m in await self._messages_after(post_id, "0")]

    async def dm(
        self,
        dev_name: str,
        to: str,
        text: str,
        *,
        attachments: Sequence[Attachment] = (),
        embed: Embed | None = None,
    ) -> InboundMessage:
        """A DM from the bot to the user `to`: a snowflake, `@<id>` or `<@id>`, signed."""
        user = to.strip()
        mention = USER_MENTION_RE.match(user)
        user = mention.group(1) if mention else user.removeprefix("@")
        if not SNOWFLAKE_RE.match(user):
            raise ValueError(f"a Discord DM needs a user ID, not {to!r}")
        channel = await self._discord_write("POST", "/users/@me/channels", {"recipient_id": user})
        if not isinstance(channel, dict) or "id" not in channel:
            raise DiscordError("discord", 0, "no DM channel")
        return await self.send(
            dev_name, str(channel["id"]), text, attachments=attachments, embed=embed
        )

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
        except (DiscordError, BackendUnavailable, NotFound, httpx.HTTPError, ValueError) as exc:
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
            if not await self.check_health():
                # D-106: no poll while scream-hole is unhealthy; never Discord instead.
                await self._sleep(self.poll_interval_s)
                continue
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
                except (
                    DiscordError,
                    BackendUnavailable,
                    NotFound,
                    httpx.HTTPError,
                    ValueError,
                ) as exc:
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
