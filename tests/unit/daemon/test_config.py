"""Story 1.4: `/etc/crewchat/servers.toml` (§5.B; R-06)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from crewchat.config import ConfigError, load_config, parse_config

TOML = """
default_crew = "oaw"

[[mattermost]]
id = "dev"
url = "http://mm.invalid"

[discord]
enabled = true

[crews.oaw]
needs_list = "bj:needs"

[crews.oaw.dev]
broker_socket = "/run/crewbroker/oaw-dev.sock"
broker_user_id = "b-oaw"
"""


def test_servers_toml_parses(tmp_path: Path) -> None:
    path = tmp_path / "servers.toml"
    path.write_text(TOML)
    cfg = load_config(path)
    assert [s.id for s in cfg.mattermost] == ["dev"]
    assert cfg.discord_enabled is True
    assert cfg.default_crew == "oaw"
    assert cfg.broker_timeout_s == 2.0
    assert cfg.crews["oaw"].needs_list == "bj:needs"
    broker = cfg.crews["oaw"].brokers["dev"]
    assert str(broker.broker_socket) == "/run/crewbroker/oaw-dev.sock"
    assert cfg.broker_user_ids() == ["b-oaw"]


def _raw() -> dict[str, Any]:
    return tomllib.loads(TOML)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r.pop("default_crew"), "default_crew"),
        (lambda r: r.update(default_crew="otter"), "default_crew"),
        (lambda r: r["crews"]["oaw"].pop("needs_list"), "needs_list"),
        (lambda r: r["crews"]["oaw"]["dev"].pop("broker_user_id"), "broker_user_id"),
        (lambda r: r["crews"]["oaw"]["dev"].update(broker_socket="rel.sock"), "absolute"),
        (lambda r: r["crews"]["oaw"].update(prod={"broker_socket": "/x"}), "no \\[\\[mattermost"),
        (lambda r: r.update(broker_timeout_s=0), "broker_timeout_s"),
        (lambda r: r["mattermost"].append({"id": "dev", "url": "x"}), "twice"),
        (lambda r: r.update(discord={"enabled": "yes"}), "discord.enabled"),
    ],
)
def test_servers_toml_rejects(change: Any, message: str) -> None:
    raw = _raw()
    change(raw)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no such file"):
        load_config(tmp_path / "nope.toml")
