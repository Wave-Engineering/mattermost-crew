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


def test_squawkie_table() -> None:
    """Story 3.2: `[squawkie]` is optional; `credential` is an absolute path, with a url."""
    raw = _raw()
    cfg = parse_config(raw)
    assert cfg.squawkie_url is None and cfg.squawkie_credential is None
    raw["squawkie"] = {"url": "http://sq.lan:3000", "credential": "/etc/crewchat/sq.credential"}
    cfg = parse_config(raw)
    assert cfg.squawkie_url == "http://sq.lan:3000"
    assert cfg.squawkie_credential == Path("/etc/crewchat/sq.credential")
    assert parse_config({**raw, "squawkie": {"url": "http://x"}}).squawkie_credential is None
    for bad, message in (
        ("nope", "squawkie must be a table"),
        ({"url": ""}, "squawkie.url"),
        ({"url": "http://x", "credential": "rel"}, "absolute"),
        ({"credential": "/etc/x"}, "needs squawkie.url"),
    ):
        with pytest.raises(ConfigError, match=message):
            parse_config({**raw, "squawkie": bad})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no such file"):
        load_config(tmp_path / "nope.toml")


def test_config_retention_default() -> None:
    """D-104: `my_threads_retention_days` is top-level, defaults to 90, and is at least 1."""
    raw = tomllib.loads(TOML)
    assert parse_config(raw).my_threads_retention_days == 90
    assert parse_config({**raw, "my_threads_retention_days": 7}).my_threads_retention_days == 7
    assert parse_config({**raw, "my_threads_retention_days": 1}).my_threads_retention_days == 1
    for bad in (0, -3, 1.5, "90", True):
        with pytest.raises(ConfigError, match="my_threads_retention_days"):
            parse_config({**raw, "my_threads_retention_days": bad})
    # A `[daemon]` table is not where it lives: the top-level default still applies.
    assert (
        parse_config({**raw, "daemon": {"my_threads_retention_days": 5}}).my_threads_retention_days
        == 90
    )


def test_config_filter_settings() -> None:
    """Story 3.3: the filters' settings are top-level, with the Dev Spec's defaults."""
    raw = tomllib.loads(TOML)
    cfg = parse_config(raw)
    assert (cfg.crew_info_max_age_s, cfg.refetch_min_interval_s) == (300.0, 2.0)
    assert (cfg.kiosk_rate_per_hour, cfg.loop_guard_window) == (10, 20)
    cfg = parse_config(
        {
            **raw,
            "crew_info_max_age_s": 60,
            "refetch_min_interval_s": 0.5,
            "kiosk_rate_per_hour": 3,
            "loop_guard_window": 5,
        }
    )
    assert (cfg.crew_info_max_age_s, cfg.refetch_min_interval_s) == (60.0, 0.5)
    assert (cfg.kiosk_rate_per_hour, cfg.loop_guard_window) == (3, 5)
    for key, bad in (
        ("crew_info_max_age_s", 0),
        ("refetch_min_interval_s", -1),
        ("crew_info_max_age_s", True),
        ("kiosk_rate_per_hour", 0),
        ("kiosk_rate_per_hour", 2.5),
        ("loop_guard_window", "20"),
    ):
        with pytest.raises(ConfigError, match=key):
            parse_config({**raw, key: bad})


# -- per-server controls (#117) --------------------------------------------------------------
def test_server_kiosk_defaults_off() -> None:
    (server,) = parse_config(_raw()).mattermost
    assert server.kiosk is False and server.kiosk_daily_cap == 25
    raw = _raw()
    raw["mattermost"][0].update(kiosk=True, kiosk_daily_cap=0)
    (server,) = parse_config(raw).mattermost
    assert server.kiosk is True and server.kiosk_daily_cap == 0  # 0: no kiosk answers
    broker = parse_config(_raw()).crews["oaw"].brokers["dev"]
    assert broker.agents_allow is None and broker.agents_deny == ()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"kiosk": "yes"}, "kiosk must be true or false"),
        ({"kiosk": 1}, "kiosk must be true or false"),
        ({"kiosk_daily_cap": -1}, "kiosk_daily_cap"),
        ({"kiosk_daily_cap": 2.5}, "kiosk_daily_cap"),
        ({"kiosk_daily_cap": True}, "kiosk_daily_cap"),
    ],
)
def test_server_kiosk_types_refused(change: dict[str, Any], message: str) -> None:
    raw = _raw()
    raw["mattermost"][0].update(change)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_allowlist_entry_forms() -> None:
    raw = _raw()
    raw["crews"]["oaw"]["dev"].update(
        agents_allow=["gitlab.com/partner-org", "/home/agent/work", "name:helper"],
        agents_deny=["name:scratch"],
    )
    broker = parse_config(raw).crews["oaw"].brokers["dev"]
    assert broker.agents_allow == ("gitlab.com/partner-org", "/home/agent/work", "name:helper")
    assert broker.agents_deny == ("name:scratch",)
    raw["crews"]["oaw"]["dev"]["agents_allow"] = []
    assert parse_config(raw).crews["oaw"].brokers["dev"].agents_allow == ()  # nobody

    for key, bad in (
        ("agents_allow", ["https://gitlab.com/partner-org"]),  # a URL, not a key
        ("agents_allow", ["GitLab.com/partner-org"]),  # repo_key lowercases the host
        ("agents_allow", ["gitlab.com/partner-org/sub"]),  # only the top-level group
        ("agents_allow", ["relative/path/x"]),
        ("agents_allow", ["name:"]),
        ("agents_allow", ["name:Bad Name"]),
        ("agents_allow", "gitlab.com/partner-org"),  # not an array
        ("agents_deny", [3]),
    ):
        raw = _raw()
        raw["crews"]["oaw"]["dev"][key] = bad
        with pytest.raises(ConfigError, match=key):
            parse_config(raw)
