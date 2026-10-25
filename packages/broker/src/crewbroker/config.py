"""The broker's configuration: `/etc/crewbroker/<crew>-<server_id>.toml` (Dev Spec §5.6).

One instance serves one crew on one Mattermost server. The file names:

    crew = "oaw"                     # optional: defaults to the first human's username (R-02)
    server_id = "oaw-dev"
    server_url = "https://mm.example.lan"
    human_user_ids = ["<user id>"]   # the crew's human partners, by Mattermost user ID
    trusted = ["<crew>", ...]        # trusted crews, by name only

`trusted` is names only. Each trusted crew's membership (`broker_user_id`, `human_user_ids`,
`minted_bot_ids`) comes from that crew's own broker, which the daemon calls directly; it is
never copied into this file. `broker_user_id` is never configured either: it is filled at
startup from `/users/me`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crewbroker.mattermost import MattermostAPI

CONFIG_DIR = Path("/etc/crewbroker")
STATE_ROOT = Path("/var/lib/crewbroker")
RUN_DIR = Path("/run/crewbroker")

NAME_RE = re.compile(r"^[a-z0-9._-]+$")
# Membership data belongs to a crew's own broker, never to a config file.
MEMBERSHIP_KEYS = ("broker_user_id", "human_user_ids", "minted_bot_ids")


class ConfigError(ValueError):
    """The configuration file is missing, malformed, or names something it must not."""


@dataclass
class CrewConfig:
    crew: str
    server_id: str
    server_url: str
    human_user_ids: list[str]
    trusted: list[str] = field(default_factory=list)
    broker_user_id: str = ""  # filled at startup from /users/me


def config_path(instance: str, config_dir: Path = CONFIG_DIR) -> Path:
    return config_dir / f"{instance}.toml"


def credential_path(instance: str, config_dir: Path = CONFIG_DIR) -> Path:
    return config_dir / f"{instance}.credential"


def state_dir(instance: str, state_root: Path = STATE_ROOT) -> Path:
    return state_root / instance


def socket_path(instance: str, run_dir: Path = RUN_DIR) -> Path:
    return run_dir / f"{instance}.sock"


def _read_raw(instance: str, config_dir: Path) -> tuple[dict[str, Any], str]:
    path = config_path(instance, config_dir)
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), str(path)
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: no such file") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def read_server_url(instance: str, config_dir: Path = CONFIG_DIR) -> str:
    """Only `server_url`: what startup needs to build the client that `load_config` uses."""
    raw, source = _read_raw(instance, config_dir)
    return _str(raw, "server_url", source)


def load_config(
    instance: str,
    *,
    config_dir: Path = CONFIG_DIR,
    client: MattermostAPI | None = None,
) -> CrewConfig:
    """Read the instance's config. With `client`, fill `broker_user_id` and default `crew`."""
    raw, source = _read_raw(instance, config_dir)
    return parse_config(raw, source=source, client=client)


def parse_config(
    raw: dict[str, Any], *, source: str = "<config>", client: MattermostAPI | None = None
) -> CrewConfig:
    for key in ("broker_user_id", "minted_bot_ids"):
        if key in raw:
            raise ConfigError(f"{source}: {key!r} is never configured; it comes from the server")

    server_id = _str(raw, "server_id", source)
    server_url = _str(raw, "server_url", source)

    humans = raw.get("human_user_ids")
    if not (isinstance(humans, list) and humans and all(isinstance(h, str) and h for h in humans)):
        raise ConfigError(f"{source}: human_user_ids must be a non-empty list of user IDs")

    trusted = _trusted(raw.get("trusted", []), source)

    crew = raw.get("crew")
    if crew is not None and not isinstance(crew, str):
        raise ConfigError(f"{source}: crew must be a string")

    broker_user_id = ""
    if client is not None:
        broker_user_id = str(client.me()["id"])
        if crew is None:
            # R-02: the crew defaults to the human partner's username.
            crew = str(client.get_user(humans[0])["username"])
    if crew is None:
        raise ConfigError(f"{source}: crew is absent and there is no server to default it from")
    if not NAME_RE.fullmatch(crew):
        raise ConfigError(f"{source}: crew {crew!r} has characters outside a-z0-9._-")

    return CrewConfig(
        crew=crew,
        server_id=server_id,
        server_url=server_url,
        human_user_ids=list(humans),
        trusted=trusted,
        broker_user_id=broker_user_id,
    )


def _str(raw: dict[str, Any], key: str, source: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{source}: {key} must be a non-empty string")
    return value


def _trusted(value: Any, source: str) -> list[str]:
    if isinstance(value, dict):
        raise ConfigError(
            f"{source}: trusted must be a list of crew names, not a table; each trusted "
            "crew's membership comes from its own broker"
        )
    if not isinstance(value, list):
        raise ConfigError(f"{source}: trusted must be a list of crew names")
    names: list[str] = []
    for item in value:
        if isinstance(item, dict):
            leaked = [k for k in MEMBERSHIP_KEYS if k in item]
            what = f"membership keys {leaked}" if leaked else "a table"
            raise ConfigError(
                f"{source}: trusted entries are crew names only, not {what}; each trusted "
                "crew's membership comes from its own broker"
            )
        if not isinstance(item, str) or not NAME_RE.fullmatch(item):
            raise ConfigError(f"{source}: trusted entry {item!r} is not a crew name")
        names.append(item)
    return names
