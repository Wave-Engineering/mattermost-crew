"""#231's spike against the CI leg's Mattermost: may the broker post, pin and edit the legend?

The broker owns the agent legend, and it posts as its broker account: a plain `system_user`
with the patched role (R-06) that is a member of the team and the channel. That account, and for
comparison a bot account with no role beyond a bot's, each create a post, pin it, edit it in
place, and delete it. What a deleted post then answers is what `Legend.sync` keys its
re-create on.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration.mmhttp import mm_request

ROOT = Path(__file__).resolve().parents[2]
ROLE_PATCH = ROOT / "scripts" / "ci" / "broker-role-patch.sh"


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


def token_for(mattermost: Any, user_id: str) -> str:
    resp = mm_request(mattermost, "POST", f"/users/{user_id}/tokens", json={"description": "it"})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def join(mattermost: Any, team_id: str, channel_id: str, user_id: str) -> None:
    resp = mm_request(
        mattermost,
        "POST",
        f"/teams/{team_id}/members",
        json={"team_id": team_id, "user_id": user_id},
    )
    assert resp.status_code == 201, resp.text
    resp = mm_request(
        mattermost, "POST", f"/channels/{channel_id}/members", json={"user_id": user_id}
    )
    assert resp.status_code == 201, resp.text


@pytest.fixture(scope="module")
def place(mattermost: Any) -> Iterator[dict[str, str]]:
    """A team and a public channel, with a broker account and a bot account in both."""
    run = secrets.token_hex(3)
    resp = mm_request(
        mattermost,
        "POST",
        "/teams",
        json={"name": f"legend{run}", "display_name": f"Legend {run}", "type": "I"},
    )
    assert resp.status_code == 201, resp.text
    team_id = str(resp.json()["id"])
    resp = mm_request(
        mattermost,
        "POST",
        "/channels",
        json={"team_id": team_id, "name": f"oaw{run}", "display_name": f"oaw {run}", "type": "O"},
    )
    assert resp.status_code == 201, resp.text
    channel_id = str(resp.json()["id"])

    broker_id = mkuser(mattermost, f"crewbroker-legend-{run}")
    env = {**os.environ, "MM_URL": mattermost.url, "MM_ADMIN_TOKEN": mattermost.admin_token}
    subprocess.run([str(ROLE_PATCH), broker_id], env=env, check=True)
    join(mattermost, team_id, channel_id, broker_id)

    resp = mm_request(
        mattermost,
        "POST",
        "/bots",
        json={"username": f"legend-bot-{run}", "display_name": f"legend bot {run}"},
    )
    assert resp.status_code == 201, resp.text
    bot_id = str(resp.json()["user_id"])
    join(mattermost, team_id, channel_id, bot_id)

    yield {
        "channel_id": channel_id,
        "broker": token_for(mattermost, broker_id),
        "bot": token_for(mattermost, bot_id),
    }
    # Not in servers.toml, so the account gives the patched role back: IT-01 asserts that only
    # the configured brokers hold it (R-06).
    resp = mm_request(mattermost, "PUT", f"/users/{broker_id}/roles", json={"roles": "system_user"})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("who", ["broker", "bot"])
def test_poster_can_create_pin_edit_and_delete(
    mattermost: Any, place: dict[str, str], who: str
) -> None:
    token, channel_id = place[who], place["channel_id"]

    def call(method: str, path: str, **kw: Any) -> Any:
        return mm_request(mattermost, method, path, token=token, **kw)

    resp = call("POST", "/posts", json={"channel_id": channel_id, "message": "legend v1"})
    assert resp.status_code == 201, f"create: {resp.status_code} {resp.text}"
    post_id = str(resp.json()["id"])

    resp = call("POST", f"/posts/{post_id}/pin")
    assert resp.status_code == 200, f"pin: {resp.status_code} {resp.text}"
    assert call("GET", f"/posts/{post_id}").json()["is_pinned"] is True
    pinned = call("GET", f"/channels/{channel_id}/pinned").json()
    assert post_id in pinned["order"], "the pinned list lacks the post"

    resp = call("PUT", f"/posts/{post_id}/patch", json={"message": "legend v2"})
    assert resp.status_code == 200, f"patch: {resp.status_code} {resp.text}"
    edited = call("GET", f"/posts/{post_id}").json()
    assert edited["message"] == "legend v2"
    assert edited["is_pinned"] is True, "an edit unpinned the post"
    assert edited["edit_at"] > 0

    resp = call("DELETE", f"/posts/{post_id}")
    assert resp.status_code == 200, f"delete: {resp.status_code} {resp.text}"
    # What a lost post answers, to the poster: the legend re-creates on this.
    gone = call("GET", f"/posts/{post_id}")
    assert gone.status_code == 404, f"get after delete: {gone.status_code} {gone.text}"
    patch = call("PUT", f"/posts/{post_id}/patch", json={"message": "legend v3"})
    assert patch.status_code in (403, 404), f"patch after delete: {patch.status_code} {patch.text}"
