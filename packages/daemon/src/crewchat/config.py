"""The daemon's configuration: `/etc/crewchat/servers.toml` (Dev Spec §5.B, §5.6).

    default_crew = "oaw"
    broker_timeout_s = 2                 # optional; every daemon->broker call's timeout
    my_threads_retention_days = 90       # optional; how long a thread stays "mine" (D-104)

    [[mattermost]]
    id = "oaw-dev"
    url = "https://mm.example.lan"

    [discord]
    enabled = true
    screamhole_url = "http://127.0.0.1:8787"   # required when enabled, for `crewchat serve`
    credential = "/etc/crewchat/discord.credential"  # optional; this is the default
    guild_id = "<the guild's id>"               # optional: the channels the stream polls

    [squawkie]                           # optional: the doorbell's squawk feed (Story 3.2)
    url = "http://squawkie.lan:3000"
    credential = "/etc/crewchat/squawkie.credential"  # optional: its API token, mode 0600

    [crews.oaw]
    needs_list = "bj:needs"              # the sqtk list `register` returns (R-11)

    [crews.oaw.oaw-dev]                  # one table per crew and Mattermost server
    broker_socket = "/run/crewbroker/oaw-oaw-dev.sock"
    broker_user_id = "<the broker account's user id>"

The `broker_user_id` entries are the configured broker list: the only accounts that may hold
the patched role (R-06). IT-01 asserts against them.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("/etc/crewchat/servers.toml")
DEFAULT_BROKER_TIMEOUT_S = 2.0
# How long a thread the agent posted in keeps counting as "mine" (D-104). Our own setting:
# it never reads any server's retention policy (CT-01).
DEFAULT_MY_THREADS_RETENTION_DAYS = 90
DISCORD_CREDENTIAL_PATH = Path("/etc/crewchat/discord.credential")
NAME_RE = re.compile(r"^[a-z0-9._-]+$")
CREW_KEYS = {"needs_list"}


class ConfigError(ValueError):
    """`servers.toml` is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class MattermostServer:
    id: str
    url: str


@dataclass(frozen=True)
class BrokerEntry:
    """`[crews.<crew>.<server_id>]`: where that crew's broker for that server listens."""

    crew: str
    server_id: str
    broker_socket: Path
    broker_user_id: str


@dataclass(frozen=True)
class CrewEntry:
    name: str
    needs_list: str
    brokers: dict[str, BrokerEntry] = field(default_factory=dict)  # by server_id


@dataclass(frozen=True)
class ServersConfig:
    mattermost: list[MattermostServer]
    discord_enabled: bool
    default_crew: str
    crews: dict[str, CrewEntry]
    broker_timeout_s: float = DEFAULT_BROKER_TIMEOUT_S
    my_threads_retention_days: int = DEFAULT_MY_THREADS_RETENTION_DAYS
    discord_screamhole_url: str | None = None
    discord_credential: Path = DISCORD_CREDENTIAL_PATH
    discord_guild_id: str | None = None
    squawkie_url: str | None = None
    squawkie_credential: Path | None = None

    def broker_entries(self) -> list[BrokerEntry]:
        return [b for crew in self.crews.values() for b in crew.brokers.values()]

    def broker_user_ids(self) -> list[str]:
        """The configured broker list (R-06)."""
        return [b.broker_user_id for b in self.broker_entries()]


def load_config(path: Path = CONFIG_PATH) -> ServersConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: no such file") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_config(raw, source=str(path))


def parse_config(raw: dict[str, Any], *, source: str = "<servers.toml>") -> ServersConfig:
    servers = _mattermost(raw.get("mattermost", []), source)
    server_ids = {s.id for s in servers}

    discord = raw.get("discord", {})
    if not isinstance(discord, dict):
        raise ConfigError(f"{source}: discord must be a table")
    enabled = discord.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{source}: discord.enabled must be true or false")
    screamhole_url = discord.get("screamhole_url")
    if screamhole_url is not None and (not isinstance(screamhole_url, str) or not screamhole_url):
        raise ConfigError(f"{source}: discord.screamhole_url must be a non-empty string")
    credential = discord.get("credential", str(DISCORD_CREDENTIAL_PATH))
    if not isinstance(credential, str) or not credential.startswith("/"):
        raise ConfigError(f"{source}: discord.credential must be an absolute path")
    guild_id = discord.get("guild_id")
    if guild_id is not None and (not isinstance(guild_id, str) or not guild_id.isdigit()):
        raise ConfigError(f"{source}: discord.guild_id must be a snowflake string")

    squawkie = raw.get("squawkie", {})
    if not isinstance(squawkie, dict):
        raise ConfigError(f"{source}: squawkie must be a table")
    squawkie_url = squawkie.get("url")
    if squawkie_url is not None and (not isinstance(squawkie_url, str) or not squawkie_url):
        raise ConfigError(f"{source}: squawkie.url must be a non-empty string")
    squawkie_credential = squawkie.get("credential")
    if squawkie_credential is not None and (
        not isinstance(squawkie_credential, str) or not squawkie_credential.startswith("/")
    ):
        raise ConfigError(f"{source}: squawkie.credential must be an absolute path")
    if squawkie_credential is not None and squawkie_url is None:
        raise ConfigError(f"{source}: squawkie.credential needs squawkie.url")

    timeout = raw.get("broker_timeout_s", DEFAULT_BROKER_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError(f"{source}: broker_timeout_s must be a positive number")

    retention = raw.get("my_threads_retention_days", DEFAULT_MY_THREADS_RETENTION_DAYS)
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
        raise ConfigError(f"{source}: my_threads_retention_days must be an integer of at least 1")

    crews_raw = raw.get("crews", {})
    if not isinstance(crews_raw, dict):
        raise ConfigError(f"{source}: crews must be a table")
    crews = {name: _crew(name, value, server_ids, source) for name, value in crews_raw.items()}

    default_crew = raw.get("default_crew")
    if not isinstance(default_crew, str) or default_crew not in crews:
        raise ConfigError(f"{source}: default_crew must name a [crews.<crew>] table")

    return ServersConfig(
        mattermost=servers,
        discord_enabled=enabled,
        default_crew=default_crew,
        crews=crews,
        broker_timeout_s=float(timeout),
        my_threads_retention_days=retention,
        discord_screamhole_url=screamhole_url,
        discord_credential=Path(credential),
        discord_guild_id=guild_id,
        squawkie_url=squawkie_url,
        squawkie_credential=Path(squawkie_credential) if squawkie_credential else None,
    )


def _mattermost(value: Any, source: str) -> list[MattermostServer]:
    if not isinstance(value, list):
        raise ConfigError(f"{source}: mattermost must be an array of tables")
    servers: list[MattermostServer] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError(f"{source}: each [[mattermost]] entry is a table")
        sid, url = item.get("id"), item.get("url")
        if not isinstance(sid, str) or not NAME_RE.fullmatch(sid):
            raise ConfigError(f"{source}: mattermost id {sid!r} is not a server id")
        if not isinstance(url, str) or not url:
            raise ConfigError(f"{source}: mattermost {sid} needs a url")
        if any(s.id == sid for s in servers):
            raise ConfigError(f"{source}: mattermost id {sid} is listed twice")
        servers.append(MattermostServer(id=sid, url=url))
    return servers


def _crew(name: str, value: Any, server_ids: set[str], source: str) -> CrewEntry:
    if not NAME_RE.fullmatch(name):
        raise ConfigError(f"{source}: crew {name!r} has characters outside a-z0-9._-")
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: crews.{name} must be a table")
    needs_list = value.get("needs_list")
    if not isinstance(needs_list, str) or not needs_list:
        raise ConfigError(f"{source}: crews.{name}.needs_list must be a non-empty string")
    brokers: dict[str, BrokerEntry] = {}
    for key, table in value.items():
        if key in CREW_KEYS:
            continue
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: crews.{name}.{key} is not a known key or a table")
        if key not in server_ids:
            raise ConfigError(f"{source}: crews.{name}.{key} names no [[mattermost]] server")
        sock, broker_id = table.get("broker_socket"), table.get("broker_user_id")
        if not isinstance(sock, str) or not sock.startswith("/"):
            raise ConfigError(f"{source}: crews.{name}.{key}.broker_socket must be absolute")
        if not isinstance(broker_id, str) or not broker_id:
            raise ConfigError(f"{source}: crews.{name}.{key}.broker_user_id is required")
        brokers[key] = BrokerEntry(
            crew=name, server_id=key, broker_socket=Path(sock), broker_user_id=broker_id
        )
    return CrewEntry(name=name, needs_list=needs_list, brokers=brokers)
