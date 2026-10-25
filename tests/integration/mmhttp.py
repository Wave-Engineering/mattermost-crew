"""The one test-side HTTP helper for Mattermost, rate-limit aware (#59).

CI applies the dev space's rate limits (`scripts/ci/mattermost-up.sh`), so a test's admin or
token call can meet a `429 Too Many Requests`. `mm_request` waits it out the way the broker
does (`crewbroker/mattermost.py`): `Retry-After`, else `X-RateLimit-Reset`, else 1 s. Every
Mattermost call in `tests/integration/` goes through it.

It lives here, not in `conftest.py`, so a unit test can import it without a live server;
`conftest.py` re-exports it.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

MAX_ATTEMPTS = 6
MAX_WAIT_S = 10.0
DEFAULT_WAIT_S = 1.0


class _Server(Protocol):
    @property
    def api(self) -> str: ...

    @property
    def auth_headers(self) -> dict[str, str]: ...


def retry_after(resp: httpx.Response) -> float:
    """Seconds to wait after a 429: Retry-After, else X-RateLimit-Reset, else 1, capped at 10."""
    for header in ("Retry-After", "X-RateLimit-Reset"):
        try:
            wait = float(resp.headers.get(header, ""))
        except ValueError:
            continue
        return min(max(wait, 0.1), MAX_WAIT_S)
    return DEFAULT_WAIT_S


def mm_request(
    mattermost: _Server,
    method: str,
    path: str,
    *,
    token: str | None = None,
    timeout: float = 15,
    client: httpx.Client | None = None,
    **kw: Any,
) -> httpx.Response:
    """Call `mattermost.api + path` as the admin, or as `token`, retrying on 429.

    Up to `MAX_ATTEMPTS` calls. After the last one the final response comes back unchanged,
    so the test still asserts on it. It never sleeps on anything but a 429. `client` lets a
    unit test supply an `httpx.MockTransport`.
    """
    if token is None:
        headers = dict(mattermost.auth_headers)
    else:
        headers = {"Authorization": f"Bearer {token}"}
    headers.update(kw.pop("headers", None) or {})
    url = f"{mattermost.api}{path}"
    send = client.request if client is not None else httpx.request
    attempt = 1
    while True:
        resp = send(method, url, headers=headers, timeout=timeout, **kw)
        if resp.status_code != 429 or attempt >= MAX_ATTEMPTS:
            return resp
        attempt += 1
        time.sleep(retry_after(resp))
