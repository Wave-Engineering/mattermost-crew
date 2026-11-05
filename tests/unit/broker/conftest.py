"""An in-memory Mattermost with the permission model the broker account has (D-100).

The broker sees only the bots it owns: `GET /bots/{id}` is 404 for any other bot, and
`GET /bots` lists only its own active bots, unless `read_others_bots` is set (as if the role
had gained that permission), when it lists every active bot on the server.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
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
        self.disable_fails: set[str] = set()
        self.users: dict[str, dict[str, Any]] = {
            BROKER_ID: {"id": BROKER_ID, "username": "crewbroker", "is_bot": False, "delete_at": 0},
            HUMAN_ID: {"id": HUMAN_ID, "username": "bj", "is_bot": False, "delete_at": 0},
        }
        self.bots: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        # DM history per channel for the backfill (#159): each post as the REST API gives it.
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.post_fails = False
        # The agent legend (#231): posts by id, which are pinned or deleted, and a patch's
        # forced error status (403 as under a post-edit time limit, 429 as a rate limit).
        self.by_id: dict[str, dict[str, Any]] = {}
        self.pinned: set[str] = set()
        self.deleted: set[str] = set()
        self.patch_status: int | None = None
        self.pin_fails = False
        # Membership (#132): target (team or channel) id -> member user ids.
        self.members: dict[str, list[str]] = {}
        self.add_fails: set[str] = set()  # targets whose add answers 403
        self.exists_as_400 = False  # an existing member answered 400 `…exists`, not 201
        # Teams and channels by id, and which of them the broker account belongs to.
        self.teams: dict[str, dict[str, Any]] = {}
        self.channels: dict[str, dict[str, Any]] = {}
        self.my_ids: set[str] = set()
        self.lookup_down = False  # every name lookup answers `lookup_status`
        self.lookup_status = 503  # as if the server were down; 429 is a rate limit
        self.on_add: Callable[[str], None] | None = None  # called with each add's target
        # As measured for #132: a `team_user` broker adds its bots only to a channel it is in,
        # and joins a public channel itself. Off by default, as the #132 tests predate it.
        self.channel_needs_broker = False
        self.images: dict[str, bytes] = {}  # user id -> the PNG `set_user_image` sent (#151)
        self.image_fails = False
        self._ids = itertools.count(1)

    # -- helpers for tests ------------------------------------------------------------------
    def add_user(
        self, username: str, *, is_bot: bool = False, owner_id: str = "", description: str = ""
    ) -> str:
        uid = f"u{next(self._ids)}"
        self.users[uid] = {"id": uid, "username": username, "is_bot": is_bot, "delete_at": 0}
        if is_bot:
            self.bots[uid] = {
                "user_id": uid,
                "owner_id": owner_id,
                "delete_at": 0,
                "description": description,
            }
        return uid

    def add_team(self, tid: str, name: str, display: str, *, mine: bool = True) -> str:
        self.teams[tid] = {"id": tid, "name": name, "display_name": display}
        if mine:
            self.my_ids.add(tid)
        return tid

    def add_channel(
        self,
        cid: str,
        team_id: str,
        name: str,
        display: str,
        kind: str = "O",
        *,
        mine: bool = True,
        header: str = "",
        purpose: str = "",
    ) -> str:
        self.channels[cid] = {
            "id": cid,
            "team_id": team_id,
            "name": name,
            "display_name": display,
            "type": kind,
            "delete_at": 0,
            "header": header,
            "purpose": purpose,
        }
        if mine:
            self.my_ids.add(cid)
        return cid

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
        uid = self.add_user(username, is_bot=True, owner_id=self.me_id, description=description)
        return dict(self.bots[uid])

    def set_user_image(self, user_id: str, png: bytes) -> None:
        self.calls.append(("set_user_image", user_id))
        if self.image_fails:
            raise MattermostError("POST", f"/users/{user_id}/image", 500, "boom")
        self.images[user_id] = png
        self.users[user_id]["last_picture_update"] = 1

    def enable_bot(self, bot_user_id: str) -> None:
        self.calls.append(("enable_bot", bot_user_id))
        self._visible(bot_user_id)["delete_at"] = 0
        self.users[bot_user_id]["delete_at"] = 0

    def disable_bot(self, bot_user_id: str) -> None:
        self.calls.append(("disable_bot", bot_user_id))
        if bot_user_id in self.disable_fails:
            raise MattermostError("PUT", f"/bots/{bot_user_id}/disable", 500, "boom")
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

    def direct_channel(self, user_id: str, other_id: str) -> str:
        self.calls.append(("direct_channel", (user_id, other_id)))
        return "__".join(sorted([user_id, other_id]))

    def posts_since(self, channel_id: str, since_ms: int) -> list[dict[str, Any]]:
        """As the server's `since`: posts created or changed after it, edits included."""
        self.calls.append(("posts_since", (channel_id, since_ms)))
        found = [
            p
            for p in self.history.get(channel_id, [])
            if max(p.get("create_at", 0), p.get("update_at", 0)) > since_ms
        ]
        return sorted(found, key=lambda p: p.get("create_at", 0))

    def create_post(
        self, channel_id: str, message: str, props: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append(("create_post", channel_id))
        if self.post_fails:
            raise MattermostError("POST", "/posts", 500, "boom")
        post = {"channel_id": channel_id, "message": message, "props": dict(props or {})}
        self.posts.append(post)
        # By id for the legend (#231): the same dict, so an edit shows in `posts` too.
        pid = f"p{next(self._ids)}"
        self.by_id[pid] = post
        return {**post, "id": pid}

    def get_post(self, post_id: str) -> dict[str, Any]:
        self.calls.append(("get_post", post_id))
        post = self.by_id.get(post_id)
        if post is None or post_id in self.deleted:
            raise NotFound("GET", f"/posts/{post_id}", 404, "not found")
        return {**post, "id": post_id, "is_pinned": post_id in self.pinned}

    def patch_post(self, post_id: str, message: str) -> dict[str, Any]:
        self.calls.append(("patch_post", post_id))
        if self.patch_status is not None:
            raise MattermostError("PUT", f"/posts/{post_id}/patch", self.patch_status, "refused")
        post = self.get_post(post_id)
        self.calls.pop()  # the patch's own lookup isn't a call the broker made
        self.by_id[post_id]["message"] = message
        return {**post, "message": message}

    def pin_post(self, post_id: str) -> None:
        self.calls.append(("pin_post", post_id))
        if self.pin_fails:
            raise MattermostError("POST", f"/posts/{post_id}/pin", 500, "boom")
        self.get_post(post_id)
        self.calls.pop()
        self.pinned.add(post_id)

    def _add_member(self, call: str, kind: str, target: str, user_id: str) -> None:
        self.calls.append((call, (target, user_id)))
        if self.on_add is not None:
            self.on_add(target)
        path = f"/{kind}s/{target}/members"
        denied = MattermostError("POST", path, 403, "no permission", "api.context.permissions")
        if target in self.add_fails:
            raise denied
        if kind == "channel" and self.channel_needs_broker and target not in self.my_ids:
            if user_id != self.me_id or self.channels[target]["type"] != "O":
                raise denied
            self.my_ids.add(target)  # the broker joins a public channel itself
        members = self.members.setdefault(target, [])
        if user_id in members:
            if self.exists_as_400:
                exists = f"store.sql_{kind}.save_member.exists.app_error"
                raise MattermostError("POST", path, 400, "already a member", exists)
            return  # 201, as the #132 probe saw
        members.append(user_id)

    def _lookup(self, call: str, arg: Any, path: str) -> None:
        self.calls.append((call, arg))
        if self.lookup_down:
            raise MattermostError("GET", path, self.lookup_status, "unavailable")

    def get_team_by_name(self, name: str) -> dict[str, Any]:
        path = f"/teams/name/{name}"
        self._lookup("get_team_by_name", name, path)
        for team in self.teams.values():
            if team["name"] == name:
                return dict(team)
        raise NotFound("GET", path, 404, "not found", "app.team.get_by_name.missing.app_error")

    def get_channel_by_name(self, team_id: str, name: str) -> dict[str, Any]:
        path = f"/teams/{team_id}/channels/name/{name}"
        self._lookup("get_channel_by_name", (team_id, name), path)
        for ch in self.channels.values():
            # A private channel is visible only to a member, as on the server.
            visible = ch["type"] == "O" or ch["id"] in self.my_ids
            if ch["team_id"] == team_id and ch["name"] == name and visible:
                return dict(ch)
        raise NotFound("GET", path, 404, "not found", "app.channel.get_by_name.missing")

    def list_my_teams(self) -> list[dict[str, Any]]:
        self._lookup("list_my_teams", None, "/users/me/teams")
        return [dict(t) for tid, t in self.teams.items() if tid in self.my_ids]

    def list_team_channels(self, team_id: str) -> list[dict[str, Any]]:
        self._lookup("list_team_channels", team_id, f"/teams/{team_id}/channels")
        return [
            dict(c) for c in self.channels.values() if c["team_id"] == team_id and c["type"] == "O"
        ]

    def list_my_team_channels(self, team_id: str) -> list[dict[str, Any]]:
        self._lookup("list_my_team_channels", team_id, f"/users/me/teams/{team_id}/channels")
        return [
            dict(c)
            for cid, c in self.channels.items()
            if c["team_id"] == team_id and cid in self.my_ids
        ]

    def add_team_member(self, team_id: str, user_id: str) -> None:
        self._add_member("add_team_member", "team", team_id, user_id)

    def add_channel_member(self, channel_id: str, user_id: str) -> None:
        self._add_member("add_channel_member", "channel", channel_id, user_id)


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
    # One fake time for both clocks, so a test's deadlines are on the clock it moves.
    return Broker(
        crew_config, fake, tmp_path / "state", clock=clock, epoch=lambda: clock.now.timestamp()
    )
