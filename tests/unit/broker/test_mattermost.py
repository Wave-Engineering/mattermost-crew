"""Story 1.3: the broker's REST client, against a mock transport."""

from __future__ import annotations

import json

import httpx
import pytest
from crewbroker.mattermost import MattermostClient, MattermostError, NotFound


def test_list_bots_paginates_with_default_include_deleted() -> None:
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        seen.append(request.url.params)
        page = int(request.url.params["page"])
        count = 200 if page == 0 else 3
        return httpx.Response(200, json=[{"user_id": f"{page}-{i}"} for i in range(count)])

    client = MattermostClient("http://mm.invalid/", "tok", transport=httpx.MockTransport(handler))
    bots = client.list_bots()
    assert len(bots) == 203
    assert [p["page"] for p in seen] == ["0", "1"]
    assert all(p["per_page"] == "200" and "include_deleted" not in p for p in seen)


def test_errors_and_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/username/nobody"):
            return httpx.Response(404, json={"message": "not found"})
        if request.url.path.endswith("/bots/x"):
            assert request.url.params["include_deleted"] == "true"
            return httpx.Response(404, json={"message": "not found"})
        if request.url.path.endswith("/tokens"):
            body = json.loads(request.content)
            assert body == {"description": "d", "expires_at": 5}
            return httpx.Response(200, json={"id": "t", "token": "s", "expires_at": 5})
        return httpx.Response(403, json={"message": "no permission"})

    client = MattermostClient("http://mm.invalid", "tok", transport=httpx.MockTransport(handler))
    assert client.get_user_by_username("nobody") is None
    with pytest.raises(NotFound):
        client.get_bot("x")
    assert client.create_token("u", "d", expires_at_ms=5)["expires_at"] == 5
    with pytest.raises(MattermostError, match="403: no permission"):
        client.me()
    client.close()


def test_rate_limit_is_waited_out() -> None:
    """CT-06: a 429 is retried after the server's reset window, a bounded number of times."""
    answers = iter([429, 429, 200])
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(answers)
        if status == 429:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "2"}, json={})
        return httpx.Response(200, json={"id": "me"})

    client = MattermostClient(
        "http://mm.invalid", "tok", transport=httpx.MockTransport(handler), sleep=waits.append
    )
    assert client.me() == {"id": "me"}
    assert waits == [2.0, 2.0]

    def always_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "limit exceeded"})

    waits.clear()
    client = MattermostClient(
        "http://mm.invalid", "tok", transport=httpx.MockTransport(always_429), sleep=waits.append
    )
    with pytest.raises(MattermostError, match="429"):
        client.me()
    assert waits == [1.0] * 5


def test_membership_adds_and_error_id() -> None:
    """#132: the two adds' paths and bodies, and an error keeps Mattermost's `id`."""
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        if "/channels/denied/" in request.url.path:
            return httpx.Response(
                403, json={"id": "api.context.permissions.app_error", "message": "no permission"}
            )
        return httpx.Response(201, json={"user_id": "bot"})

    client = MattermostClient("http://mm.invalid", "tok", transport=httpx.MockTransport(handler))
    client.add_team_member("team", "bot")
    client.add_channel_member("chan", "bot")
    with pytest.raises(MattermostError) as caught:
        client.add_channel_member("denied", "bot")
    assert seen[:2] == [
        ("/api/v4/teams/team/members", {"team_id": "team", "user_id": "bot"}),
        ("/api/v4/channels/chan/members", {"user_id": "bot"}),
    ]
    assert caught.value.status == 403
    assert caught.value.error_id == "api.context.permissions.app_error"
    client.close()
