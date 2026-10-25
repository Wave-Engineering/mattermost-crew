"""IT-18 (Story 3.1): the human partner flips the switch by DMing the broker (R-23, R-24, R-34).

A broker account, a partner and an outsider are created on the CI leg's Mattermost; each
logs in for a session token. `crewbroker.service.Broker` and its `DMListener` run in process
on the broker's token, with an injected clock so social can lapse without waiting. The
partner and the outsider DM the broker over the real server, and the test reads the replies
there. The broker account needs no patched role for this: DMs and its websocket are a plain
user's rights, so IT-01's "only configured brokers hold the role" is untouched.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from crewbroker.config import CrewConfig
from crewbroker.mattermost import MattermostClient
from crewbroker.service import BROKER_PROP, Broker, DMListener
from crewbroker.switch import REFUSAL

from tests.integration.mmhttp import mm_request

REPLY_TIMEOUT_S = 20.0
POLL_S = 0.5


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def mkuser(mattermost: Any, username: str) -> tuple[str, str]:
    """A new user, logged in: (user id, session token)."""
    password = f"It-{secrets.token_hex(8)}-Aa1!"
    resp = mm_request(
        mattermost,
        "POST",
        "/users",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    assert resp.status_code == 201, resp.text
    login = mm_request(
        mattermost,
        "POST",
        "/users/login",
        json={"login_id": username, "password": password},
    )
    assert login.status_code == 200, login.text
    return str(resp.json()["id"]), login.headers["Token"]


@pytest.fixture
def world(mattermost: Any, tmp_path: Path) -> Iterator[dict[str, Any]]:
    run = secrets.token_hex(3)
    broker_id, broker_token = mkuser(mattermost, f"swbroker-{run}")
    partner_id, partner_token = mkuser(mattermost, f"swpartner-{run}")
    outsider_id, outsider_token = mkuser(mattermost, f"swoutsider-{run}")
    clock = Clock()
    client = MattermostClient(mattermost.url, broker_token)
    config = CrewConfig(
        crew=f"sw{run}",
        server_id="ci",
        server_url=mattermost.url,
        human_user_ids=[partner_id],
        trusted=[],
        broker_user_id=broker_id,
    )
    broker = Broker(config, client, tmp_path / "state", clock=clock)
    listener = DMListener(broker, mattermost.url, broker_token)
    stop = threading.Event()
    thread = threading.Thread(target=listener.run, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + REPLY_TIMEOUT_S
    while not listener.connection_id:  # the server's `hello`: the listener is subscribed
        assert time.monotonic() < deadline, "the broker's websocket never said hello"
        time.sleep(0.1)
    try:
        yield {
            "broker": broker,
            "broker_id": broker_id,
            "clock": clock,
            "partner": (partner_id, partner_token),
            "outsider": (outsider_id, outsider_token),
        }
    finally:
        stop.set()
        thread.join(timeout=10)
        client.close()


def dm_channel(mattermost: Any, user: tuple[str, str], broker_id: str) -> str:
    resp = mm_request(
        mattermost, "POST", "/channels/direct", token=user[1], json=[user[0], broker_id]
    )
    assert resp.status_code in (200, 201), resp.text
    return str(resp.json()["id"])


def broker_posts(mattermost: Any, token: str, channel: str, broker_id: str) -> list[dict[str, Any]]:
    resp = mm_request(
        mattermost, "GET", f"/channels/{channel}/posts", token=token, params={"per_page": 100}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    posts = [body["posts"][pid] for pid in body["order"]]
    return sorted(
        (p for p in posts if p["user_id"] == broker_id), key=lambda p: int(p["create_at"])
    )


def wait_for_broker_post(
    mattermost: Any, token: str, channel: str, broker_id: str, count: int
) -> dict[str, Any]:
    """The broker's `count`th post in `channel`, once it is there."""
    deadline = time.monotonic() + REPLY_TIMEOUT_S
    while True:
        posts = broker_posts(mattermost, token, channel, broker_id)
        if len(posts) >= count:
            return posts[count - 1]
        assert time.monotonic() < deadline, f"no broker post #{count} in {channel}"
        time.sleep(POLL_S)


def say(mattermost: Any, user: tuple[str, str], channel: str, text: str) -> None:
    resp = mm_request(
        mattermost, "POST", "/posts", token=user[1], json={"channel_id": channel, "message": text}
    )
    assert resp.status_code == 201, resp.text


def test_it18_partner_dms_flip_the_switch_and_a_lapse_is_announced(
    mattermost: Any, world: dict[str, Any]
) -> None:
    broker: Broker = world["broker"]
    broker_id: str = world["broker_id"]
    clock: Clock = world["clock"]
    partner = world["partner"]
    channel = dm_channel(mattermost, partner, broker_id)
    posts = 0

    def command(text: str) -> dict[str, Any]:
        nonlocal posts
        say(mattermost, partner, channel, text)
        posts += 1
        reply = wait_for_broker_post(mattermost, partner[1], channel, broker_id, posts)
        assert reply["props"].get(BROKER_PROP) in (1, "1", True)
        return reply

    # The defaults: crew only, kiosk on (R-20, R-34).
    state = broker.switch_state({})
    assert (state["mode"], state["kiosk_on"]) == ("crew", True)

    # `social 2h`: the mode and the expiry change (R-23, R-24).
    assert "social until" in command("social 2h")["message"]
    state = broker.switch_state({})
    assert state["mode"] == state["effective_mode"] == "social"
    assert state["revert_to"] == "crew"
    assert datetime.fromisoformat(state["expires_at"]) == clock.now + timedelta(hours=2)

    # `crew` ends social; `kiosk off` turns the kiosk off (R-34).
    assert "crew only" in command("crew")["message"]
    state = broker.switch_state({})
    assert (state["mode"], state["expires_at"]) == ("crew", None)
    assert "kiosk off" in command("kiosk off")["message"]
    assert broker.switch_state({})["kiosk_on"] is False

    # Social again, then past its expiry: it lapses back, and the partner is DMed (R-23).
    command("social 2h")
    clock.now += timedelta(hours=3)
    state = broker.switch_state({})
    assert state["mode"] == state["effective_mode"] == "crew"
    assert broker.switch.load().mode == "crew"
    posts += 1
    notice = wait_for_broker_post(mattermost, partner[1], channel, broker_id, posts)
    assert notice["message"] == "social lapsed → crew"


def test_it18_outsider_dm_is_refused_and_changes_nothing(
    mattermost: Any, world: dict[str, Any]
) -> None:
    broker: Broker = world["broker"]
    broker_id: str = world["broker_id"]
    outsider = world["outsider"]
    before = broker.switch_state({})
    channel = dm_channel(mattermost, outsider, broker_id)
    for n, text in enumerate(("social 2h", "kiosk off"), start=1):
        say(mattermost, outsider, channel, text)
        reply = wait_for_broker_post(mattermost, outsider[1], channel, broker_id, n)
        assert reply["message"] == REFUSAL
    assert broker.switch_state({}) == before
    assert not broker.switch.path.exists()  # nothing was ever written (R-24)
