"""The broker's model proxy (#141; D-111): the kiosk's only way to its model.

One HTTP/1.1 request per connection, on `<server>.model.sock`. Only
`POST /a/<answer_id>/v1/messages` and `POST /a/<answer_id>/v1/messages/count_tokens` are
served; anything else is 404. The proxy:

- reads the request body whole (`Content-Length`, at most `MAX_REQUEST_BYTES`) and forces its
  `model` to the pinned model;
- drops every credential header the caller sent, and adds the broker's own key as `x-api-key`;
- counts each answer's `/v1/messages` calls. The one after `max_turns` is refused with a 429,
  an Anthropic-shaped `turn limit reached` error and `x-should-retry: false`, so the client
  answers with what it has instead of backing off and retrying;
- streams the upstream response back byte for byte, and reads the token counts from its `usage`
  (a JSON body, or a `text/event-stream`), for the metrics.

`model.conf` and the key are read with `crewkiosk-run`'s rules: `parse_model_conf` and
`parse_key`. The key file is the broker's own, `/etc/crewkiosk/broker-model.credential`, never
the runner's `model.credential` (babelfish, 2026-10-30: two keys).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import ssl
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-5-5"
# Claude Code's own aliases: with no gateway, the API would get one as a model id (crewkiosk-run).
CLAUDE_ALIASES = frozenset({"default", "best", "fable", "sonnet", "opus", "haiku", "opusplan"})
MAX_REQUEST_BYTES = 1024 * 1024
MAX_HEAD_BYTES = 16 * 1024
# The part of a response body kept to read `usage` from, when it isn't an event stream.
MAX_METER_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT_S = 30
RESPONSE_TIMEOUT_S = 300
PATH_RE = re.compile(
    r"^/a/([0-9a-f]{32})(/v1/messages(?:/count_tokens)?)(?:\?([A-Za-z0-9=&_.-]*))?$"
)
# Request headers passed through. Everything else, every credential among them, is dropped.
PASS_HEADERS = frozenset({"anthropic-version", "anthropic-beta", "content-type", "accept"})


class ConfError(Exception):
    pass


@dataclass(frozen=True)
class Upstream:
    scheme: str
    host: str
    port: int
    path: str  # a prefix, no trailing slash

    @classmethod
    def parse(cls, url: str) -> Upstream:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ConfError(f"not an http(s) URL: {url!r}")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return cls(parts.scheme, parts.hostname, port, parts.path.rstrip("/"))


@dataclass(frozen=True)
class ModelConf:
    base_url: str
    model: str


def parse_model_conf(path: Path) -> ModelConf:
    """`model.conf` as `crewkiosk-run` reads it: `base_url` (https) and `model`, nothing else.
    A missing file is the defaults; one that exists but can't be read is an error."""
    base_url, model = "", DEFAULT_MODEL
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfError(f"cannot read {path}: {exc.strerror}") from None
        for n, line in enumerate(text.splitlines(), 1):
            key, _, value = line.partition("=")
            if (not key.strip() and not value) or key.lstrip().startswith("#"):
                continue
            if key == "base_url":
                if not re.fullmatch(r"https://\S+", value):
                    raise ConfError(f"{path}: base_url must be an https URL with no spaces")
                base_url = value
            elif key == "model":
                if not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
                    raise ConfError(f"{path}: model may hold only A-Z a-z 0-9 . _ : -")
                model = value
            else:
                # The line number, never the text: a key pasted here has no `=` to split.
                raise ConfError(f"{path} line {n}: unknown key (only base_url and model)")
    if not base_url and model.lower() in CLAUDE_ALIASES:
        raise ConfError(f"{path}: with no base_url, model must be a full model name")
    return ModelConf(base_url or DEFAULT_BASE_URL, model)


def parse_key(path: Path) -> str:
    """The key file's first line: the key alone, or `ANTHROPIC_API_KEY=<key>`."""
    try:
        first = path.read_bytes().split(b"\n", 1)[0].decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfError(f"cannot read {path}: {type(exc).__name__}") from None
    key = first.removeprefix("ANTHROPIC_API_KEY=")
    if "=" in key or any(c.isspace() for c in key):
        raise ConfError(f"{path} must hold the key alone, with no = or whitespace in it")
    if not key:
        raise ConfError(f"{path} is empty")
    return key


@dataclass
class Call:
    """One model call, for the audit and the metrics."""

    answer_id: str
    path: str
    turn: int | None  # None for count_tokens
    status: int
    bytes: int
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0


class UsageMeter:
    """Token counts from a response body, fed as it streams: SSE `message_start` and
    `message_delta` events, or a JSON body's `usage`. Input counts include cache reads and
    writes, which the model also processed."""

    def __init__(self, content_type: str) -> None:
        self.sse = content_type.split(";")[0].strip().lower() == "text/event-stream"
        self.buf = bytearray()
        self.input_tokens = 0
        self.output_tokens = 0

    def feed(self, data: bytes) -> None:
        if not self.sse:
            if len(self.buf) < MAX_METER_BYTES:
                self.buf += data[: MAX_METER_BYTES - len(self.buf)]
            return
        self.buf += data
        while b"\n" in self.buf:
            line, _, rest = bytes(self.buf).partition(b"\n")
            self.buf = bytearray(rest)
            if line.startswith(b"data:"):
                with contextlib.suppress(ValueError):
                    self._event(json.loads(line[5:]))

    def close(self) -> None:
        if not self.sse:
            with contextlib.suppress(ValueError):
                body = json.loads(self.buf)
                if isinstance(body, dict):
                    self._usage(body.get("usage"), output=True)

    def _event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        if event.get("type") == "message_start" and isinstance(event.get("message"), dict):
            self._usage(event["message"].get("usage"), output=False)
        elif event.get("type") == "message_delta":
            self._usage(event.get("usage"), output=True)

    def _usage(self, usage: Any, *, output: bool) -> None:
        if not isinstance(usage, dict):
            return
        n = sum(
            v
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            if isinstance(v := usage.get(k), int)
        )
        self.input_tokens = max(self.input_tokens, n)
        out = usage.get("output_tokens")
        if output and isinstance(out, int):
            self.output_tokens = max(self.output_tokens, out)  # cumulative in a stream


class Dechunker:
    """`Transfer-Encoding: chunked`, decoded as it streams, for the meter only: the client
    gets the upstream's bytes as they were."""

    def __init__(self) -> None:
        self.buf = b""
        self.left = 0  # bytes of chunk data still to come, then its CRLF
        self.done = False

    def feed(self, data: bytes) -> bytes:
        self.buf += data
        out = bytearray()
        while not self.done:
            if self.left:
                take = self.buf[: self.left]
                out += take
                self.left -= len(take)
                self.buf = self.buf[len(take) :]
                if self.left:
                    break
                self.buf = self.buf.removeprefix(b"\r\n")
                continue
            line, sep, rest = self.buf.partition(b"\r\n")
            if not sep:
                break
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                self.done = True
                break
            self.buf = rest
            if size == 0:
                self.done = True
            self.left = size
        return bytes(out)


def _error(status: int, kind: str, message: str, *, retry: bool = True) -> bytes:
    body = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
    reason = {400: "Bad Request", 404: "Not Found", 411: "Length Required"}.get(status, "Error")
    head = [
        f"HTTP/1.1 {status} {reason}",
        "content-type: application/json",
        f"content-length: {len(body)}",
        "connection: close",
    ]
    if not retry:
        head.append("x-should-retry: false")
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


class ModelProxy:
    """One server's model socket. `on_call` gets each call after it ends."""

    def __init__(
        self,
        *,
        upstream: Upstream,
        model: str,
        key: Callable[[], str | None],
        max_turns: Callable[[], int],
        on_call: Callable[[Call], None],
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.upstream = upstream
        self.model = model
        self.key = key
        self.max_turns = max_turns
        self.on_call = on_call
        self.ssl_context = ssl_context
        self.turns: dict[str, int] = {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        start = time.monotonic()
        try:
            await self._serve(reader, writer, start)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, start: float
    ) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        if len(head) > MAX_HEAD_BYTES:
            writer.write(_error(400, "invalid_request_error", "request head too large"))
            return
        lines = head.decode("latin-1").split("\r\n")
        method, _, rest = lines[0].partition(" ")
        target = rest.rpartition(" ")[0]
        m = PATH_RE.fullmatch(target)
        if method != "POST" or m is None:
            writer.write(_error(404, "not_found_error", "not found"))
            return
        answer_id, path, query = m.group(1), m.group(2), m.group(3)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        if "transfer-encoding" in headers or "content-length" not in headers:
            writer.write(_error(411, "invalid_request_error", "a Content-Length is required"))
            return
        try:
            length = int(headers["content-length"])
        except ValueError:
            writer.write(_error(400, "invalid_request_error", "bad Content-Length"))
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            writer.write(_error(413, "request_too_large", "request body too large"))
            return
        try:
            body = json.loads(await reader.readexactly(length))
        except ValueError:
            body = None
        if not isinstance(body, dict):
            writer.write(_error(400, "invalid_request_error", "the body must be a JSON object"))
            return
        body["model"] = self.model

        turn: int | None = None
        if path == "/v1/messages":
            turn = self.turns.get(answer_id, 0) + 1
            if turn > self.max_turns():
                writer.write(
                    _error(429, "rate_limit_error", "turn limit reached", retry=False)
                )
                self.on_call(Call(answer_id, path, turn, 429, 0, _ms(start)))
                return
            self.turns[answer_id] = turn
        key = self.key()
        if key is None:
            writer.write(_error(503, "api_error", "the broker has no model key", retry=False))
            self.on_call(Call(answer_id, path, turn, 503, 0, _ms(start)))
            return
        call = await self._forward(writer, path, query, headers, body, key, answer_id, turn)
        call.latency_ms = _ms(start)
        self.on_call(call)

    async def _forward(
        self,
        writer: asyncio.StreamWriter,
        path: str,
        query: str | None,
        headers: dict[str, str],
        body: dict[str, Any],
        key: str,
        answer_id: str,
        turn: int | None,
    ) -> Call:
        up = self.upstream
        payload = json.dumps(body).encode()
        target = up.path + path + (f"?{query}" if query else "")
        out = [f"POST {target} HTTP/1.1", f"host: {up.host}"]
        out += [f"{k}: {v}" for k, v in headers.items() if k in PASS_HEADERS]
        out += [
            f"x-api-key: {key}",
            f"content-length: {len(payload)}",
            "accept-encoding: identity",
            "connection: close",
        ]
        call = Call(answer_id, path, turn, 502, 0, 0)
        try:
            ctx = self.ssl_context
            if up.scheme == "https" and ctx is None:
                ctx = ssl.create_default_context()
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    up.host,
                    up.port,
                    ssl=ctx if up.scheme == "https" else None,
                    server_hostname=up.host if up.scheme == "https" else None,
                ),
                CONNECT_TIMEOUT_S,
            )
        except (OSError, TimeoutError):
            writer.write(_error(502, "api_error", "the model endpoint is unreachable"))
            return call
        try:
            up_writer.write(("\r\n".join(out) + "\r\n\r\n").encode() + payload)
            await up_writer.drain()
            await asyncio.wait_for(self._relay(up_reader, writer, call), RESPONSE_TIMEOUT_S)
        finally:
            up_writer.close()
            with contextlib.suppress(OSError, ssl.SSLError):
                await up_writer.wait_closed()
        return call

    async def _relay(
        self, up: asyncio.StreamReader, writer: asyncio.StreamWriter, call: Call
    ) -> None:
        head = await up.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        with contextlib.suppress(IndexError, ValueError):
            call.status = int(lines[0].split(" ")[1])
        rheaders = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in lines[1:] if line)
        }
        meter = UsageMeter(rheaders.get("content-type", ""))
        chunked = "chunked" in rheaders.get("transfer-encoding", "").lower()
        dechunk = Dechunker() if chunked else None
        writer.write(head)
        call.bytes = 0
        while data := await up.read(65536):
            writer.write(data)
            await writer.drain()
            call.bytes += len(data)
            meter.feed(dechunk.feed(data) if dechunk else data)
        meter.close()
        call.input_tokens, call.output_tokens = meter.input_tokens, meter.output_tokens


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
