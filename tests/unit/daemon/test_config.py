"""Story 1.4: `/etc/crewchat/servers.toml` (§5.B; R-06)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from crewchat.config import LEGEND_DIR_MAX, ConfigError, legend_dir, load_config, parse_config

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


def _research(raw: dict[str, Any], **server: Any) -> dict[str, Any]:
    raw["mattermost"][0].update(kiosk=True, kiosk_research=True, **server)
    raw["crews"]["oaw"]["dev"]["agents_allow"] = ["forge.example.lan/partner-org"]
    return raw


def test_server_research_keys_parse() -> None:
    """#139: kiosk research and its corpus keys, off and empty by default."""
    (server,) = parse_config(_raw()).mattermost
    assert server.kiosk_research is False
    assert server.corpus_extra == () and server.corpus_exclude == ()
    assert server.corpus_refresh_minutes == 15
    assert server.kiosk_max_turns == 8
    raw = _research(
        _raw(),
        corpus_extra=["forge.example.lan/partner-org/docs"],
        corpus_exclude=["forge.example.lan/partner-org/group/infra"],
        corpus_refresh_minutes=60,
        kiosk_max_turns=4,
    )
    (server,) = parse_config(raw).mattermost
    assert server.kiosk_research is True
    assert server.corpus_extra == ("forge.example.lan/partner-org/docs",)
    assert server.corpus_exclude == ("forge.example.lan/partner-org/group/infra",)
    assert server.corpus_refresh_minutes == 60
    assert server.kiosk_max_turns == 4


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"kiosk_research": "yes"}, "kiosk_research must be true or false"),
        ({"id": "status"}, "mattermost id 'status' is reserved"),
        ({"id": ".."}, "mattermost id '..' is reserved"),
        ({"corpus_refresh_minutes": 0}, "corpus_refresh_minutes"),
        ({"corpus_refresh_minutes": True}, "corpus_refresh_minutes"),
        ({"kiosk_max_turns": 0}, "kiosk_max_turns must be an integer of at least 1"),
        ({"kiosk_max_turns": True}, "kiosk_max_turns"),
        ({"corpus_extra": "forge.example.lan/a/b"}, "corpus_extra must be an array"),
        ({"corpus_extra": ["forge.example.lan/org"]}, "is not host/group/.../repo"),
        ({"corpus_extra": ["forge.example.lan/org/repo.git/"]}, "is not host/group/.../repo"),
        ({"corpus_exclude": ["forge.example.lan/org/../x"]}, "is not host/group/.../repo"),
        ({"corpus_exclude": ["Forge.lan/org/repo"]}, "is not host/group/.../repo"),
        ({"kiosk": False}, "kiosk_research needs kiosk = true"),
    ],
)
def test_server_research_refused(change: dict[str, Any], message: str) -> None:
    raw = _research(_raw())
    raw["mattermost"][0].update(change)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


@pytest.mark.parametrize(
    ("sid", "message"),
    [
        ("oaw-dev", r"is \[a-z0-9_\]\+ \(no '-', no '\.'\), not 'oaw-dev'"),
        ("dev.model", r"no '\.'"),
        ("control", "serves no server 'control'"),
        ("x" * 80, "too long for /run/crewkiosk-broker/x+\\.model\\.sock"),
        ("x" * 21, "too long for its user, crewkiosk-r-x{21}$"),
    ],
)
def test_research_server_id_refused(sid: str, message: str) -> None:
    """#142: a research server's id is crewkiosk-research-units' own, or its units are never
    written and every answer would frame to a socket that doesn't exist. The same id with
    research off still loads."""
    raw = _research(_raw(), id=sid)
    raw["crews"]["oaw"][sid] = raw["crews"]["oaw"].pop("dev")
    with pytest.raises(ConfigError, match=rf"mattermost {re.escape(sid)}\.kiosk_research: "):
        parse_config(raw)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)
    raw["mattermost"][0]["kiosk_research"] = False
    parse_config(raw)


def test_research_needs_allowlist_on_every_crew() -> None:
    """D-112: an absent agents_allow admits every agent, so research refuses it."""
    raw = _research(_raw())
    del raw["crews"]["oaw"]["dev"]["agents_allow"]
    with pytest.raises(ConfigError, match=r"needs agents_allow on crews\.oaw\.dev"):
        parse_config(raw)
    raw = _research(_raw())
    raw["crews"]["wave"] = {
        "needs_list": "bj:needs",
        "dev": {"broker_socket": "/run/crewbroker/wave-dev.sock", "broker_user_id": "b-wave"},
    }
    with pytest.raises(ConfigError, match=r"crews\.wave\.dev"):
        parse_config(raw)
    raw["crews"]["wave"]["dev"]["agents_allow"] = []  # explicit, even if empty
    parse_config(raw)


def test_allowlist_entry_forms() -> None:
    raw = _raw()
    raw["crews"]["oaw"]["dev"].update(
        agents_allow=["forge.example.lan/partner-org", "/home/agent/work", "name:helper"],
        agents_deny=["name:scratch"],
    )
    broker = parse_config(raw).crews["oaw"].brokers["dev"]
    want = ("forge.example.lan/partner-org", "/home/agent/work", "name:helper")
    assert broker.agents_allow == want
    assert broker.agents_deny == ("name:scratch",)
    raw["crews"]["oaw"]["dev"]["agents_allow"] = []
    assert parse_config(raw).crews["oaw"].brokers["dev"].agents_allow == ()  # nobody

    for key, bad in (
        ("agents_allow", ["https://forge.example.lan/partner-org"]),  # a URL, not a key
        ("agents_allow", ["Forge.example.lan/partner-org"]),  # repo_key lowercases the host
        ("agents_allow", ["forge.example.lan/partner-org/sub"]),  # only the top-level group
        ("agents_allow", ["relative/path/x"]),
        ("agents_allow", ["name:"]),
        ("agents_allow", ["name:Bad Name"]),
        ("agents_allow", "forge.example.lan/partner-org"),  # not an array
        ("agents_deny", [3]),
    ):
        raw = _raw()
        raw["crews"]["oaw"]["dev"][key] = bad
        with pytest.raises(ConfigError, match=key):
            parse_config(raw)


def test_legend_dir() -> None:
    """#231: only the last element of a register's project_dir is ever kept."""
    assert legend_dir("") == ""
    assert legend_dir("   ") == ""
    assert legend_dir("crew-231") == "crew-231"
    assert legend_dir("/home/agent/work/crew-231/") == "crew-231"
    assert legend_dir("x" * LEGEND_DIR_MAX) == "x" * LEGEND_DIR_MAX
    for bad in (None, 7, ["a"], "/", "..", "x" * (LEGEND_DIR_MAX + 1), "a\nb", "a\x1bb"):
        assert legend_dir(bad) is None, bad
