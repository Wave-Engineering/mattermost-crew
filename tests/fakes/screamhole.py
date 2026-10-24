"""An HTTP fake of scream-hole's read API, and of the Discord REST writes behind it (Story 2.2).

`FakeDiscord` holds the guild: its channels, messages, DM channels and reactions. Two front ends
serve it, as in production:

- **scream-hole's read API** (`handle_screamhole`): `GET /api/v10/channels/{id}/messages` with a
  required snowflake `after` and an optional `limit` (the newest `limit` matches), oldest first;
  an uncached channel answers `200 []` (scream-hole #16); `GET /api/v10/guilds/{id}/channels`;
  and `GET /health`.
- **Discord's REST API** (`handle_discord`), for writes only: `POST /channels/{id}/messages`,
  `POST /users/@me/channels` and `PUT .../reactions/{emoji}/@me`, with `Authorization: Bot`.
  Any `GET` to Discord is recorded in `direct_reads` and refused: reads must go through
  scream-hole (D-013).

`transport()` is one `httpx.MockTransport` for both hosts, for unit tests. `serve()` runs the
scream-hole front end on a real local port, for integration tests.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

import httpx

DISCORD_EPOCH_MS = 1_420_070_400_000
SCREAMHOLE_URL = "http://screamhole.invalid"
DISCORD_API = "http://discord.invalid/api/v10"
GUILD_ID = "900000000000000001"
BOT_TOKEN = "fake-bot-token"
BOT_USER = {"id": "800000000000000001", "username": "crew-bot", "bot": True}


class FakeDiscord:
    def __init__(self, *, token: str = BOT_TOKEN, now: Callable[[], float] = time.time) -> None:
        self.token = token
        self.now = now
        self.channels: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.dm_channels: dict[str, str] = {}  # recipient -> channel
        self.reactions: list[tuple[str, str, str]] = []  # (channel, message, emoji)
        self.requests: list[tuple[str, str, str]] = []  # (service, method, path?query)
        self.direct_reads: list[str] = []
        self.rate_limit_next = 0
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)

    # -- the guild --------------------------------------------------------------------------
    def snowflake(self) -> str:
        ms = int(self.now() * 1000) - DISCORD_EPOCH_MS
        return str((ms << 22) + (next(self._seq) & 0x3FFFFF))

    def add_channel(self, name: str, *, type: int = 0, channel_id: str | None = None) -> str:
        cid = channel_id or str(700000000000000000 + next(self._ids))
        self.channels[cid] = {"id": cid, "name": name, "type": type, "guild_id": GUILD_ID}
        self.messages.setdefault(cid, [])
        return cid

    def add_message(
        self,
        channel: str,
        content: str,
        *,
        author: dict[str, Any] | None = None,
        reply_to: str | None = None,
        mentions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """A message as Discord stores it; scream-hole sees it at once (no poll lag here)."""
        msg_id = self.snowflake()
        msg: dict[str, Any] = {
            "id": msg_id,
            "channel_id": channel,
            "content": content,
            "author": dict(author or BOT_USER),
            "timestamp": datetime.fromtimestamp(self.now(), UTC).isoformat(),
            "type": 19 if reply_to else 0,
            "mentions": list(mentions or []),
        }
        if reply_to:
            msg["message_reference"] = {"message_id": reply_to, "channel_id": channel}
        self.messages.setdefault(channel, []).append(msg)
        return msg

    # -- scream-hole ------------------------------------------------------------------------
    def screamhole_response(self, method: str, path: str, query: dict[str, str]) -> tuple[int, Any]:
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        self.requests.append(("scream-hole", method, f"{path}?{qs}" if qs else path))
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}
        parts = path.strip("/").split("/")
        if method != "GET" or parts[:2] != ["api", "v10"]:
            return 501, {"proxy": "scream-hole", "error": "not routed"}
        rest = parts[2:]
        if len(rest) == 3 and rest[0] == "guilds" and rest[2] == "channels":
            if rest[1] != GUILD_ID:
                return 404, {"error": f"No cached data for guild {rest[1]}"}
            return 200, list(self.channels.values())
        if len(rest) == 3 and rest[0] == "channels" and rest[2] == "messages":
            after = query.get("after", "")
            if not after.isdigit():
                return 400, {"error": "`after` is required and must be a snowflake"}
            found = [m for m in self.messages.get(rest[1], []) if int(m["id"]) > int(after)]
            found.sort(key=lambda m: int(m["id"]))
            limit = query.get("limit")
            if limit is not None:
                if not limit.isdigit() or int(limit) <= 0:
                    return 400, {"error": "`limit` must be a positive integer"}
                found = found[-int(limit) :]
            return 200, found
        return 501, {"proxy": "scream-hole", "error": "not routed"}

    def handle_screamhole(self, request: httpx.Request) -> httpx.Response:
        query = {k: v for k, v in request.url.params.items()}
        status, body = self.screamhole_response(request.method, request.url.path, query)
        return httpx.Response(status, json=body)

    # -- Discord REST (writes) --------------------------------------------------------------
    def handle_discord(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v10")
        self.requests.append(("discord", request.method, path))
        if request.method == "GET":
            self.direct_reads.append(path)
            return httpx.Response(403, json={"message": "reads go through scream-hole"})
        if request.headers.get("Authorization") != f"Bot {self.token}":
            return httpx.Response(401, json={"message": "401: Unauthorized", "code": 0})
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return httpx.Response(429, json={"retry_after": 0.25, "global": False})
        parts = path.strip("/").split("/")
        body = json.loads(request.content) if request.content else None
        if request.method == "POST" and len(parts) == 3 and parts[::2] == ["channels", "messages"]:
            content = str(body.get("content", ""))
            if len(content) > 2000:
                return httpx.Response(400, json={"message": "content too long"})
            return httpx.Response(200, json=self.add_message(parts[1], content))
        if request.method == "POST" and path == "/users/@me/channels":
            recipient = str(body["recipient_id"])
            channel = self.dm_channels.setdefault(
                recipient, str(600000000000000000 + len(self.dm_channels))
            )
            self.messages.setdefault(channel, [])
            return httpx.Response(200, json={"id": channel, "type": 1})
        if (
            request.method == "PUT"
            and len(parts) == 7
            and parts[0] == "channels"
            and parts[2] == "messages"
            and parts[4] == "reactions"
            and parts[6] == "@me"
        ):
            ids = {m["id"] for m in self.messages.get(parts[1], [])}
            if parts[3] not in ids:
                return httpx.Response(404, json={"message": "Unknown Message", "code": 10008})
            self.reactions.append((parts[1], parts[3], unquote(parts[5])))
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"no route {request.method} {path}"})

    # -- transports -------------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == httpx.URL(SCREAMHOLE_URL).host:
            return self.handle_screamhole(request)
        if request.url.host == httpx.URL(DISCORD_API).host:
            return self.handle_discord(request)
        self.requests.append(("other", request.method, str(request.url)))
        return httpx.Response(502, json={"message": "unknown host"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    @contextmanager
    def serve(self) -> Iterator[str]:
        """scream-hole's read API on a real local port; yields its base URL."""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = httpx.URL(self.path)
                query = {k: v for k, v in url.params.items()}
                status, body = fake.screamhole_response("GET", url.path, query)
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
