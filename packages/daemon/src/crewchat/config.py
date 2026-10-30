"""The daemon's configuration: `/etc/crewchat/servers.toml` (Dev Spec §5.B, §5.6).

    default_crew = "oaw"
    broker_timeout_s = 2                 # optional; every daemon->broker call's timeout
    my_threads_retention_days = 90       # optional; how long a thread stays "mine" (D-104)
    crew_info_max_age_s = 300            # optional; a last good crew_info older is uncached
    refetch_min_interval_s = 2           # optional; the floor between cache-miss re-fetch rounds
    kiosk_rate_per_hour = 10             # optional; kiosk routings per outsider/expert/server
    loop_guard_window = 20               # optional; bot messages per conversation (R-25)

    [[mattermost]]
    id = "oaw-dev"
    url = "https://mm.example.lan"
    kiosk = true                         # optional; the kiosk is off on a server unless set
    kiosk_daily_cap = 25                 # optional; kiosk answers per server per UTC day
    kiosk_research = true                # optional; research over the corpus pool (D-112)
    corpus_extra = ["forge.example.lan/partner-org/docs"]    # optional; full repos
    corpus_exclude = ["forge.example.lan/partner-org/infra"] # optional; always wins
    corpus_refresh_minutes = 15          # optional; how often the corpus is rebuilt

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
    agents_allow = ["forge.example.lan/partner-org", "/home/agent/work", "name:helper"]  # optional
    agents_deny = ["name:scratch"]       # optional; always wins over agents_allow

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
DEFAULT_CREW_INFO_MAX_AGE_S = 300.0
DEFAULT_REFETCH_MIN_INTERVAL_S = 2.0
DEFAULT_KIOSK_RATE_PER_HOUR = 10
DEFAULT_LOOP_GUARD_WINDOW = 20
DEFAULT_KIOSK_DAILY_CAP = 25
DEFAULT_CORPUS_REFRESH_MINUTES = 15
# A full repo, as the shim's `repo_ref()` gives it: `host/group[/subgroup...]/repo`, no `.git`.
REPO_ENTRY_RE = re.compile(r"^[a-z0-9.-]+(/[A-Za-z0-9._-]+){2,}$")
# An allowlist entry (#117): a forge key `host/org`, as `repo_key` gives it; an absolute path;
# or `name:<dev_name>`.
FORGE_ENTRY_RE = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9._-]+$")
NAME_ENTRY_PREFIX = "name:"
DISCORD_CREDENTIAL_PATH = Path("/etc/crewchat/discord.credential")
NAME_RE = re.compile(r"^[a-z0-9._-]+$")
RESERVED_SERVER_IDS = frozenset({"status", ".", ".."})
CREW_KEYS = {"needs_list"}


class ConfigError(ValueError):
    """`servers.toml` is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class MattermostServer:
    id: str
    url: str
    # The operator's ceiling on the kiosk for this server (#117): off unless turned on, whatever
    # the broker's switch says. `kiosk_daily_cap` bounds its answers per UTC day.
    kiosk: bool = False
    kiosk_daily_cap: int = DEFAULT_KIOSK_DAILY_CAP
    # Kiosk research over this server's corpus pool (#136, D-110 to D-112): off unless set, and
    # then only with `kiosk` on and an explicit `agents_allow` on every crew here. The extra and
    # exclude entries are full repos, `host/group/.../repo`.
    kiosk_research: bool = False
    corpus_extra: tuple[str, ...] = ()
    corpus_exclude: tuple[str, ...] = ()
    corpus_refresh_minutes: int = DEFAULT_CORPUS_REFRESH_MINUTES


@dataclass(frozen=True)
class BrokerEntry:
    """`[crews.<crew>.<server_id>]`: where that crew's broker for that server listens, and
    which agents `register` mints an account for there (`crewchat.allowlist`, #117)."""

    crew: str
    server_id: str
    broker_socket: Path
    broker_user_id: str
    agents_allow: tuple[str, ...] | None = None  # None: every agent (today's behaviour)
    agents_deny: tuple[str, ...] = ()


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
    crew_info_max_age_s: float = DEFAULT_CREW_INFO_MAX_AGE_S
    refetch_min_interval_s: float = DEFAULT_REFETCH_MIN_INTERVAL_S
    kiosk_rate_per_hour: int = DEFAULT_KIOSK_RATE_PER_HOUR
    loop_guard_window: int = DEFAULT_LOOP_GUARD_WINDOW
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

    max_age = _positive(raw, "crew_info_max_age_s", DEFAULT_CREW_INFO_MAX_AGE_S, source)
    refetch = _positive(raw, "refetch_min_interval_s", DEFAULT_REFETCH_MIN_INTERVAL_S, source)
    kiosk_rate = _count(raw, "kiosk_rate_per_hour", DEFAULT_KIOSK_RATE_PER_HOUR, source)
    window = _count(raw, "loop_guard_window", DEFAULT_LOOP_GUARD_WINDOW, source)

    crews_raw = raw.get("crews", {})
    if not isinstance(crews_raw, dict):
        raise ConfigError(f"{source}: crews must be a table")
    crews = {name: _crew(name, value, server_ids, source) for name, value in crews_raw.items()}
    _research_rules(servers, crews, source)

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
        crew_info_max_age_s=max_age,
        refetch_min_interval_s=refetch,
        kiosk_rate_per_hour=kiosk_rate,
        loop_guard_window=window,
        discord_screamhole_url=screamhole_url,
        discord_credential=Path(credential),
        discord_guild_id=guild_id,
        squawkie_url=squawkie_url,
        squawkie_credential=Path(squawkie_credential) if squawkie_credential else None,
    )


def _positive(raw: dict[str, Any], key: str, default: float, source: str) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError(f"{source}: {key} must be a positive number")
    return float(value)


def _count(raw: dict[str, Any], key: str, default: int, source: str) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{source}: {key} must be an integer of at least 1")
    return value


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
        if sid in RESERVED_SERVER_IDS:
            # The corpus is /var/lib/crewcorpus/<id>, beside its world-traversable status/.
            raise ConfigError(f"{source}: mattermost id {sid!r} is reserved")
        if not isinstance(url, str) or not url:
            raise ConfigError(f"{source}: mattermost {sid} needs a url")
        if any(s.id == sid for s in servers):
            raise ConfigError(f"{source}: mattermost id {sid} is listed twice")
        kiosk = item.get("kiosk", False)
        if not isinstance(kiosk, bool):
            raise ConfigError(f"{source}: mattermost {sid}.kiosk must be true or false")
        cap = item.get("kiosk_daily_cap", DEFAULT_KIOSK_DAILY_CAP)
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            # 0 is allowed: no kiosk answers on that server.
            raise ConfigError(
                f"{source}: mattermost {sid}.kiosk_daily_cap must be an integer of at least 0"
            )
        research = item.get("kiosk_research", False)
        if not isinstance(research, bool):
            raise ConfigError(f"{source}: mattermost {sid}.kiosk_research must be true or false")
        refresh = item.get("corpus_refresh_minutes", DEFAULT_CORPUS_REFRESH_MINUTES)
        if isinstance(refresh, bool) or not isinstance(refresh, int) or refresh < 1:
            raise ConfigError(
                f"{source}: mattermost {sid}.corpus_refresh_minutes must be an integer "
                "of at least 1"
            )
        servers.append(
            MattermostServer(
                id=sid,
                url=url,
                kiosk=kiosk,
                kiosk_daily_cap=cap,
                kiosk_research=research,
                corpus_extra=_repos(item.get("corpus_extra", []), f"{sid}.corpus_extra", source),
                corpus_exclude=_repos(
                    item.get("corpus_exclude", []), f"{sid}.corpus_exclude", source
                ),
                corpus_refresh_minutes=refresh,
            )
        )
    return servers


def is_repo(value: str) -> bool:
    """A full repo, `host/group/.../repo`, with no `.` or `..` segment."""
    return REPO_ENTRY_RE.fullmatch(value) is not None and not any(
        part in {".", ".."} for part in value.split("/")
    )


def _repos(value: Any, where: str, source: str) -> tuple[str, ...]:
    """A `corpus_extra` or `corpus_exclude` list of full repos."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{source}: mattermost {where} must be an array of strings")
    for entry in value:
        if not is_repo(entry):
            raise ConfigError(
                f"{source}: mattermost {where} entry {entry!r} is not host/group/.../repo"
            )
    return tuple(value)


def _research_rules(
    servers: list[MattermostServer], crews: dict[str, CrewEntry], source: str
) -> None:
    """D-112: research needs the kiosk on, and an explicit allowlist on every crew there,
    because an absent `agents_allow` admits every agent and the pool would follow."""
    for server in servers:
        if not server.kiosk_research:
            continue
        if not server.kiosk:
            raise ConfigError(f"{source}: mattermost {server.id}.kiosk_research needs kiosk = true")
        for crew in crews.values():
            entry = crew.brokers.get(server.id)
            if entry is not None and entry.agents_allow is None:
                raise ConfigError(
                    f"{source}: mattermost {server.id}.kiosk_research needs agents_allow on "
                    f"crews.{crew.name}.{server.id} (D-112)"
                )


def _agents(value: Any, where: str, source: str) -> tuple[str, ...]:
    """An `agents_allow` or `agents_deny` list; each entry in one of the three forms."""
    if not isinstance(value, list):
        raise ConfigError(f"{source}: {where} must be an array of strings")
    entries: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ConfigError(f"{source}: {where} entries are strings")
        if entry.startswith(NAME_ENTRY_PREFIX):
            ok = NAME_RE.fullmatch(entry[len(NAME_ENTRY_PREFIX) :]) is not None
        elif entry.startswith("/"):
            ok = "\x00" not in entry
        else:
            ok = FORGE_ENTRY_RE.fullmatch(entry) is not None
        if not ok:
            raise ConfigError(
                f"{source}: {where} entry {entry!r} is not host/org, an absolute path, "
                "or name:<dev_name>"
            )
        entries.append(entry)
    return tuple(entries)


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
        where = f"crews.{name}.{key}"
        allow = table.get("agents_allow")
        brokers[key] = BrokerEntry(
            crew=name,
            server_id=key,
            broker_socket=Path(sock),
            broker_user_id=broker_id,
            agents_allow=None if allow is None else _agents(allow, f"{where}.agents_allow", source),
            agents_deny=_agents(table.get("agents_deny", []), f"{where}.agents_deny", source),
        )
    return CrewEntry(name=name, needs_list=needs_list, brokers=brokers)
