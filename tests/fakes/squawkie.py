"""A fake of squawkie: its lists, its squawks and its SSE feed (Story 3.2).

`FakeSquawkie` holds lists and squawks as squawkie does, and serves the routes the daemon's
squawkie adapter uses:

- `GET /api/stream`: `text/event-stream`. It writes `: connected` at once (as squawkie does,
  #116), then one `data: <json>` frame per broadcast event.
- `GET /api/lists`: every list, with its `last_activity`.
- `GET /api/lists/{id}/squawks`: the list's squawks.

With `token` set, every request needs `Authorization: Bearer <token>`, else 401. With
`refuse_streams` above zero, that many stream requests answer 503. `drop_streams()` ends every
open stream, as a server restart or a network drop would. `update` changes a squawk and
broadcasts `squawk.updated`, as squawkie's API does.

Two front ends serve it: `transport()` (an async `httpx.MockTransport`, for unit tests) and
`serve()` (a real local HTTP server on its own thread, for the integration tests).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import queue
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

URL = "http://squawkie.invalid"
_END = b""


class _Subscriber:
    """One open stream: frames queued for it, an empty frame ending it."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self.loop = loop
        self.aq: asyncio.Queue[bytes] | None = asyncio.Queue() if loop is not None else None
        self.tq: queue.Queue[bytes] = queue.Queue()
        self.open = True

    def put(self, frame: bytes) -> None:
        if not self.open:
            return
        if self.aq is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(self.aq.put_nowait, frame)
        else:
            self.tq.put(frame)
        if frame == _END:
            self.open = False


class FakeSquawkie:
    def __init__(self, *, token: str | None = None) -> None:
        self.token = token
        self.lists: dict[int, dict[str, Any]] = {}
        self.squawks: dict[int, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.refuse_streams = 0
        self.subscribers: list[_Subscriber] = []
        self._ids = itertools.count(1)
        self._clock = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        self._lock = threading.Lock()

    # -- state ------------------------------------------------------------------------------
    def now(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat().replace("+00:00", "Z")

    def add_list(self, name: str) -> int:
        list_id = next(self._ids)
        stamp = self.now()
        self.lists[list_id] = {"id": list_id, "name": name, "created_at": stamp}
        return list_id

    def add_squawk(self, list_id: int, text: str, state: str = "open") -> dict[str, Any]:
        sid = next(self._ids)
        stamp = self.now()
        seq = 1 + sum(1 for s in self.squawks.values() if s["list_id"] == list_id)
        squawk = {
            "id": sid,
            "list_id": list_id,
            "seq": seq,
            "text": text,
            "state": state,
            "initials": "bj",
            "created_at": stamp,
            "updated_at": stamp,
            "has_image": False,
            "image_ids": [],
            # squawkie-talkie #168: answer options, the one picked, and BJ's note.
            "options": [],
            "choice": None,
            "note": "",
        }
        self.squawks[sid] = squawk
        self.broadcast({"type": "squawk.created", "squawk": self.event_view(squawk)})
        return squawk

    @staticmethod
    def event_view(squawk: dict[str, Any]) -> dict[str, Any]:
        """A squawk as squawkie broadcasts it: the note is withheld (sent as "") while the
        squawk is still open, so a half-written draft is never pushed. `GET` is not redacted."""
        view = dict(squawk)
        if view["state"] == "open":
            view["note"] = ""
        return view

    def update(
        self,
        squawk_id: int,
        *,
        text: str | None = None,
        state: str | None = None,
        initials: str | None = None,
        choice: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Change a squawk and broadcast `squawk.updated`. As squawkie does, `initials` records
        who made the change (the web UI and `sqtk` always send them). A `choice` disposes the
        squawk as `confirmed` and records it; a plain `state` change clears any choice."""
        squawk = self.squawks[squawk_id]
        if text is not None:
            squawk["text"] = text
        if choice is not None:
            squawk["state"] = "confirmed"
            squawk["choice"] = choice
        elif state is not None:
            squawk["state"] = state
            squawk["choice"] = None
        if note is not None:
            squawk["note"] = note
        if initials is not None:
            squawk["initials"] = initials
        squawk["updated_at"] = self.now()
        self.broadcast({"type": "squawk.updated", "squawk": self.event_view(squawk)})
        return squawk

    def last_activity(self, list_id: int) -> str:
        stamps = [self.lists[list_id]["created_at"]]
        for s in self.squawks.values():
            if s["list_id"] == list_id:
                stamps += [s["created_at"], s["updated_at"]]
        return max(stamps)

    # -- the feed ---------------------------------------------------------------------------
    def broadcast(self, event: dict[str, Any]) -> None:
        self.send_raw(f"data: {json.dumps(event)}\n\n")

    def send_raw(self, text: str) -> None:
        with self._lock:
            subs = [s for s in self.subscribers if s.open]
        for sub in subs:
            sub.put(text.encode())

    def drop_streams(self) -> None:
        """End every open stream."""
        with self._lock:
            subs, self.subscribers = list(self.subscribers), []
        for sub in subs:
            sub.put(_END)

    def open_streams(self) -> int:
        with self._lock:
            return sum(1 for s in self.subscribers if s.open)

    # -- routing ----------------------------------------------------------------------------
    def _authorized(self, header: str | None) -> bool:
        return self.token is None or header == f"Bearer {self.token}"

    def route(self, method: str, path: str, auth: str | None) -> tuple[int, Any]:
        """A JSON route's `(status, body)`; the stream is handled by each front end."""
        self.requests.append((method, path))
        if not self._authorized(auth):
            return 401, {"error": "unauthorized"}
        parts = path.strip("/").split("/")
        if method == "GET" and parts == ["api", "lists"]:
            return 200, [
                {**lst, "last_activity": self.last_activity(lid)}
                for lid, lst in sorted(self.lists.items())
            ]
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "lists"]:
            if parts[3] == "squawks" and parts[2].isdigit() and int(parts[2]) in self.lists:
                lid = int(parts[2])
                return 200, [dict(s) for s in self.squawks.values() if s["list_id"] == lid]
            return 404, {"error": "list not found"}
        return 404, {"error": "not found"}

    def _stream_status(self, auth: str | None) -> int:
        self.requests.append(("GET", "/api/stream"))
        if not self._authorized(auth):
            return 401
        if self.refuse_streams:
            self.refuse_streams -= 1
            return 503
        return 200

    def _subscribe(self, loop: asyncio.AbstractEventLoop | None) -> _Subscriber:
        sub = _Subscriber(loop)
        with self._lock:
            self.subscribers.append(sub)
        sub.put(b": connected\n\n")
        return sub

    # -- front end: in process --------------------------------------------------------------
    async def handle(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization")
        if request.url.path == "/api/stream":
            status = self._stream_status(auth)
            if status != 200:
                return httpx.Response(status, json={"error": "refused"})
            sub = self._subscribe(asyncio.get_running_loop())

            async def body() -> AsyncIterator[bytes]:
                assert sub.aq is not None
                while True:
                    frame = await sub.aq.get()
                    if frame == _END:
                        return
                    yield frame

            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, content=body()
            )
        status, data = self.route(request.method, request.url.path, auth)
        return httpx.Response(status, json=data)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    # -- front end: a real port -------------------------------------------------------------
    @contextmanager
    def serve(self) -> Iterator[str]:
        """squawkie on a real local port; yields its base URL."""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                auth = self.headers.get("Authorization")
                path = self.path.split("?", 1)[0]
                if path == "/api/stream":
                    self._stream(auth)
                    return
                status, data = fake.route("GET", path, auth)
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _stream(self, auth: str | None) -> None:
                status = fake._stream_status(auth)
                if status != 200:
                    raw = b'{"error":"refused"}'
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                sub = fake._subscribe(None)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        frame = sub.tq.get()
                        if frame == _END:
                            break
                        self.wfile.write(frame)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            self.drop_streams()
            server.shutdown()
            server.server_close()
            thread.join()
