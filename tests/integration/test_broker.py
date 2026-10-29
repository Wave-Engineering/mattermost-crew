"""Story 1.3 against the CI leg's Mattermost: the broker's accounts, tokens and prune.

Each module run creates its own broker account and human partner, gives the broker the
patched role with `scripts/ci/broker-role-patch.sh`, and drives `crewbroker.service.Broker`
in process, with an injected clock where time matters (IT-03).
"""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from crewbroker import mint
from crewbroker.config import CrewConfig, parse_config
from crewbroker.mattermost import MattermostClient
from crewbroker.membership import Membership
from crewbroker.service import Broker, OpError
from crewchat.registry import account_name as daemon_account_name

from tests.integration.mmhttp import mm_request

ROOT = Path(__file__).resolve().parents[2]
ROLE_PATCH = ROOT / "scripts" / "ci" / "broker-role-patch.sh"
PATCHED = {"create_bot", "read_bots", "manage_bots"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def mkuser(mattermost: Any, username: str) -> str:
    resp = mm_request(
        mattermost,
        "POST",
        "/users",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": f"It-{secrets.token_hex(8)}-Aa1!",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@pytest.fixture(scope="module")
def setup(mattermost: Any) -> Iterator[dict[str, str]]:
    run = secrets.token_hex(3)
    crew = f"it{run}"
    broker_id = mkuser(mattermost, f"crewbroker-{run}")
    partner_id = mkuser(mattermost, f"partner-{run}")
    env = {**os.environ, "MM_URL": mattermost.url, "MM_ADMIN_TOKEN": mattermost.admin_token}
    subprocess.run([str(ROLE_PATCH), broker_id], env=env, check=True)
    resp = mm_request(mattermost, "POST", f"/users/{broker_id}/tokens", json={"description": "it"})
    assert resp.status_code == 200, resp.text
    yield {
        "crew": crew,
        "broker_id": broker_id,
        "partner_id": partner_id,
        "broker_token": resp.json()["token"],
    }
    # This module's broker account is not in servers.toml, so it gives the patched role back:
    # IT-01 asserts that only the configured brokers hold it (R-06).
    resp = mm_request(mattermost, "PUT", f"/users/{broker_id}/roles", json={"roles": "system_user"})
    assert resp.status_code == 200, resp.text


def test_it17_crew_defaults_to_partner_username_and_can_be_overridden(
    mattermost: Any, setup: dict[str, str]
) -> None:
    """IT-17, R-02: no `crew` in the config → the partner's username; a `crew` overrides it."""
    client = MattermostClient(mattermost.url, setup["broker_token"])
    raw: dict[str, Any] = {
        "server_id": "ci",
        "server_url": mattermost.url,
        "human_user_ids": [setup["partner_id"]],
    }
    try:
        defaulted = parse_config(dict(raw), client=client)
        assert defaulted.crew == user(mattermost, setup["partner_id"])["username"]
        assert defaulted.broker_user_id == setup["broker_id"]
        assert parse_config({**raw, "crew": "override"}, client=client).crew == "override"
    finally:
        client.close()


def test_it17_account_naming_fits_ct05(mattermost: Any, broker: Broker) -> None:
    """IT-17, R-01: `<dev>.<crew>` up to 64 characters is created; 65 is refused before the
    server is asked. The daemon names the account exactly as the broker does."""
    crew = broker.config.crew
    dev = "n" * (64 - len(crew) - 1)
    minted = broker.mint(args(broker, dev))
    assert minted["account"] == f"{dev}.{crew}" == daemon_account_name(dev, crew)
    assert len(minted["account"]) == 64
    found = mm_request(mattermost, "GET", f"/users/username/{minted['account']}").json()
    assert found["is_bot"] is True
    with pytest.raises(OpError, match="bad_request"):
        broker.mint(args(broker, dev + "n"))
    with pytest.raises(ValueError):
        daemon_account_name(dev + "n", crew)
    for bad in ("Upper", "sp ace", "slash/x"):
        with pytest.raises(OpError, match="bad_request"):
            broker.mint(args(broker, bad))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def broker(mattermost: Any, setup: dict[str, str], tmp_path: Path, clock: Clock) -> Broker:
    mint.clear_expiry_cache()
    client = MattermostClient(mattermost.url, setup["broker_token"])
    config = CrewConfig(
        crew=setup["crew"],
        server_id="ci",
        server_url=mattermost.url,
        human_user_ids=[setup["partner_id"]],
        trusted=[],
        broker_user_id=setup["broker_id"],
    )
    return Broker(config, client, tmp_path / "state", clock=clock)


def args(broker: Broker, dev: str) -> dict[str, str]:
    return {"dev_name": dev, "crew": broker.config.crew}


def user(mattermost: Any, user_id: str) -> dict[str, Any]:
    resp = mm_request(mattermost, "GET", f"/users/{user_id}")
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()
    return data


def token_works(mattermost: Any, token: str) -> bool:
    return mm_request(mattermost, "GET", "/users/me", token=token).status_code == 200


def test_broker_role_is_patched_and_not_admin(mattermost: Any, setup: dict[str, str]) -> None:
    """R-06: the broker account holds the patched role and no system-admin role."""
    me = mm_request(mattermost, "GET", "/users/me", token=setup["broker_token"]).json()
    roles = set(me["roles"].split())
    assert roles == {"system_user", "system_user_access_token"}
    assert "system_admin" not in roles
    role = mm_request(mattermost, "GET", "/roles/name/system_user_access_token").json()
    assert set(role["permissions"]) >= PATCHED


def test_ensure_bot_creates_once(
    mattermost: Any, broker: Broker, setup: dict[str, str]
) -> None:
    """R-01, R-03: `<dev>.<crew>` is created on the first call only."""
    first = broker.mint(args(broker, "once"))
    second = broker.mint(args(broker, "once"))
    name = f"once.{broker.config.crew}"
    assert first["account"] == second["account"] == name
    found = mm_request(mattermost, "GET", f"/users/username/{name}").json()
    assert found["is_bot"] is True
    assert broker.accounts.minted_bot_ids() == [found["id"]]
    bot = mm_request(mattermost, "GET", f"/bots/{found['id']}", token=setup["broker_token"]).json()
    assert bot["owner_id"] == setup["broker_id"]


def test_mint_token_works_with_native_expiry_where_supported(
    mattermost: Any, broker: Broker, setup: dict[str, str]
) -> None:
    """R-05, R-07: a working token; native expiry is set exactly when the server has it."""
    minted = broker.mint(args(broker, "expiry"))
    me = mm_request(mattermost, "GET", "/users/me", token=minted["token"]).json()
    assert me["username"] == minted["account"]
    meta = mm_request(
        mattermost, "GET", f"/users/tokens/{minted['token_id']}", token=setup["broker_token"]
    ).json()
    detected = "present" if meta.get("expires_at") else "absent"
    supported = mint.native_expiry_supported(
        broker.client, broker.server_key, broker.config.broker_user_id
    )
    assert detected == ("present" if supported else "absent")
    if mattermost.expected_native_token_expiry is not None:
        assert detected == mattermost.expected_native_token_expiry
    rec = broker.tokens.get(minted["token_id"])
    assert rec is not None and (rec.expires_at is not None) == supported


def test_remint_and_revoke_are_separate(mattermost: Any, broker: Broker) -> None:
    """R-05: the old token stays valid until it is revoked."""
    first = broker.mint(args(broker, "rotate"))
    second = broker.remint(args(broker, "rotate"))
    assert token_works(mattermost, first["token"]) and token_works(mattermost, second["token"])
    broker.revoke({"token_id": first["token_id"]})
    assert not token_works(mattermost, first["token"])
    assert token_works(mattermost, second["token"])


def test_crew_info_drops_reassigned_bot(
    mattermost: Any, broker: Broker, setup: dict[str, str]
) -> None:
    """R-22, D-100: a bot reassigned with POST /bots/{id}/assign/{user} drops out."""
    broker.mint(args(broker, "keep"))
    broker.mint(args(broker, "moved"))
    ids = broker.accounts.minted_bot_ids()
    keep_id, moved_id = ids
    info = broker.crew_info({})
    assert info["broker_user_id"] == setup["broker_id"]
    assert info["human_user_ids"] == [setup["partner_id"]]
    assert info["trusted"] == []
    assert info["minted_bot_ids"] == [keep_id, moved_id]

    resp = mm_request(
        mattermost,
        "POST",
        f"/bots/{moved_id}/assign/{setup['partner_id']}",
        token=setup["broker_token"],
    )
    assert resp.status_code == 200, resp.text
    assert broker.crew_info({})["minted_bot_ids"] == [keep_id]
    # And the squat check holds: the reassigned name can't be minted for.
    with pytest.raises(OpError, match="squatted"):
        broker.mint(args(broker, "moved"))

    resp = mm_request(mattermost, "POST", f"/bots/{moved_id}/assign/{setup['broker_id']}")
    assert resp.status_code == 200, resp.text
    assert broker.crew_info({})["minted_bot_ids"] == [keep_id, moved_id]


def test_pruned_agent_reactivated_on_register(
    mattermost: Any, broker: Broker, clock: Clock
) -> None:
    """R-03, R-30: a pruned account is reactivated by its next register, then mint works."""
    first = broker.mint(args(broker, "pruned"))
    bot_id = broker.accounts.minted_bot_ids()[0]
    clock.now += timedelta(days=31)
    assert broker.prune() == [first["account"]]
    assert user(mattermost, bot_id)["delete_at"] > 0
    assert not token_works(mattermost, first["token"])
    assert broker.crew_info({})["minted_bot_ids"] == []

    again = broker.mint(args(broker, "pruned"))
    assert user(mattermost, bot_id)["delete_at"] == 0
    assert token_works(mattermost, again["token"])
    assert broker.crew_info({})["minted_bot_ids"] == [bot_id]


def test_deactivate_leaves_the_bot_deactivated(mattermost: Any, broker: Broker) -> None:
    """#127: `deactivate` disables the bot on the server with the broker's own role, and the
    `delete_at` it answers is the server's. The next mint reactivates it, as after a prune."""
    first = broker.mint(args(broker, "canary"))
    bot_id = broker.accounts.minted_bot_ids()[0]

    reply = broker.deactivate(args(broker, "canary"))

    assert reply["account"] == first["account"]
    assert reply["delete_at"] > 0
    assert user(mattermost, bot_id)["delete_at"] > 0
    assert not token_works(mattermost, first["token"])
    again = broker.mint(args(broker, "canary"))
    assert user(mattermost, bot_id)["delete_at"] == 0
    assert token_works(mattermost, again["token"])


def test_mint_refuses_squatted_name(mattermost: Any, broker: Broker) -> None:
    """R-01, R-03: an existing account not owned by this broker → loud failure, no token."""
    crew = broker.config.crew
    mkuser(mattermost, f"human.{crew}")
    resp = mm_request(
        mattermost,
        "POST",
        "/bots",
        json={"username": f"adminbot.{crew}", "display_name": "not ours"},
    )
    assert resp.status_code == 201, resp.text
    for dev in ("human", "adminbot"):
        with pytest.raises(OpError, match="squatted"):
            broker.mint(args(broker, dev))
    assert broker.accounts.minted_bot_ids() == []
    assert not broker.tokens.path.exists()


def test_it03_prune_counts_register_not_remint(
    mattermost: Any, broker: Broker, clock: Clock
) -> None:
    """IT-03 (R-30): re-minted daily but no register for 31 days → deactivated; 29 → kept."""
    start = clock.now
    broker.mint(args(broker, "idle"))
    for _ in range(31):
        clock.now += timedelta(days=1)
        rotated = broker.remint(args(broker, "idle"))
        broker.revoke({"token_id": rotated["token_id"]})
    clock.now = start + timedelta(days=2)
    busy = broker.mint(args(broker, "busy"))
    clock.now = start + timedelta(days=31)

    crew = broker.config.crew
    assert broker.prune() == [f"idle.{crew}"]
    idle_id, busy_id = broker.accounts.minted_bot_ids()
    assert user(mattermost, idle_id)["delete_at"] > 0
    assert user(mattermost, busy_id)["delete_at"] == 0
    assert token_works(mattermost, busy["token"])


def mkchannel(mattermost: Any, team_id: str, name: str, kind: str, display: str = "") -> str:
    resp = mm_request(
        mattermost,
        "POST",
        "/channels",
        json={"team_id": team_id, "name": name, "display_name": display or name, "type": kind},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def is_member(mattermost: Any, kind: str, target: str, user_id: str) -> bool:
    return mm_request(mattermost, "GET", f"/{kind}s/{target}/members/{user_id}").status_code == 200


def test_mint_joins_the_team_and_default_channels(
    mattermost: Any, broker: Broker, setup: dict[str, str], tmp_path: Path
) -> None:
    """#132 (D-109): names resolve at startup, by URL name or display name, and as the probe
    found on oaw-dev, a broker that is a plain `team_user` and a member of each channel adds its
    bot to the team, a public and a private channel. A channel it is not in answers 403, and a
    name nothing has is unresolved: both are reported, and the mint still returns its token."""
    run = secrets.token_hex(3)
    team_display = f"Join Crew {run}"
    resp = mm_request(
        mattermost,
        "POST",
        "/teams",
        json={"name": f"join{run}", "display_name": team_display, "type": "I"},
    )
    assert resp.status_code == 201, resp.text
    team_id = str(resp.json()["id"])
    public = mkchannel(mattermost, team_id, f"pub{run}", "O")
    private = mkchannel(mattermost, team_id, f"priv{run}", "P", f"Crew Private {run}")
    outside = mkchannel(mattermost, team_id, f"out{run}", "O")  # the broker is never added
    broker_id = setup["broker_id"]
    resp = mm_request(
        mattermost,
        "POST",
        f"/teams/{team_id}/members",
        json={"team_id": team_id, "user_id": broker_id},
    )
    assert resp.status_code == 201, resp.text
    for channel in (public, private):
        resp = mm_request(
            mattermost, "POST", f"/channels/{channel}/members", json={"user_id": broker_id}
        )
        assert resp.status_code == 201, resp.text

    def joiner(state: str, *channels: str) -> tuple[Broker, Membership]:
        # As a person writes the file: the team by display name in another case, a channel by
        # URL name, the private one by display name.
        config = replace(broker.config, team=team_display.upper(), default_channels=list(channels))
        built = Broker(config, broker.client, tmp_path / state, clock=broker.clock)
        assert built.membership is not None
        return built, built.membership

    names = (f"pub{run}", f"crew private {run}")
    joining, membership = joiner("a", *names)
    assert [t.id for t in membership.targets()] == [team_id, public, private]
    joined = [team_display.upper(), *names]
    first = joining.mint(args(joining, "joiner"))
    assert first["membership"] == {"joined": joined, "failed": []}
    bot_id = user_by_name(mattermost, first["account"])
    for kind, target in (("team", team_id), ("channel", public), ("channel", private)):
        assert is_member(mattermost, kind, target, bot_id), (kind, target)

    # Again: already a member everywhere, and clean.
    second = joining.mint(args(joining, "joiner"))
    assert second["membership"] == {"joined": joined, "failed": []}

    # A public channel the broker isn't in resolves, and its add is 403. A name nothing has
    # doesn't resolve. Both are reported, the other channel still joins, and the token works.
    # Its own state, so its own bot: "joiner" is the first broker's, and this one would refuse it.
    mixed, _ = joiner("b", f"pub{run}", f"out{run}", f"nowhere{run}")
    third = mixed.mint(args(mixed, "mixer"))
    assert third["membership"]["joined"] == [team_display.upper(), f"pub{run}"]
    refused, unknown = third["membership"]["failed"]
    assert refused["id"] == outside and "403" in refused["error"]
    assert unknown["id"] is None and "no channel has" in unknown["error"]
    assert not is_member(mattermost, "channel", outside, user_by_name(mattermost, third["account"]))
    assert token_works(mattermost, third["token"])


def user_by_name(mattermost: Any, username: str) -> str:
    resp = mm_request(mattermost, "GET", f"/users/username/{username}")
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])
