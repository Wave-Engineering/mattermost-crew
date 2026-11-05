"""#231 against the CI leg's Mattermost: the broker's pinned agent legend.

The module makes a team with an `oaw` channel, puts its own broker account (with the patched
role) in both, and drives `crewbroker.service.Broker` in process. Mints only record an agent's
legend fields; each reconcile pass brings the one pinned post up to date.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from crewbroker import mint
from crewbroker.config import CrewConfig
from crewbroker.legend import MARKER_PROP, MARKER_VERSION
from crewbroker.mattermost import MattermostClient
from crewbroker.service import Broker

from tests.integration.mmhttp import mm_request
from tests.integration.test_legend_permissions import join, mkuser, token_for

ROOT = Path(__file__).resolve().parents[2]
ROLE_PATCH = ROOT / "scripts" / "ci" / "broker-role-patch.sh"


@pytest.fixture(scope="module")
def setup(mattermost: Any) -> Iterator[dict[str, str]]:
    run = secrets.token_hex(3)
    resp = mm_request(
        mattermost,
        "POST",
        "/teams",
        json={"name": f"legend{run}", "display_name": f"Legend {run}", "type": "I"},
    )
    assert resp.status_code == 201, resp.text
    team = str(resp.json()["name"])
    team_id = str(resp.json()["id"])
    resp = mm_request(
        mattermost,
        "POST",
        "/channels",
        json={"team_id": team_id, "name": "oaw", "display_name": "oaw", "type": "O"},
    )
    assert resp.status_code == 201, resp.text
    channel_id = str(resp.json()["id"])

    broker_id = mkuser(mattermost, f"crewbroker-lg-{run}")
    partner_id = mkuser(mattermost, f"partner-lg-{run}")
    env = {**os.environ, "MM_URL": mattermost.url, "MM_ADMIN_TOKEN": mattermost.admin_token}
    subprocess.run([str(ROLE_PATCH), broker_id], env=env, check=True)
    join(mattermost, team_id, channel_id, broker_id)
    yield {
        "crew": f"lg{run}",
        "team": team,
        "channel_id": channel_id,
        "broker_id": broker_id,
        "partner_id": partner_id,
        "broker_token": token_for(mattermost, broker_id),
    }
    # Not in servers.toml, so the account gives the patched role back: IT-01 asserts that only
    # the configured brokers hold it (R-06).
    resp = mm_request(mattermost, "PUT", f"/users/{broker_id}/roles", json={"roles": "system_user"})
    assert resp.status_code == 200, resp.text


def build(mattermost: Any, setup: dict[str, str], state: Path) -> Broker:
    mint.clear_expiry_cache()
    config = CrewConfig(
        crew=setup["crew"],
        server_id="ci",
        server_url=mattermost.url,
        human_user_ids=[setup["partner_id"]],
        trusted=[],
        broker_user_id=setup["broker_id"],
        team=setup["team"],
        default_channels=["oaw"],
    )
    return Broker(config, MattermostClient(mattermost.url, setup["broker_token"]), state)


def legend_args(broker: Broker, dev: str, project_dir: str, in_scope: bool) -> dict[str, Any]:
    return {
        "dev_name": dev,
        "crew": broker.config.crew,
        "project_dir": project_dir,
        "in_scope": in_scope,
    }


def pinned(mattermost: Any, channel_id: str) -> list[dict[str, Any]]:
    resp = mm_request(mattermost, "GET", f"/channels/{channel_id}/pinned")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return [body["posts"][i] for i in body["order"]]


def test_legend_is_one_pinned_post_edited_in_place(
    mattermost: Any, setup: dict[str, str], tmp_path: Path
) -> None:
    broker = build(mattermost, setup, tmp_path / "state")
    crew = setup["crew"]
    broker.mint(legend_args(broker, "alpha", "crew-231", True))
    broker.mint(legend_args(broker, "bravo", "site-tools", False))  # out of scope: name only
    assert broker.reconcile_membership() is not None

    (post,) = pinned(mattermost, setup["channel_id"])
    post_id = str(post["id"])
    assert post["user_id"] == setup["broker_id"]
    assert post["props"][MARKER_PROP] == MARKER_VERSION
    assert f"| `alpha.{crew}` | `crew-231` |" in post["message"]
    assert f"| `bravo.{crew}` |  |" in post["message"]
    assert "site-tools" not in post["message"]

    # A burst of mints between passes is one edit of the same post, and an unchanged pass
    # writes nothing.
    broker.mint(legend_args(broker, "charlie", "notes", True))
    broker.mint(legend_args(broker, "charlie", "notes-2", True))
    broker.mint(legend_args(broker, "delta", "", True))
    assert broker.sync_legend() == "edited"
    assert broker.sync_legend() == "unchanged"
    (post,) = pinned(mattermost, setup["channel_id"])
    assert str(post["id"]) == post_id
    assert int(post["edit_at"]) > 0
    assert f"| `charlie.{crew}` | `notes-2` |" in post["message"]
    assert f"| `delta.{crew}` |  |" in post["message"]

    # A restart keeps the post. One that someone unpinned is pinned again, not reposted.
    resp = mm_request(mattermost, "POST", f"/posts/{post_id}/unpin")
    assert resp.status_code == 200, resp.text
    restarted = build(mattermost, setup, tmp_path / "state")
    assert restarted.sync_legend() == "pinned"
    (post,) = pinned(mattermost, setup["channel_id"])
    assert str(post["id"]) == post_id

    # A deleted post is replaced by a new pinned one.
    resp = mm_request(mattermost, "DELETE", f"/posts/{post_id}")
    assert resp.status_code == 200, resp.text
    again = build(mattermost, setup, tmp_path / "state")
    assert again.sync_legend() == "created"
    (post,) = pinned(mattermost, setup["channel_id"])
    assert str(post["id"]) != post_id
    assert f"| `alpha.{crew}` | `crew-231` |" in post["message"]
