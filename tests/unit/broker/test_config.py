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
        "[trusted]\notter = true\n",
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


def test_membership_keys_default_empty(tmp_path: Path) -> None:
    """#132: neither key set is today's behaviour, so an existing config is unchanged."""
    write(tmp_path, 'crew = "oaw"\n' + BASE)
    config = load_config("oaw-dev", config_dir=tmp_path)
    assert config.team == "" and config.default_channels == []

    write(tmp_path, 'crew = "oaw"\nteam = "oaw"\n' + BASE)
    assert load_config("oaw-dev", config_dir=tmp_path).default_channels == []
    # URL names and display names alike: whether they exist is the server's to say.
    body = 'crew = "oaw"\nteam = "Oak and Wave"\ndefault_channels = ["town-square", "Agent Ops"]\n'
    write(tmp_path, body + BASE)
    config = load_config("oaw-dev", config_dir=tmp_path)
    assert config.team == "Oak and Wave"
    assert config.default_channels == ["town-square", "Agent Ops"]


def test_membership_keys_refuse_empty_and_duplicate_entries(tmp_path: Path) -> None:
    team = 'team = "oaw"\n'
    bad = {
        'team = ""\n': "team",
        'team = "  "\n': "team",
        "team = 7\n": "team",
        team + 'default_channels = [""]\n': "default_channels",
        team + 'default_channels = ["town-square", 7]\n': "default_channels",
        team + 'default_channels = "town-square"\n': "default_channels",
        team + 'default_channels = ["General", "general"]\n': "twice",
        # Channels need their team: a bot joins a channel only once it is on the team.
        'default_channels = ["town-square"]\n': "needs team",
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


def test_config_reconcile_and_tag_keys(tmp_path: Path) -> None:
    """#167: the reconcile interval defaults to 300 s (0 is off), the tag to `crew-<crew>`."""
    write(tmp_path, 'crew = "oaw"\n' + BASE)
    config = load_config("oaw-dev", config_dir=tmp_path)
    assert config.membership_reconcile_s == 300
    assert config.channel_tag == "crew-oaw"
    write(tmp_path, 'crew = "oaw"\nmembership_reconcile_s = 0\nchannel_tag = "oaw_ops"\n' + BASE)
    config = load_config("oaw-dev", config_dir=tmp_path)
    assert (config.membership_reconcile_s, config.channel_tag) == (0, "oaw_ops")

    bad = {
        "membership_reconcile_s = -1\n": "membership_reconcile_s",
        "membership_reconcile_s = 1.5\n": "membership_reconcile_s",
        "membership_reconcile_s = true\n": "membership_reconcile_s",
        'membership_reconcile_s = "300"\n': "membership_reconcile_s",
        'channel_tag = "Crew OaW"\n': "channel_tag",
        'channel_tag = ""\n': "channel_tag",
        "channel_tag = 7\n": "channel_tag",
    }
    for body, named in bad.items():
        write(tmp_path, 'crew = "oaw"\n' + body + BASE)
        with pytest.raises(ConfigError, match=named):
            load_config("oaw-dev", config_dir=tmp_path)
