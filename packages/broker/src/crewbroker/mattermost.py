"""The broker's Mattermost REST client: only the calls the broker makes, as the broker account.

The broker account holds `system_user_access_token` patched with `create_bot`, `read_bots`
and `manage_bots`, and nothing more (R-06). So it can read and manage the bots it owns, and
no others: `GET /bots/{id}` is 404 for a bot it does not own (D-100).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

DEFAULT_TIMEOUT_S = 10.0
BOTS_PAGE_SIZE = 200
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_MAX_WAIT_S = 5.0


class MattermostError(Exception):
    """A Mattermost API call failed."""

    def __init__(self, method: str, path: str, status: int | None, detail: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        code = status if status is not None else "error"
        super().__init__(f"{method} {path} -> {code}: {detail}")


class NotFound(MattermostError):
    """The server answered 404."""


class MattermostAPI(Protocol):
    """What the broker needs from a server. `MattermostClient` is the real one."""

    def me(self) -> dict[str, Any]: ...

    def get_user(self, user_id: str) -> dict[str, Any]: ...

    def get_user_by_username(self, username: str) -> dict[str, Any] | None: ...

    def get_bot(self, bot_user_id: str) -> dict[str, Any]: ...

    def list_bots(self) -> list[dict[str, Any]]: ...

    def create_bot(self, username: str, display_name: str, description: str) -> dict[str, Any]: ...

    def enable_bot(self, bot_user_id: str) -> None: ...

    def disable_bot(self, bot_user_id: str) -> None: ...

    def create_token(
        self, user_id: str, description: str, expires_at_ms: int | None = None
    ) -> dict[str, Any]: ...

    def revoke_token(self, token_id: str) -> None: ...


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
    ) -> None:
        self.url = url.rstrip("/")
        self._sleep = sleep
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
    ) -> Any:
        attempt = 0
        while True:
            try:
                resp = self._http.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                raise MattermostError(method, path, None, str(exc)) from exc
            if resp.status_code != 429 or attempt >= RATE_LIMIT_RETRIES:
                break
            # Live within the server's per-user rate limit (CT-06): wait out the window.
            attempt += 1
            self._sleep(_retry_after(resp))
        if resp.status_code == 404:
            raise NotFound(method, path, 404, _detail(resp))
        if not resp.is_success:
            raise MattermostError(method, path, resp.status_code, _detail(resp))
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


def _retry_after(resp: httpx.Response) -> float:
    """Seconds to wait after a 429: Retry-After, else Mattermost's X-RateLimit-Reset, else 1."""
    for header in ("Retry-After", "X-RateLimit-Reset"):
        try:
            wait = float(resp.headers.get(header, ""))
        except ValueError:
            continue
        return min(max(wait, 0.1), RATE_LIMIT_MAX_WAIT_S)
    return 1.0


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("id") or body)[:200]
    return str(body)[:200]
