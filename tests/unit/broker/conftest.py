"""An in-memory Mattermost with the permission model the broker account has (D-100).

The broker sees only the bots it owns: `GET /bots/{id}` is 404 for any other bot, and
`GET /bots` lists only its own active bots, unless `read_others_bots` is set (as if the role
had gained that permission), when it lists every active bot on the server.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from crewbroker import mint
from crewbroker.config import CrewConfig
from crewbroker.mattermost import MattermostError, NotFound
from crewbroker.service import Broker

BROKER_ID = "broker-user-id"
HUMAN_ID = "human-user-id"


class FakeMattermost:
    def __init__(self, *, supports_expiry: bool = True) -> None:
        self.me_id = BROKER_ID
        self.supports_expiry = supports_expiry
        self.read_others_bots = False
        self.list_bots_fails = False
        self.revoke_fails: set[str] = set()
        self.users: dict[str, dict[str, Any]] = {
            BROKER_ID: {"id": BROKER_ID, "username": "crewbroker", "is_bot": False, "delete_at": 0},
            HUMAN_ID: {"id": HUMAN_ID, "username": "bj", "is_bot": False, "delete_at": 0},
        }
        self.bots: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self._ids = itertools.count(1)

    # -- helpers for tests ------------------------------------------------------------------
    def add_user(self, username: str, *, is_bot: bool = False, owner_id: str = "") -> str:
        uid = f"u{next(self._ids)}"
        self.users[uid] = {"id": uid, "username": username, "is_bot": is_bot, "delete_at": 0}
        if is_bot:
            self.bots[uid] = {"user_id": uid, "owner_id": owner_id, "delete_at": 0}
        return uid

    def reassign(self, bot_id: str, owner_id: str) -> None:
        self.bots[bot_id]["owner_id"] = owner_id

    def token_works(self, token: str) -> bool:
        for rec in self.tokens.values():
            if rec["token"] == token:
                user = self.users[rec["user_id"]]
                return not rec["revoked"] and not user["delete_at"]
        return False

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)

    def _visible(self, bot_id: str) -> dict[str, Any]:
        bot = self.bots.get(bot_id)
        if bot is None or (bot["owner_id"] != self.me_id and not self.read_others_bots):
            raise NotFound("GET", f"/bots/{bot_id}", 404, "not found")
        return bot

    # -- MattermostAPI ----------------------------------------------------------------------
    def me(self) -> dict[str, Any]:
        self.calls.append(("me", None))
        return dict(self.users[self.me_id])

    def get_user(self, user_id: str) -> dict[str, Any]:
        self.calls.append(("get_user", user_id))
        if user_id not in self.users:
            raise NotFound("GET", f"/users/{user_id}", 404, "not found")
        return dict(self.users[user_id])

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        self.calls.append(("get_user_by_username", username))
        for user in self.users.values():
            if user["username"] == username:
                return dict(user)
        return None

    def get_bot(self, bot_user_id: str) -> dict[str, Any]:
        self.calls.append(("get_bot", bot_user_id))
        return dict(self._visible(bot_user_id))

    def list_bots(self) -> list[dict[str, Any]]:
        self.calls.append(("list_bots", None))
        if self.list_bots_fails:
            raise MattermostError("GET", "/bots", 500, "boom")
        return [
            dict(b)
            for b in self.bots.values()
            if not b["delete_at"] and (b["owner_id"] == self.me_id or self.read_others_bots)
        ]

    def create_bot(self, username: str, display_name: str, description: str) -> dict[str, Any]:
        self.calls.append(("create_bot", username))
        if self.get_user_by_username(username) is not None:
            raise MattermostError("POST", "/bots", 400, "username taken")
        uid = self.add_user(username, is_bot=True, owner_id=self.me_id)
        return dict(self.bots[uid])

    def enable_bot(self, bot_user_id: str) -> None:
        self.calls.append(("enable_bot", bot_user_id))
        self._visible(bot_user_id)["delete_at"] = 0
        self.users[bot_user_id]["delete_at"] = 0

    def disable_bot(self, bot_user_id: str) -> None:
        self.calls.append(("disable_bot", bot_user_id))
        self._visible(bot_user_id)["delete_at"] = 1
        self.users[bot_user_id]["delete_at"] = 1

    def create_token(
        self, user_id: str, description: str, expires_at_ms: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(("create_token", user_id))
        tid = f"t{next(self._ids)}"
        rec: dict[str, Any] = {
            "id": tid,
            "token": f"secret-{tid}",
            "user_id": user_id,
            "revoked": False,
        }
        if self.supports_expiry and expires_at_ms is not None:
            rec["expires_at"] = expires_at_ms
        self.tokens[tid] = rec
        return {k: v for k, v in rec.items() if k != "revoked"}

    def revoke_token(self, token_id: str) -> None:
        self.calls.append(("revoke_token", token_id))
        if token_id in self.revoke_fails:
            raise MattermostError("POST", "/users/tokens/revoke", 500, "boom")
        if token_id not in self.tokens:
            raise NotFound("POST", "/users/tokens/revoke", 404, "not found")
        self.tokens[token_id]["revoked"] = True


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def _fresh_expiry_cache() -> Iterator[None]:
    mint.clear_expiry_cache()
    yield
    mint.clear_expiry_cache()


@pytest.fixture
def fake() -> FakeMattermost:
    return FakeMattermost()


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def crew_config() -> CrewConfig:
    return CrewConfig(
        crew="oaw",
        server_id="dev",
        server_url="http://mm.invalid",
        human_user_ids=[HUMAN_ID],
        trusted=["otter"],
        broker_user_id=BROKER_ID,
    )


@pytest.fixture
def broker(fake: FakeMattermost, crew_config: CrewConfig, tmp_path: Path, clock: Clock) -> Broker:
    return Broker(crew_config, fake, tmp_path / "state", clock=clock)
