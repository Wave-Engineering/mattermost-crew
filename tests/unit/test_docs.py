"""Story 3.7: the runbook, the partner guide and the architecture document.

The runbook's install and verify blocks are run for real by IT-20
(`tests/integration/test_runbook.py`). These tests check, without a host, what IT-20 relies on and
what it can't see: the sections, the blocks it extracts, their syntax, the Configure section
against the real config parsers, and the partner guide against the broker's command parser.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from datetime import UTC, datetime

from crewbroker import config as broker_config
from crewbroker.switch import USAGE, SwitchState, apply_command
from crewchat import config as daemon_config

from tests.required import missing
from tests.runbook_md import DOCS, RUNBOOK, VERIFY_HEADING, fenced_blocks, runbook_blocks, sections

PARTNER_GUIDE = DOCS / "partner-guide.md"
ARCHITECTURE = DOCS / "architecture.md"

REQUIRED_SECTIONS = (
    "Prerequisites",
    "Install the daemon",
    "Install the broker",
    "Configure",
    VERIFY_HEADING,
    "Upgrade",
    "Rollback",
)


def runbook_sections() -> dict[str, str]:
    return dict(sections(RUNBOOK.read_text(encoding="utf-8")))


# --- the runbook --------------------------------------------------------------------------------


def test_runbook_has_required_sections() -> None:
    headings = runbook_sections()
    missing = [s for s in REQUIRED_SECTIONS if s not in headings]
    assert missing == [], f"docs/runbook.md lacks sections: {missing}"


def test_runbook_blocks_are_the_install_and_verify_bash_blocks_in_order() -> None:
    blocks = runbook_blocks(RUNBOOK.read_text(encoding="utf-8"))
    order = list(dict.fromkeys(heading for heading, _ in blocks))
    assert order[:2] == ["Install the daemon", "Install the broker"]
    assert order[-1] == VERIFY_HEADING
    assert all(h.startswith("Install ") for h in order[:-1])
    # The verify block runs `make verify-install`, and is the last thing IT-20 runs.
    assert "make verify-install" in blocks[-1][1]
    # Nothing outside those sections is ever run: Configure, Upgrade and Rollback use
    # `text`/`toml` fences.
    for heading in ("Prerequisites", "Configure", "Upgrade", "Rollback"):
        assert fenced_blocks(runbook_sections()[heading], "bash") == [], heading


def test_runbook_install_blocks_are_valid_bash() -> None:
    for heading, script in runbook_blocks(RUNBOOK.read_text(encoding="utf-8")):
        proc = subprocess.run(
            ["bash", "-n"], input=script, capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, f"'{heading}': {proc.stderr}"


def test_runbook_install_blocks_pass_shellcheck() -> None:
    if shutil.which("shellcheck") is None:
        missing("shellcheck")
    for heading, script in runbook_blocks(RUNBOOK.read_text(encoding="utf-8")):
        proc = subprocess.run(
            ["shellcheck", "--shell=bash", "-"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"'{heading}':\n{proc.stdout}"


def test_runbook_blocks_are_self_contained() -> None:
    """Each block runs in a new shell: it sets strict mode and names its own variables."""
    for heading, script in runbook_blocks(RUNBOOK.read_text(encoding="utf-8")):
        assert script.startswith("set -euo pipefail\n"), heading
        assert not re.search(r"^\s*cd\s", script, flags=re.M), f"'{heading}' changes directory"


def test_runbook_broker_is_one_instance_per_crew_and_server() -> None:
    body = runbook_sections()["Install the broker"]
    assert "crewbroker@<crew>-<server_id>" in body
    (script,) = fenced_blocks(body, "bash")
    assert "crewbroker@${instance}" in script
    assert "for entry in $CREWBROKER_INSTANCES" in script


def test_runbook_prerequisites_name_users_groups_and_the_mount() -> None:
    body = runbook_sections()["Prerequisites"]
    for needed in (
        "`crewchat`",
        "`crewbroker`",
        "`crewkiosk`",
        "`crewchat-clients`",
        "empty HOME",
        "never in `crewchat`",
        "tmpfiles",
        "Python 3.12",
        "uv",
        "-v /run/crewchat:/run/crewchat",
    ):
        assert needed in body, needed


def _toml_blocks() -> list[dict[str, object]]:
    return [tomllib.loads(b) for b in fenced_blocks(runbook_sections()["Configure"], "toml")]


def test_runbook_configure_examples_load_through_the_real_parsers() -> None:
    servers, broker = _toml_blocks()
    parsed = daemon_config.parse_config(servers, source="runbook servers.toml")
    assert parsed.broker_user_ids() == ["<the broker account's user id>"]
    crew = broker_config.parse_config(broker, source="runbook broker toml")
    assert crew.crew == parsed.default_crew
    assert crew.server_id == parsed.mattermost[0].id


def test_runbook_partner_server_example_loads() -> None:
    """#117: the per-server controls example, added to the servers.toml example, loads
    through the real parser, with the allowlist it shows."""
    body = runbook_sections()["Configure"]
    (servers_block,) = fenced_blocks(body, "toml")[:1]
    example = next(b for b in fenced_blocks(body, "text") if 'id = "partner"' in b)
    parsed = daemon_config.parse_config(tomllib.loads(servers_block + "\n" + example))
    partner = next(s for s in parsed.mattermost if s.id == "partner")
    assert partner.kiosk is False
    entry = parsed.crews["oaw"].brokers["partner"]
    assert entry.agents_allow == ("forge.example.lan/partner-org", "name:helper")
    assert entry.agents_deny == ("name:scratch",)
    assert "accident guard, not a security boundary" in body


def test_runbook_configure_lists_every_required_field() -> None:
    servers, broker = _toml_blocks()
    assert set(servers) >= {
        "default_crew",
        "broker_timeout_s",
        "kiosk_rate_per_hour",
        "loop_guard_window",
        "my_threads_retention_days",
        "mattermost",
        "discord",
        "crews",
    }
    # The defaults, as the spec and the code define them.
    assert servers["broker_timeout_s"] == daemon_config.DEFAULT_BROKER_TIMEOUT_S
    assert servers["kiosk_rate_per_hour"] == daemon_config.DEFAULT_KIOSK_RATE_PER_HOUR
    assert servers["loop_guard_window"] == daemon_config.DEFAULT_LOOP_GUARD_WINDOW
    assert servers["my_threads_retention_days"] == daemon_config.DEFAULT_MY_THREADS_RETENTION_DAYS
    mattermost = servers["mattermost"]
    assert isinstance(mattermost, list)
    assert set(mattermost[0]) == {"id", "url", "kiosk", "kiosk_daily_cap"}
    # The per-server kiosk keys at their defaults (#117): off, and 25 a day.
    assert mattermost[0]["kiosk"] is False
    assert mattermost[0]["kiosk_daily_cap"] == daemon_config.DEFAULT_KIOSK_DAILY_CAP
    assert servers["discord"] == {"enabled": False}
    crews = servers["crews"]
    assert isinstance(crews, dict)
    (crew_table,) = crews.values()
    assert "needs_list" in crew_table
    (server_table,) = (v for v in crew_table.values() if isinstance(v, dict))
    assert set(server_table) == {"broker_socket", "broker_user_id"}

    assert set(broker) == {"crew", "server_id", "server_url", "human_user_ids", "trusted"}
    body = runbook_sections()["Configure"]
    assert 'trusted = ["<crew>", …]' in body
    assert "`broker_user_id` is never configured" in body
    assert "/users/me" in body


def test_runbook_verify_names_the_known_credentials() -> None:
    body = runbook_sections()[VERIFY_HEADING]
    assert "test -e <file> && ! sudo -u agent test -r <file>" in body
    for name in (
        "/etc/crewbroker/<crew>-<server_id>.credential",
        "/var/lib/crewbroker/<crew>-<server_id>/tokens.json",
        "/var/lib/crewchat/tokens/verify-canary.json",
        "/etc/crewchat/discord.credential",
        "crewkiosk",
    ):
        assert name in body, name


# --- the partner guide --------------------------------------------------------------------------


def usage_commands() -> list[str]:
    """The commands the broker's own usage reply lists: `social <duration>`, `trusted`, …"""
    return [c for c in re.findall(r"`([^`]+)`", USAGE) if not c.startswith("social ") or "<" in c]


def guide_commands() -> list[str]:
    """The commands in the partner guide's table: the first cell of each row."""
    body = dict(sections(PARTNER_GUIDE.read_text(encoding="utf-8")))["The commands"]
    return re.findall(r"^\| `([^`]+)` \|", body, flags=re.M)


def accepted(command: str) -> bool:
    """True when `apply_command` acts on `command`, rather than answering with the usage."""
    cfg = broker_config.CrewConfig(
        crew="oaw", server_id="dev", server_url="http://mm", human_user_ids=["partner"]
    )
    text = command.replace("<duration>", "2h")
    _, reply = apply_command(SwitchState(), "partner", text, cfg, datetime.now(UTC))
    return USAGE not in reply and not reply.startswith("not a duration")


def test_partner_guide_lists_every_command() -> None:
    parser = usage_commands()
    assert parser, "no commands found in crewbroker.switch.USAGE"
    # The usage text is the parser's own list: each of its commands is really accepted.
    assert all(accepted(c) for c in parser), parser
    documented = guide_commands()
    assert sorted(documented) == sorted(parser), (documented, parser)
    # And nothing the guide documents is refused by the parser.
    assert all(accepted(c) for c in documented)


def test_partner_guide_rejects_an_unknown_command() -> None:
    assert not accepted("open sesame")


def test_partner_guide_covers_per_server_expiry_kiosk_and_review() -> None:
    text = PARTNER_GUIDE.read_text(encoding="utf-8")
    heads = [h for h, _ in sections(text)]
    for needed in (
        "The switch is per server",
        "What each mode delivers",
        "Expiry and lapse notices",
        "The kiosk",
        "The review on social engage",
        "Writing an agent's `## Kiosk` section",
    ):
        assert needed in heads, needed
    assert "social lapsed → crew" in text  # the broker's lapse notice, verbatim
    assert "I have nothing to share yet." in text  # the kiosk's fixed reply, verbatim


# --- the architecture document ------------------------------------------------------------------


def test_architecture_states_the_trust_boundaries() -> None:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    parts = dict(sections(text))
    for needed in (
        "System context",
        "Components",
        "Trust boundaries",
        "Membership",
        "Data paths",
        "Per backend",
    ):
        assert needed in parts, needed
    boundaries = parts["Trust boundaries"]
    for needed in (
        "/run/crewchat/crewchat.sock",
        "`crewchat` user",
        "`crewbroker` user",
        "`crewkiosk` user",
    ):
        assert needed in boundaries, needed
    membership = " ".join(parts["Membership"].split())
    for needed in (
        "owner_id",
        "minted_bot_ids",
        "Username suffixes are never used",
        "No broker ever calls another broker",
        "that crew's own broker reports",
    ):
        assert needed in membership, needed
    for needed in ("Push (the doorbell)", "Pull (`read`, `thread`)", "Kiosk"):
        assert f"### {needed}" in parts["Data paths"], needed

_RED_FORMAT = 'single quotes are not ruff format style'
