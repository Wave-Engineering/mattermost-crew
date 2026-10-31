"""Story 1.3: the broker's REST client, against a mock transport."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from crewbroker.mattermost import (
    DeadlineExceeded,
    MattermostClient,
    MattermostError,
    NotFound,
    call_deadline,
    uncapped,
)
from crewbroker.mint import clear_expiry_cache, mint_token, native_expiry_supported
from crewbroker.state import TokenStore


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


def test_rate_limit_respects_deadline() -> None:
    """#158: under `call_deadline`, a 429 whose wait would pass the deadline raises
    `DeadlineExceeded` at once, without sleeping."""
    waits: list[float] = []

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "5"}, json={})

    client = MattermostClient(
        "http://mm.invalid",
        "tok",
        transport=httpx.MockTransport(limited),
        sleep=waits.append,
        clock=lambda: 1000.0,
    )
    with call_deadline(1001.0), pytest.raises(DeadlineExceeded, match="429"):
        client.me()
    assert waits == []
    with call_deadline(999.0), pytest.raises(DeadlineExceeded, match="deadline passed"):
        client.me()  # already past: no request at all


def test_request_timeout_capped_by_deadline() -> None:
    """#158: each request's timeout is the smaller of the client's and the time left."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"id": "me"})

    client = MattermostClient(
        "http://mm.invalid", "tok", transport=httpx.MockTransport(handler), clock=lambda: 1000.0
    )
    with call_deadline(1003.0):
        client.me()
    with call_deadline(1060.0):
        client.me()
    client.me()  # no deadline
    assert [t["read"] for t in seen] == [3.0, 10.0, 10.0]


def test_timeout_past_the_deadline_is_deadline_exceeded() -> None:
    """#158: a request cut off by the capped timeout, once the deadline has passed, is a
    `DeadlineExceeded`; without a deadline it stays a plain `MattermostError`."""
    now = [1000.0]

    def slow(request: httpx.Request) -> httpx.Response:
        now[0] += 5
        raise httpx.ReadTimeout("slow", request=request)

    client = MattermostClient(
        "http://mm.invalid", "tok", transport=httpx.MockTransport(slow), clock=lambda: now[0]
    )
    with call_deadline(1002.0), pytest.raises(DeadlineExceeded):
        client.me()
    with pytest.raises(MattermostError) as exc:
        client.me()
    assert not isinstance(exc.value, DeadlineExceeded)


def test_slow_create_token_is_recorded_past_the_deadline(tmp_path: Path) -> None:
    """#158 review: the request that creates a token is never cut short by the deadline. Its
    reply carries the only copy of the secret, so a reply arriving past the deadline is still
    recorded, and the next call's supersede can revoke it (#41)."""
    now = [1000.0]
    timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        now[0] += 3  # the server took 3 s; the deadline was 1 s away
        return httpx.Response(200, json={"id": "t1", "token": "secret", "user_id": "b"})

    client = MattermostClient(
        "http://mm.invalid", "tok", transport=httpx.MockTransport(handler), clock=lambda: now[0]
    )
    tokens = TokenStore(tmp_path)
    with call_deadline(1001.0):
        token = mint_token(
            client, "b", tokens, expiry_supported=False, now=datetime(2026, 10, 31, tzinfo=UTC)
        )
    assert timeouts == [10.0]  # the client's own timeout, not the 1 s left
    assert token.token_id == "t1" and tokens.get("t1") is not None


def test_uncapped_still_refuses_past_the_deadline() -> None:
    """`uncapped` drops only the cap: a 429 past the deadline still raises, without sleeping,
    and nothing is sent once it has passed."""
    sent: list[str] = []
    waits: list[float] = []

    def limited(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "5"}, json={})

    client = MattermostClient(
        "http://mm.invalid",
        "tok",
        transport=httpx.MockTransport(limited),
        sleep=waits.append,
        clock=lambda: 1000.0,
    )
    with call_deadline(1001.0), uncapped(), pytest.raises(DeadlineExceeded, match="429"):
        client.me()
    with call_deadline(999.0), uncapped(), pytest.raises(DeadlineExceeded):
        client.me()
    assert waits == [] and len(sent) == 1


def test_expiry_probe_revokes_past_the_deadline() -> None:
    """#158 review: the probe's revoke is sent even when its `create_token` used up the
    deadline, so no probe token is left live on the broker's own account."""
    now = [1000.0]
    revoked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/tokens/revoke"):
            revoked.append(json.loads(request.content)["token_id"])
            return httpx.Response(200, json={"status": "OK"})
        now[0] += 3
        return httpx.Response(200, json={"id": "probe", "token": "s"})

    client = MattermostClient(
        "http://mm.invalid", "tok", transport=httpx.MockTransport(handler), clock=lambda: now[0]
    )
    clear_expiry_cache()
    try:
        with call_deadline(1001.0):
            assert native_expiry_supported(client, "http://mm.invalid", "broker") is False
    finally:
        clear_expiry_cache()
    assert revoked == ["probe"]


def test_no_deadline_keeps_retries() -> None:
    """#158: outside `call_deadline` (prune, the DM listener) a 429 is still waited out."""
    answers = iter([429, 200])
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if next(answers) == 429:
            return httpx.Response(429, headers={"Retry-After": "5"}, json={})
        return httpx.Response(200, json={"id": "me"})

    client = MattermostClient(
        "http://mm.invalid", "tok", transport=httpx.MockTransport(handler), sleep=waits.append
    )
    with call_deadline(None):
        assert client.me() == {"id": "me"}
    assert waits == [5.0]


def test_membership_adds_and_error_id() -> None:
    """#132: the name lookups, the two adds' paths and bodies, and an error keeps its `id`."""
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if "/channels/denied/" in request.url.path:
            return httpx.Response(
                403, json={"id": "api.context.permissions.app_error", "message": "no permission"}
            )
        if request.method == "GET":
            return httpx.Response(200, json={"id": "the-id"})
        return httpx.Response(201, json={"user_id": "bot"})

    client = MattermostClient("http://mm.invalid", "tok", transport=httpx.MockTransport(handler))
    assert client.get_team_by_name("oaw")["id"] == "the-id"
    assert client.get_channel_by_name("tid", "town-square")["id"] == "the-id"
    client.add_team_member("team", "bot")
    client.add_channel_member("chan", "bot")
    with pytest.raises(MattermostError) as caught:
        client.add_channel_member("denied", "bot")
    assert seen[:4] == [
        ("GET", "/api/v4/teams/name/oaw", None),
        ("GET", "/api/v4/teams/tid/channels/name/town-square", None),
        ("POST", "/api/v4/teams/team/members", {"team_id": "team", "user_id": "bot"}),
        ("POST", "/api/v4/channels/chan/members", {"user_id": "bot"}),
    ]
    assert caught.value.status == 403
    assert caught.value.error_id == "api.context.permissions.app_error"
    client.close()


def test_name_listings_for_display_name_matching() -> None:
    """#132: the broker's teams, the team's public channels (every page), its own channels."""
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v4/teams/t/channels":
            count = 200 if request.url.params["page"] == "0" else 1
            return httpx.Response(200, json=[{"id": f"c{i}"} for i in range(count)])
        return httpx.Response(200, json=[{"id": "x"}])

    client = MattermostClient("http://mm.invalid", "tok", transport=httpx.MockTransport(handler))
    assert client.list_my_teams() == [{"id": "x"}]
    assert len(client.list_team_channels("t")) == 201
    assert client.list_my_team_channels("t") == [{"id": "x"}]
    assert [path for path, _ in seen] == [
        "/api/v4/users/me/teams",
        "/api/v4/teams/t/channels",
        "/api/v4/teams/t/channels",
        "/api/v4/users/me/teams/t/channels",
    ]
    assert [p.get("page") for _, p in seen[1:3]] == ["0", "1"]

    def not_a_list(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x"})

    client = MattermostClient("http://mm.invalid", "tok", transport=httpx.MockTransport(not_a_list))
    with pytest.raises(MattermostError, match="not a list"):
        client.list_my_teams()
