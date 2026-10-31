"""IT-19 against the CI leg's Mattermost (Story 3.8; R-01): bot account avatars and `@`
autocomplete.

Two agents register through the daemon's registry over the module's in-process broker
(`test_mattermost_adapter.Daemon`), so their bot accounts are the broker's own. The broker sets
each bot's avatar at its first `mint`, a deterministic identicon of the account name (#151): a
bot can't set its own (403, spike 1.2), so its owner, the broker account, does (D-098). An
ordinary user then sees each bot's own avatar at `GET /users/{id}/image`, and finds each bot
account in `GET /users/autocomplete?name=<dev>`.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from tests.integration.mmhttp import mm_request
from tests.integration.test_filters import login_user
from tests.integration.test_mattermost_adapter import Daemon, setup

__all__ = ["setup"]  # the module's Mattermost team, broker and channel (a fixture)

DEVS = ("avia", "avib")


def image(mattermost: Any, token: str, user_id: str) -> bytes:
    resp = mm_request(mattermost, "GET", f"/users/{user_id}/image", token=token)
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("Content-Type", "").startswith("image/"), resp.headers
    return resp.content


def avatar_hash(mattermost: Any, token: str, user_id: str) -> bytes:
    """What `GET /users/{id}/image` serves, as `token` sees it, hashed."""
    return hashlib.sha256(image(mattermost, token, user_id)).digest()


def test_it19_bot_avatars_and_autocomplete(
    mattermost: Any, setup: dict[str, str], tmp_path: Path
) -> None:
    d = Daemon(mattermost, setup, tmp_path)

    async def register() -> dict[str, str]:
        return {dev: await d.register(dev) for dev in DEVS}

    ids = asyncio.run(register())
    crew = setup["crew"]
    observer_id, observer = login_user(mattermost, f"observer-{crew}")
    resp = mm_request(
        mattermost,
        "POST",
        f"/teams/{setup['team_id']}/members",
        json={"team_id": setup["team_id"], "user_id": observer_id},
    )
    assert resp.status_code == 201, resp.text

    # The broker set each avatar at mint; the test sets none.
    for dev, bot_id in ids.items():
        resp = mm_request(mattermost, "GET", f"/users/{bot_id}", token=observer)
        assert resp.status_code == 200, resp.text
        assert resp.json().get("last_picture_update", 0) > 0, f"{dev}'s avatar was never set"
    seen = {dev: avatar_hash(mattermost, observer, i) for dev, i in ids.items()}
    assert seen["avia"] != seen["avib"], "the two bots share one avatar"

    for dev, bot_id in ids.items():
        resp = mm_request(
            mattermost,
            "GET",
            "/users/autocomplete",
            token=observer,
            params={"in_team": setup["team_id"], "name": dev},
        )
        assert resp.status_code == 200, resp.text
        users = {u["id"]: u for u in resp.json().get("users", [])}
        assert bot_id in users, f"{dev}.{crew} is not in @ autocomplete"
        assert users[bot_id]["username"] == f"{dev}.{crew}"  # R-01
        assert users[bot_id]["is_bot"] is True
        assert users[bot_id].get("last_picture_update"), f"{dev}'s avatar update is not shown"
