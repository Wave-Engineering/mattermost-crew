"""Story 1.3: the broker's configuration file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from crewbroker.config import ConfigError, load_config, read_server_url

BASE = """
server_id = "dev"
server_url = "http://mm.invalid"
human_user_ids = ["human-user-id"]
"""


def write(tmp_path: Path, body: str, instance: str = "oaw-dev") -> Path:
    (tmp_path / f"{instance}.toml").write_text(body)
    return tmp_path


def test_trusted_config_parses(tmp_path: Path) -> None:
    write(tmp_path, 'crew = "oaw"\ntrusted = ["otter", "kelp.crew"]\n' + BASE)
    config = load_config("oaw-dev", config_dir=tmp_path)
    assert config.crew == "oaw"
    assert config.trusted == ["otter", "kelp.crew"]
    assert config.server_id == "dev" and config.server_url == "http://mm.invalid"
    assert config.human_user_ids == ["human-user-id"]

    bad = [
        # a table form
        '[trusted.otter]\nbroker_user_id = "x"\n',
        '[trusted]\notter = true\n',
        # membership keys inline
        'trusted = [{name = "otter", broker_user_id = "x"}]\n',
        'trusted = [{name = "otter", human_user_ids = ["h"]}]\n',
        'trusted = [{name = "otter", minted_bot_ids = ["b"]}]\n',
        # not a name
        'trusted = ["Otter"]\n',
        'trusted = "otter"\n',
    ]
    for body in bad:
        write(tmp_path, 'crew = "oaw"\n' + BASE + body)
        with pytest.raises(ConfigError):
            load_config("oaw-dev", config_dir=tmp_path)


def test_membership_keys_never_configured(tmp_path: Path) -> None:
    for key in ('broker_user_id = "x"', 'minted_bot_ids = ["b"]'):
        write(tmp_path, f'crew = "oaw"\n{key}\n' + BASE)
        with pytest.raises(ConfigError, match="never configured"):
            load_config("oaw-dev", config_dir=tmp_path)


def test_crew_defaults_to_partner_username(tmp_path: Path, fake: Any) -> None:
    write(tmp_path, BASE)
    config = load_config("oaw-dev", config_dir=tmp_path, client=fake)
    assert config.crew == "bj"
    assert config.broker_user_id == "broker-user-id"
    # Without a server to ask, an absent crew is an error, not a guess.
    with pytest.raises(ConfigError, match="crew is absent"):
        load_config("oaw-dev", config_dir=tmp_path)
    # An explicit crew overrides the default (R-02).
    write(tmp_path, 'crew = "oaw"\n' + BASE)
    assert load_config("oaw-dev", config_dir=tmp_path, client=fake).crew == "oaw"


TEAM_ID = "1uwg3a3hqifrudqfg6rykz58gc"
CHANNEL_IDS = ["oqgfseojobrtxxbrp7or9is1ho", "pycuk7d1zpbuinkuw46syzciuo"]


def test_membership_keys_default_empty(tmp_path: Path) -> None:
    """#132: neither key set is today's behaviour, so an existing config is unchanged."""
    write(tmp_path, 'crew = "oaw"\n' + BASE)
    config = load_config("oaw-dev", config_dir=tmp_path)
    assert config.team_id == "" and config.default_channel_ids == []

    write(tmp_path, f'crew = "oaw"\nteam_id = "{TEAM_ID}"\n' + BASE)
    assert load_config("oaw-dev", config_dir=tmp_path).default_channel_ids == []
    channels = ", ".join(f'"{c}"' for c in CHANNEL_IDS)
    body = f'crew = "oaw"\nteam_id = "{TEAM_ID}"\ndefault_channel_ids = [{channels}]\n'
    write(tmp_path, body + BASE)
    config = load_config("oaw-dev", config_dir=tmp_path)
    assert config.team_id == TEAM_ID and config.default_channel_ids == CHANNEL_IDS


def test_membership_keys_take_ids_not_names(tmp_path: Path) -> None:
    team = f'team_id = "{TEAM_ID}"\n'
    bad = {
        'team_id = "oaw"\n': "team_id",
        "team_id = 7\n": "team_id",
        f'team_id = "{TEAM_ID.upper()}"\n': "team_id",
        team + 'default_channel_ids = ["town-square"]\n': "default_channel_ids",
        team + f'default_channel_ids = ["{CHANNEL_IDS[0]}", "off-topic"]\n': "default_channel_ids",
        team + f'default_channel_ids = "{CHANNEL_IDS[0]}"\n': "default_channel_ids",
        # Channels need their team: a bot joins a channel only once it is on the team.
        f'default_channel_ids = ["{CHANNEL_IDS[0]}"]\n': "needs team_id",
    }
    for body, named in bad.items():
        write(tmp_path, 'crew = "oaw"\n' + body + BASE)
        with pytest.raises(ConfigError, match=named):
            load_config("oaw-dev", config_dir=tmp_path)


def test_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no such file"):
        load_config("nope-dev", config_dir=tmp_path)
    write(tmp_path, "not = [toml")
    with pytest.raises(ConfigError):
        load_config("oaw-dev", config_dir=tmp_path)
    write(tmp_path, 'crew = "oaw"\nserver_id = "dev"\nserver_url = "u"\nhuman_user_ids = []\n')
    with pytest.raises(ConfigError, match="human_user_ids"):
        load_config("oaw-dev", config_dir=tmp_path)
    write(tmp_path, BASE)
    assert read_server_url("oaw-dev", config_dir=tmp_path) == "http://mm.invalid"
