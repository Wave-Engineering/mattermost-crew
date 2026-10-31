"""An HTTP fake of scream-hole, and of the Discord REST API behind it (Stories 2.2, 2.4c).

`FakeDiscord` holds the guild: its channels, roles, members, messages (with their embeds and
attachments), DM channels and reactions. Two front ends serve it, as in production:

- **scream-hole** (`handle_screamhole`), which proxies every `/api/v10` path (D-106). Reads:
  `GET /api/v10/channels/{id}/messages` with a required snowflake `after` and an optional
  `limit` (the newest `limit` matches), oldest first, an uncached channel answering `200 []`
  (scream-hole #16); `GET /api/v10/channels/{id}/messages/{id}`;
  `GET /api/v10/guilds/{id}/channels`, `.../roles` and `.../members/search?query=`; and
  `GET /health`. Writes are passed through to the Discord side, with the bot token.
  With `screamhole_healthy` false, every scream-hole request answers 503.
- **Discord's REST API** (`handle_discord`), for writes: `POST /channels/{id}/messages` (JSON,
  or multipart with `payload_json` and `files[n]`), `POST /users/@me/channels`,
  `PUT .../reactions/{emoji}/@me`, `POST /guilds/{id}/channels` and
  `POST /channels/{id}/threads`, with `Authorization: Bot`. Any `GET` to Discord is recorded
  in `direct_reads` and refused: reads go through scream-hole only (D-013, D-106).

Every request lands in `requests` as `(service, method, path?query)`, where `service` is the
front end it reached. `transport()` is one `httpx.MockTransport` for both hosts, for unit and
in-process integration tests. `serve()` runs scream-hole's read side on a real local port.
"""

from __future__ import annotations

import email.parser
import email.policy
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


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(fields, files)` of a multipart/form-data body; files are `{field: (name, bytes)}`."""
    raw = f"Content-Type: {content_type}\r\n\r\n".encode() + body
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    fields: dict[str, Any] = {}
    files: dict[str, Any] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            files[str(name)] = (filename, payload)
        else:
            fields[str(name)] = payload.decode()
    return fields, files


class FakeDiscord:
    def __init__(self, *, token: str = BOT_TOKEN, now: Callable[[], float] = time.time) -> None:
        self.token = token
        self.now = now
        self.channels: dict[str, dict[str, Any]] = {}
        self.roles: list[dict[str, Any]] = []
        self.members: list[dict[str, Any]] = []
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.files: dict[tuple[str, str], bytes] = {}  # (message id, filename) -> content
        self.dm_channels: dict[str, str] = {}  # recipient -> channel
        self.reactions: list[tuple[str, str, str]] = []  # (channel, message, emoji)
        self.edits: list[tuple[str, str, dict[str, Any]]] = []  # (channel, message, body)
        self.requests: list[tuple[str, str, str]] = []  # (service, method, path?query)
        self.direct_reads: list[str] = []
        self.rate_limit_next = 0
        self.screamhole_healthy = True
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)

    # -- the guild --------------------------------------------------------------------------
    def snowflake(self) -> str:
        ms = int(self.now() * 1000) - DISCORD_EPOCH_MS
        return str((ms << 22) + (next(self._seq) & 0x3FFFFF))

    def add_channel(self, name: str, *, type: int = 0, channel_id: str | None = None) -> str:
        cid = channel_id or str(700000000000000000 + next(self._ids))
        self.channels[cid] = {
            "id": cid,
            "name": name,
            "type": type,
            "guild_id": GUILD_ID,
            "position": len(self.channels),
        }
        self.messages.setdefault(cid, [])
        return cid

    def add_role(self, name: str) -> str:
        rid = str(710000000000000000 + next(self._ids))
        self.roles.append({"id": rid, "name": name})
        return rid

    def add_member(self, username: str, *, global_name: str = "", nick: str = "") -> str:
        uid = str(720000000000000000 + next(self._ids))
        user = {"id": uid, "username": username, "global_name": global_name or None}
        self.members.append({"user": user, "nick": nick or None})
        return uid

    def add_message(
        self,
        channel: str,
        content: str,
        *,
        author: dict[str, Any] | None = None,
        reply_to: str | None = None,
        mentions: list[dict[str, Any]] | None = None,
        embeds: list[dict[str, Any]] | None = None,
        files: list[tuple[str, bytes]] | None = None,
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
            "embeds": list(embeds or []),
            "attachments": [],
        }
        for n, (filename, data) in enumerate(files or []):
            att = {"id": f"{msg_id}{n}", "filename": filename, "size": len(data)}
            msg["attachments"].append(att)
            self.files[(msg_id, filename)] = data
        if reply_to:
            msg["message_reference"] = {"message_id": reply_to, "channel_id": channel}
        self.messages.setdefault(channel, []).append(msg)
        return msg

    def reads(self, service: str) -> list[str]:
        """Every GET `service` saw, but scream-hole's own `/health`."""
        return [p for s, m, p in self.requests if s == service and m == "GET" and p != "/health"]

    # -- scream-hole ------------------------------------------------------------------------
    def screamhole_response(self, method: str, path: str, query: dict[str, str]) -> tuple[int, Any]:
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        self.requests.append(("scream-hole", method, f"{path}?{qs}" if qs else path))
        if not self.screamhole_healthy:
            return 503, {"status": "unhealthy"}
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}
        parts = path.strip("/").split("/")
        if method != "GET" or parts[:2] != ["api", "v10"]:
            return 501, {"proxy": "scream-hole", "error": "not routed"}
        rest = parts[2:]
        if len(rest) >= 3 and rest[0] == "guilds":
            if rest[1] != GUILD_ID:
                return 404, {"error": f"No cached data for guild {rest[1]}"}
            if rest[2:] == ["channels"]:
                return 200, list(self.channels.values())
            if rest[2:] == ["roles"]:
                return 200, list(self.roles)
            if rest[2:] == ["members", "search"]:
                q = query.get("query", "").lower()
                return 200, [
                    m
                    for m in self.members
                    if str(m["user"]["username"]).lower().startswith(q)
                    or str(m["user"].get("global_name") or "").lower().startswith(q)
                    or str(m.get("nick") or "").lower().startswith(q)
                ]
        if len(rest) == 4 and rest[0] == "channels" and rest[2] == "messages":
            found = [m for m in self.messages.get(rest[1], []) if m["id"] == rest[3]]
            if not found:
                return 404, {"message": "Unknown Message", "code": 10008}
            return 200, found[0]
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
        if request.method != "GET":
            # scream-hole proxies the write to Discord, with the caller's bot token (D-106).
            self.requests.append(("scream-hole", request.method, request.url.path))
            if not self.screamhole_healthy:
                return httpx.Response(503, json={"status": "unhealthy"})
            return self.discord_write(request, request.url.path.removeprefix("/api/v10"))
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
        return self.discord_write(request, path)

    def _body(self, request: httpx.Request) -> tuple[Any, list[tuple[str, bytes]]]:
        ctype = request.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            fields, files = parse_multipart(ctype, request.content)
            body = json.loads(fields.get("payload_json", "{}"))
            ordered = [files[k] for k in sorted(files)]
            return body, ordered
        return (json.loads(request.content) if request.content else None), []

    def discord_write(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bot {self.token}":
            return httpx.Response(401, json={"message": "401: Unauthorized", "code": 0})
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return httpx.Response(429, json={"retry_after": 0.25, "global": False})
        parts = path.strip("/").split("/")
        body, files = self._body(request)
        if request.method == "POST" and len(parts) == 3 and parts[::2] == ["channels", "messages"]:
            content = str(body.get("content", ""))
            if len(content) > 2000:
                return httpx.Response(400, json={"message": "content too long"})
            msg = self.add_message(
                parts[1], content, embeds=body.get("embeds"), files=files or None
            )
            return httpx.Response(200, json=msg)
        if request.method == "POST" and path == "/users/@me/channels":
            recipient = str(body["recipient_id"])
            channel = self.dm_channels.setdefault(
                recipient, str(600000000000000000 + len(self.dm_channels))
            )
            self.messages.setdefault(channel, [])
            return httpx.Response(200, json={"id": channel, "type": 1})
        if request.method == "POST" and len(parts) == 3 and parts[::2] == ["guilds", "channels"]:
            if parts[1] != GUILD_ID:
                return httpx.Response(403, json={"message": "Missing Access", "code": 50001})
            cid = self.add_channel(str(body["name"]), type=int(body.get("type", 0)))
            return httpx.Response(201, json=self.channels[cid])
        if request.method == "POST" and len(parts) == 3 and parts[::2] == ["channels", "threads"]:
            if parts[1] not in self.channels:
                return httpx.Response(404, json={"message": "Unknown Channel", "code": 10003})
            tid = self.add_channel(str(body["name"]), type=int(body.get("type", 11)))
            self.channels[tid]["parent_id"] = parts[1]
            return httpx.Response(201, json=self.channels[tid])
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
        if request.method == "PATCH" and len(parts) == 4 and parts[::2] == ["channels", "messages"]:
            # An edit (#149): the bot's own message only, as Discord allows.
            found = [m for m in self.messages.get(parts[1], []) if m["id"] == parts[3]]
            if not found:
                return httpx.Response(404, json={"message": "Unknown Message", "code": 10008})
            if len(str(body.get("content", ""))) > 2000:
                return httpx.Response(400, json={"message": "content too long"})
            self.edits.append((parts[1], parts[3], body))
            msg = found[0]
            if "content" in body:
                msg["content"] = str(body["content"])
            if "embeds" in body:
                msg["embeds"] = body["embeds"]
            return httpx.Response(200, json=msg)
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
        """scream-hole's read side on a real local port; yields its base URL."""
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
