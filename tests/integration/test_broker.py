"""Story 1.3 against the CI leg's Mattermost: the broker's accounts, tokens and prune.

Each module run creates its own broker account and human partner, gives the broker the
patched role with `scripts/ci/broker-role-patch.sh`, and drives `crewbroker.service.Broker`
in process, with an injected clock where time matters (IT-03).
"""

from __future__ import annotations

import os
import secrets
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from crewbroker import mint
from crewbroker.config import CrewConfig
from crewbroker.mattermost import MattermostClient
from crewbroker.service import Broker, OpError

ROOT = Path(__file__).resolve().parents[2]
ROLE_PATCH = ROOT / "scripts" / "ci" / "broker-role-patch.sh"
PATCHED = {"create_bot", "read_bots", "manage_bots"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def admin(mattermost: Any, method: str, path: str, **kw: Any) -> httpx.Response:
    return httpx.request(
        method, f"{mattermost.api}{path}", headers=mattermost.auth_headers, timeout=15, **kw
    )


def as_token(mattermost: Any, token: str, method: str, path: str, **kw: Any) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    return httpx.request(method, f"{mattermost.api}{path}", headers=headers, timeout=15, **kw)


def mkuser(mattermost: Any, username: str) -> str:
    resp = admin(
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
def setup(mattermost: Any) -> dict[str, str]:
    run = secrets.token_hex(3)
    crew = f"it{run}"
    broker_id = mkuser(mattermost, f"crewbroker-{run}")
    partner_id = mkuser(mattermost, f"partner-{run}")
    env = {**os.environ, "MM_URL": mattermost.url, "MM_ADMIN_TOKEN": mattermost.admin_token}
    subprocess.run([str(ROLE_PATCH), broker_id], env=env, check=True)
    resp = admin(mattermost, "POST", f"/users/{broker_id}/tokens", json={"description": "it"})
    assert resp.status_code == 200, resp.text
    return {
        "crew": crew,
        "broker_id": broker_id,
        "partner_id": partner_id,
        "broker_token": resp.json()["token"],
    }


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
    resp = admin(mattermost, "GET", f"/users/{user_id}")
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()
    return data


def token_works(mattermost: Any, token: str) -> bool:
    return as_token(mattermost, token, "GET", "/users/me").status_code == 200


def test_broker_role_is_patched_and_not_admin(mattermost: Any, setup: dict[str, str]) -> None:
    """R-06: the broker account holds the patched role and no system-admin role."""
    me = as_token(mattermost, setup["broker_token"], "GET", "/users/me").json()
    roles = set(me["roles"].split())
    assert roles == {"system_user", "system_user_access_token"}
    assert "system_admin" not in roles
    role = admin(mattermost, "GET", "/roles/name/system_user_access_token").json()
    assert set(role["permissions"]) >= PATCHED


def test_ensure_bot_creates_once(
    mattermost: Any, broker: Broker, setup: dict[str, str]
) -> None:
    """R-01, R-03: `<dev>.<crew>` is created on the first call only."""
    first = broker.mint(args(broker, "once"))
    second = broker.mint(args(broker, "once"))
    name = f"once.{broker.config.crew}"
    assert first["account"] == second["account"] == name
    found = admin(mattermost, "GET", f"/users/username/{name}").json()
    assert found["is_bot"] is True
    assert broker.accounts.minted_bot_ids() == [found["id"]]
    bot = as_token(mattermost, setup["broker_token"], "GET", f"/bots/{found['id']}").json()
    assert bot["owner_id"] == setup["broker_id"]


def test_mint_token_works_with_native_expiry_where_supported(
    mattermost: Any, broker: Broker, setup: dict[str, str]
) -> None:
    """R-05, R-07: a working token; native expiry is set exactly when the server has it."""
    minted = broker.mint(args(broker, "expiry"))
    me = as_token(mattermost, minted["token"], "GET", "/users/me").json()
    assert me["username"] == minted["account"]
    meta = as_token(
        mattermost, setup["broker_token"], "GET", f"/users/tokens/{minted['token_id']}"
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

    resp = as_token(
        mattermost, setup["broker_token"], "POST", f"/bots/{moved_id}/assign/{setup['partner_id']}"
    )
    assert resp.status_code == 200, resp.text
    assert broker.crew_info({})["minted_bot_ids"] == [keep_id]
    # And the squat check holds: the reassigned name can't be minted for.
    with pytest.raises(OpError, match="squatted"):
        broker.mint(args(broker, "moved"))

    resp = admin(mattermost, "POST", f"/bots/{moved_id}/assign/{setup['broker_id']}")
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


def test_mint_refuses_squatted_name(mattermost: Any, broker: Broker) -> None:
    """R-01, R-03: an existing account not owned by this broker → loud failure, no token."""
    crew = broker.config.crew
    mkuser(mattermost, f"human.{crew}")
    resp = admin(
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
