"""The broker's Mattermost REST client: only the calls the broker makes, as the broker account.

The broker account holds `system_user_access_token` patched with `create_bot`, `read_bots`
and `manage_bots`, and nothing more (R-06). So it can read and manage the bots it owns, and
no others: `GET /bots/{id}` is 404 for a bot it does not own (D-100).

A caller with a deadline sets it with `call_deadline` (#158). Inside it, `_call` caps each
request's timeout at the time left, and raises `DeadlineExceeded` rather than send a request
or wait out a 429 past it. `uncapped` keeps the refusals but not the cap, for a request whose
reply must not be dropped once it is sent. The deadline is per thread, so the prune and
DM-listener threads, which set none, keep the plain timeout and retries.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

import httpx

DEFAULT_TIMEOUT_S = 10.0
BOTS_PAGE_SIZE = 200
CHANNELS_PAGE_SIZE = 200
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_MAX_WAIT_S = 5.0


class MattermostError(Exception):
    """A Mattermost API call failed."""

    def __init__(
        self, method: str, path: str, status: int | None, detail: str, error_id: str = ""
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.error_id = error_id  # Mattermost's error `id`, such as `api.context.permissions…`
        code = status if status is not None else "error"
        super().__init__(f"{method} {path} -> {code}: {detail}")


class NotFound(MattermostError):
    """The server answered 404."""


class DeadlineExceeded(MattermostError):
    """The caller's `call_deadline` passed, or a 429's wait would pass it."""


_local = threading.local()


@contextmanager
def call_deadline(deadline: float | None) -> Iterator[None]:
    """Bound this thread's calls by `deadline` (epoch seconds; None for no bound)."""
    previous = getattr(_local, "deadline", None)
    _local.deadline = deadline
    try:
        yield
    finally:
        _local.deadline = previous


@contextmanager
def uncapped() -> Iterator[None]:
    """Inside `call_deadline`, a request once sent runs to the client's own timeout.

    For a call whose reply must not be dropped: the one creating a token, whose secret is in
    the reply only. A capped read timeout would leave a live token nothing records, so no
    supersede could revoke it (#41, #158). The deadline still refuses to send, or to wait out
    a 429, past it.
    """
    previous = getattr(_local, "uncapped", False)
    _local.uncapped = True
    try:
        yield
    finally:
        _local.uncapped = previous


class MattermostAPI(Protocol):
    """What the broker needs from a server. `MattermostClient` is the real one."""

    def me(self) -> dict[str, Any]: ...

    def get_user(self, user_id: str) -> dict[str, Any]: ...

    def get_user_by_username(self, username: str) -> dict[str, Any] | None: ...

    def get_bot(self, bot_user_id: str) -> dict[str, Any]: ...

    def list_bots(self) -> list[dict[str, Any]]: ...

    def create_bot(self, username: str, display_name: str, description: str) -> dict[str, Any]: ...

    def set_user_image(self, user_id: str, png: bytes) -> None: ...

    def enable_bot(self, bot_user_id: str) -> None: ...

    def disable_bot(self, bot_user_id: str) -> None: ...

    def create_token(
        self, user_id: str, description: str, expires_at_ms: int | None = None
    ) -> dict[str, Any]: ...

    def revoke_token(self, token_id: str) -> None: ...

    def direct_channel(self, user_id: str, other_id: str) -> str: ...

    def posts_since(self, channel_id: str, since_ms: int) -> list[dict[str, Any]]: ...

    def create_post(
        self, channel_id: str, message: str, props: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_post(self, post_id: str) -> dict[str, Any]: ...

    def patch_post(self, post_id: str, message: str) -> dict[str, Any]: ...

    def pin_post(self, post_id: str) -> None: ...

    def get_team_by_name(self, name: str) -> dict[str, Any]: ...

    def get_channel_by_name(self, team_id: str, name: str) -> dict[str, Any]: ...

    def list_my_teams(self) -> list[dict[str, Any]]: ...

    def list_team_channels(self, team_id: str) -> list[dict[str, Any]]: ...

    def list_my_team_channels(self, team_id: str) -> list[dict[str, Any]]: ...

    def add_team_member(self, team_id: str, user_id: str) -> None: ...

    def add_channel_member(self, channel_id: str, user_id: str) -> None: ...


class MattermostClient:
    """`MattermostAPI` over httpx, authenticated with the broker account's access token."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.Client(
            base_url=f"{self.url}/api/v4",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
    ) -> Any:
        deadline: float | None = getattr(_local, "deadline", None)
        capped = deadline is not None and not getattr(_local, "uncapped", False)
        attempt = 0
        while True:
            timeout = self.timeout
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise DeadlineExceeded(method, path, None, "the caller's deadline passed")
                if capped:
                    timeout = min(timeout, remaining)
            try:
                resp = self._http.request(
                    method, path, json=json, params=params, files=files, timeout=timeout
                )
            except httpx.TimeoutException as exc:
                if capped and deadline is not None and self._clock() >= deadline:
                    raise DeadlineExceeded(method, path, None, f"timed out: {exc}") from exc
                raise MattermostError(method, path, None, str(exc)) from exc
            except httpx.HTTPError as exc:
                raise MattermostError(method, path, None, str(exc)) from exc
            if resp.status_code != 429 or attempt >= RATE_LIMIT_RETRIES:
                break
            # Live within the server's per-user rate limit (CT-06): wait out the window, unless
            # the wait would pass the caller's deadline.
            wait = _retry_after(resp)
            if deadline is not None and self._clock() + wait >= deadline:
                raise DeadlineExceeded(method, path, 429, "rate limited past the caller's deadline")
            attempt += 1
            self._sleep(wait)
        if resp.status_code == 404:
            raise NotFound(method, path, 404, _detail(resp), _error_id(resp))
        if not resp.is_success:
            raise MattermostError(method, path, resp.status_code, _detail(resp), _error_id(resp))
        if not resp.content:
            return None
        return resp.json()

    def me(self) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", "/users/me")
        return result

    def get_user(self, user_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/users/{user_id}")
        return result

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = self._call("GET", f"/users/username/{username}")
        except NotFound:
            return None
        return result

    def get_bot(self, bot_user_id: str) -> dict[str, Any]:
        # include_deleted, so a pruned (disabled) bot this broker owns is still found.
        result: dict[str, Any] = self._call(
            "GET", f"/bots/{bot_user_id}", params={"include_deleted": "true"}
        )
        return result

    def list_bots(self) -> list[dict[str, Any]]:
        # The default include_deleted=false: a pruned bot is left out (D-100).
        bots: list[dict[str, Any]] = []
        page = 0
        while True:
            batch = self._call("GET", "/bots", params={"page": page, "per_page": BOTS_PAGE_SIZE})
            if not isinstance(batch, list):
                raise MattermostError("GET", "/bots", 200, "response is not a list")
            bots.extend(batch)
            if len(batch) < BOTS_PAGE_SIZE:
                return bots
            page += 1

    def create_bot(self, username: str, display_name: str, description: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "POST",
            "/bots",
            json={"username": username, "display_name": display_name, "description": description},
        )
        return result

    def set_user_image(self, user_id: str, png: bytes) -> None:
        """Set `user_id`'s avatar. For a bot, only its owner can (D-098)."""
        self._call(
            "POST", f"/users/{user_id}/image", files={"image": ("avatar.png", png, "image/png")}
        )

    def enable_bot(self, bot_user_id: str) -> None:
        self._call("POST", f"/bots/{bot_user_id}/enable")

    def disable_bot(self, bot_user_id: str) -> None:
        self._call("POST", f"/bots/{bot_user_id}/disable")

    def create_token(
        self, user_id: str, description: str, expires_at_ms: int | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"description": description}
        if expires_at_ms is not None:
            body["expires_at"] = expires_at_ms
        result: dict[str, Any] = self._call("POST", f"/users/{user_id}/tokens", json=body)
        return result

    def revoke_token(self, token_id: str) -> None:
        self._call("POST", "/users/tokens/revoke", json={"token_id": token_id})

    def direct_channel(self, user_id: str, other_id: str) -> str:
        """The DM channel between two users, created if missing; its ID."""
        result: dict[str, Any] = self._call("POST", "/channels/direct", json=[user_id, other_id])
        return str(result["id"])

    def posts_since(self, channel_id: str, since_ms: int) -> list[dict[str, Any]]:
        """A channel's posts created or changed since `since_ms`, oldest `create_at` first.

        The server's `since` also returns older posts edited or deleted since then, so a caller
        that wants only new posts filters on `create_at` itself.
        """
        result: dict[str, Any] = self._call(
            "GET", f"/channels/{channel_id}/posts", params={"since": since_ms}
        )
        posts = result.get("posts") or {}
        found = [posts[pid] for pid in result.get("order") or [] if pid in posts]
        return sorted(found, key=lambda post: int(post.get("create_at") or 0))

    def create_post(
        self, channel_id: str, message: str, props: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if props:
            body["props"] = props
        result: dict[str, Any] = self._call("POST", "/posts", json=body)
        return result

    # The agent legend (#231): the broker account posts, pins and edits its own post, as a
    # member of the channel, with no role beyond R-06's (#231's spike). A deleted post answers
    # 404 to its poster.
    def get_post(self, post_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/posts/{post_id}")
        return result

    def patch_post(self, post_id: str, message: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "PUT", f"/posts/{post_id}/patch", json={"message": message}
        )
        return result

    def pin_post(self, post_id: str) -> None:
        self._call("POST", f"/posts/{post_id}/pin")

    # Membership (#132). The broker account adds only bots it owns, as a plain `team_user` that
    # is itself a member of each channel: no role beyond R-06's is needed (the #132 probe).
    # An existing member is answered 201, the same as a new one. The config names the team and
    # channels by URL name or display name: the `get_*_by_name` calls resolve a URL name, and the
    # listings are what a display name is matched against. A private channel is seen only by a
    # member.
    def get_team_by_name(self, name: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/teams/name/{name}")
        return result

    def get_channel_by_name(self, team_id: str, name: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/teams/{team_id}/channels/name/{name}")
        return result

    def list_my_teams(self) -> list[dict[str, Any]]:
        return self._list("/users/me/teams")

    def list_team_channels(self, team_id: str) -> list[dict[str, Any]]:
        """The team's public channels, every page."""
        channels: list[dict[str, Any]] = []
        page = 0
        while True:
            batch = self._list(
                f"/teams/{team_id}/channels", params={"page": page, "per_page": CHANNELS_PAGE_SIZE}
            )
            channels.extend(batch)
            if len(batch) < CHANNELS_PAGE_SIZE:
                return channels
            page += 1

    def list_my_team_channels(self, team_id: str) -> list[dict[str, Any]]:
        """The broker account's own channels in the team, private ones included."""
        return self._list(f"/users/me/teams/{team_id}/channels")

    def _list(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self._call("GET", path, params=params)
        if not isinstance(result, list):
            raise MattermostError("GET", path, 200, "response is not a list")
        return result

    def add_team_member(self, team_id: str, user_id: str) -> None:
        self._call(
            "POST", f"/teams/{team_id}/members", json={"team_id": team_id, "user_id": user_id}
        )

    def add_channel_member(self, channel_id: str, user_id: str) -> None:
        self._call("POST", f"/channels/{channel_id}/members", json={"user_id": user_id})


def _retry_after(resp: httpx.Response) -> float:
    """Seconds to wait after a 429: Retry-After, else Mattermost's X-RateLimit-Reset, else 1."""
    for header in ("Retry-After", "X-RateLimit-Reset"):
        try:
            wait = float(resp.headers.get(header, ""))
        except ValueError:
            continue
        return min(max(wait, 0.1), RATE_LIMIT_MAX_WAIT_S)
    return 1.0


def _error_id(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    return str(body.get("id") or "") if isinstance(body, dict) else ""


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("id") or body)[:200]
    return str(body)[:200]
