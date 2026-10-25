"""`mm_request`, the integration tests' 429-aware Mattermost helper (#59), against a mock."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from tests.integration import mmhttp
from tests.integration.mmhttp import mm_request


class Server:
    api = "http://mm.invalid/api/v4"

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer admin-token"}


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(mmhttp.time, "sleep", waits.append)
    return waits


def scripted(
    responses: Iterator[httpx.Response], seen: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(responses)

    return handler


def client_for(responses: list[httpx.Response], seen: list[httpx.Request]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(scripted(iter(responses), seen)))


def test_mm_request_retries_429(sleeps: list[float]) -> None:
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(201, json={"id": "p1"}),
    ]
    with client_for(responses, seen) as client:
        resp = mm_request(Server(), "POST", "/posts", json={"message": "hi"}, client=client)
    assert resp.status_code == 201
    assert len(seen) == 3
    assert all(str(r.url) == "http://mm.invalid/api/v4/posts" for r in seen)
    assert all(r.headers["Authorization"] == "Bearer admin-token" for r in seen)
    assert len(sleeps) == 2 and all(w <= 0.1 for w in sleeps)

    # A 429 with neither header waits the 1 s default.
    sleeps.clear()
    seen.clear()
    responses = [httpx.Response(429), httpx.Response(200)]
    with client_for(responses, seen) as client:
        resp = mm_request(Server(), "GET", "/users/me", token="agent-token", client=client)
    assert resp.status_code == 200
    assert sleeps == [1.0]
    assert seen[0].headers["Authorization"] == "Bearer agent-token"


def test_mm_request_wait_rule(sleeps: list[float]) -> None:
    """X-RateLimit-Reset is the fallback, and a long wait is capped at 10 s."""
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(429, headers={"X-RateLimit-Reset": "3"}),
        httpx.Response(429, headers={"Retry-After": "60"}),
        httpx.Response(200),
    ]
    with client_for(responses, seen) as client:
        mm_request(Server(), "GET", "/users/me", client=client)
    assert sleeps == [3.0, 10.0]


def test_mm_request_gives_up(sleeps: list[float]) -> None:
    seen: list[httpx.Request] = []
    responses = [httpx.Response(429, text=f"limit exceeded {i}") for i in range(7)]
    with client_for(responses, seen) as client:
        resp = mm_request(Server(), "GET", "/users/me", client=client)
    assert resp.status_code == 429
    assert resp.text == "limit exceeded 5"
    assert len(seen) == 6
    assert len(sleeps) == 5


@pytest.mark.parametrize("status", [200, 404])
def test_mm_request_no_sleep_on_success(sleeps: list[float], status: int) -> None:
    seen: list[httpx.Request] = []
    with client_for([httpx.Response(status)], seen) as client:
        resp = mm_request(Server(), "GET", "/users/me", client=client)
    assert resp.status_code == status
    assert len(seen) == 1
    assert sleeps == []
