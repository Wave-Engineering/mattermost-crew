"""The broker's model proxy (#141; D-111): the kiosk's only way to its model.

One HTTP/1.1 request per connection, on `<server>.model.sock`. Only
`POST /a/<answer_id>/v1/messages` and `POST /a/<answer_id>/v1/messages/count_tokens` are
served; anything else is 404. The proxy:

- reads the request body whole (`Content-Length`, at most `MAX_REQUEST_BYTES`) and forces its
  `model` to the pinned model;
- drops every credential header the caller sent, and adds the broker's own key as `x-api-key`;
- serves only an answer its `Budget` has admitted, which for the broker means one its ledger
  holds (a hello on the op socket made it): anything else is a 403, spending nothing. The kiosk
  names its own answer ids, so this is what bounds them: by the ledger's caps on answers, not
  by whatever a kiosk invents (#142);
- spends each `/v1/messages` call from the answer's turns. The one after `max_turns` is refused
  with a 429, an Anthropic-shaped `turn limit reached` error and `x-should-retry: false`, so
  the client answers with what it has instead of backing off and retrying;
- spends each `count_tokens` call from the answer's counts, apart from its turns, and refuses
  the one after `max_turns` the same way (`count_tokens limit reached`): every research server
  shares the broker's one key, so no call to it is unbounded;
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
import logging
import re
import ssl
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-5-5"
# Claude Code's own aliases: with no gateway, the API would get one as a model id (crewkiosk-run).
CLAUDE_ALIASES = frozenset({"default", "best", "fable", "sonnet", "opus", "haiku", "opusplan"})
MAX_REQUEST_BYTES = 1024 * 1024
MAX_HEAD_BYTES = 16 * 1024
LINGER_BYTES = 8 * MAX_REQUEST_BYTES  # the most of a refused body read and dropped
# The part of a response body kept to read `usage` from, when it isn't an event stream.
MAX_METER_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT_S = 30
RESPONSE_TIMEOUT_S = 300
REQUEST_TIMEOUT_S = 30  # to read a request's head and body
MAX_CONNECTIONS = 64  # per server's model socket
MAX_PER_ANSWER = 4  # of those, at once, for one answer
HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # an RFC 9110 token
HEADER_VALUE_RE = re.compile(r"[\x20-\x7e\t]*")
PATH_RE = re.compile(
    r"^/a/([0-9a-f]{32})(/v1/messages(?:/count_tokens)?)(?:\?([A-Za-z0-9=&_.-]*))?$"
)
# Request headers passed through. Everything else, every credential among them, is dropped.
PASS_HEADERS = frozenset({"anthropic-version", "anthropic-beta", "content-type", "accept"})


class ConfError(Exception):
    pass


Spend = Literal["turns", "counts"]


class Budget(Protocol):
    """One server's answers, as the proxy may serve them: the broker's ledger (`broker.SiteBudget`).
    The tables are its, so they live and go with its answer records, never the proxy's."""

    def admitted(self, answer_id: str) -> bool:
        """Whether `answer_id` may be served at all; asking keeps an admitted one live."""
        ...

    def spend(self, answer_id: str, kind: Spend) -> tuple[int, bool]:
        """The answer's next call of `kind`, and whether `max_turns` allows it: spent if so."""
        ...


class Slots:
    """One socket's connections: a cap on all of them, a smaller one per answer, so one
    answer can't hold every slot and lock the others out, and the open writers, so a stop
    can close them (on 3.12 a server's wait_closed() waits for every connection)."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.total = 0
        self.per: dict[str, int] = {}
        self.clients: set[asyncio.StreamWriter] = set()
        self.saturated = False
        self.closed = False

    def admit(self, writer: asyncio.StreamWriter, limit: int) -> bool:
        if self.closed:  # accepted just before the stop, its handler late: never admitted
            return False
        if self.total >= limit:
            if not self.saturated:  # once per saturation, never once per refusal
                log.warning("%s: %d connections, refusing more", self.label, self.total)
                self.saturated = True
            return False
        self.saturated = False
        self.total += 1
        self.clients.add(writer)
        return True

    def bind(self, answer_id: str, limit: int) -> bool:
        if self.per.get(answer_id, 0) >= limit:
            return False
        self.per[answer_id] = self.per.get(answer_id, 0) + 1
        return True

    def unbind(self, answer_id: str) -> None:
        self.per[answer_id] -= 1
        if not self.per[answer_id]:
            del self.per[answer_id]

    def release(self, writer: asyncio.StreamWriter) -> None:
        self.total -= 1
        self.clients.discard(writer)

    def close_all(self) -> None:
        self.closed = True
        for writer in list(self.clients):
            writer.close()


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

    @property
    def authority(self) -> str:
        """The Host header's value: the port only when it isn't the scheme's own."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"


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
    forwarded: bool = True  # False when the broker refused it: the turn cap, or no key
    complete: bool = False  # the whole response reached the client
    # False for an answer the budget never admitted (a 403): the ledger makes it no record.
    admitted: bool = True


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
                with contextlib.suppress(ValueError, RecursionError):
                    self._event(json.loads(line[5:]))

    def close(self) -> None:
        if not self.sse:
            with contextlib.suppress(ValueError, RecursionError):
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
    reason = {
        400: "Bad Request",
        404: "Not Found",
        411: "Length Required",
        413: "Content Too Large",
        429: "Too Many Requests",
        503: "Service Unavailable",
    }.get(status, "Error")
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
        budget: Budget,
        on_call: Callable[[Call], None],
        ssl_context: ssl.SSLContext | None = None,
        label: str = "model proxy",
    ) -> None:
        self.upstream = upstream
        self.model = model
        self.key = key
        self.budget = budget
        self.on_call = on_call
        self.ssl_context = ssl_context
        self.slots = Slots(label)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        start = time.monotonic()
        try:
            if not self.slots.admit(writer, MAX_CONNECTIONS):
                writer.write(_error(503, "overloaded_error", "too many connections"))
                return
            try:
                await self._serve(reader, writer, start)
            finally:
                self.slots.release(writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, start: float
    ) -> None:
        # A connection that sends nothing, or half a request, is closed, never held.
        request = await asyncio.wait_for(self._read(reader, writer), REQUEST_TIMEOUT_S)
        if request is None:
            return
        answer_id, path, query, headers, body = request
        unspent = None if path != "/v1/messages" else 0  # refused before any turn was counted
        # Admission first, before the slot cap or anything else that can refuse: each later
        # refusal reaches the ledger as an admitted call, and the ledger files that under the
        # answer's record, making one if there is none. Checked after the slot cap, an id no
        # hello admitted could mint its own record with a refused call, and be admitted by it.
        if not self.budget.admitted(answer_id):
            writer.write(_error(403, "permission_error", "no such answer", retry=False))
            call = Call(answer_id, path, unspent, 403, 0, _ms(start), forwarded=False)
            call.admitted = False
            self.on_call(call)
            return
        if not self.slots.bind(answer_id, MAX_PER_ANSWER):
            writer.write(_error(429, "rate_limit_error", "too many calls at once for this answer"))
            self.on_call(Call(answer_id, path, unspent, 429, 0, _ms(start), forwarded=False))
            return
        try:
            await self._call(writer, answer_id, path, query, headers, body, start)
        finally:
            self.slots.unbind(answer_id)

    async def _call(
        self,
        writer: asyncio.StreamWriter,
        answer_id: str,
        path: str,
        query: str | None,
        headers: dict[str, str],
        body: dict[str, Any],
        start: float,
    ) -> None:
        body["model"] = self.model

        # The key first: a call the broker can't make spends none of the answer's turns.
        key = self.key()
        if key is None:
            writer.write(_error(503, "api_error", "the broker has no model key", retry=False))
            unspent = None if path != "/v1/messages" else 0  # refused before any turn was counted
            self.on_call(Call(answer_id, path, unspent, 503, 0, _ms(start), forwarded=False))
            return
        turn: int | None = None
        if path == "/v1/messages":
            turn, allowed = self.budget.spend(answer_id, "turns")
            limit = "turn limit reached"
        else:
            _count, allowed = self.budget.spend(answer_id, "counts")
            limit = "count_tokens limit reached"
        if not allowed:
            writer.write(_error(429, "rate_limit_error", limit, retry=False))
            self.on_call(Call(answer_id, path, turn, 429, 0, _ms(start), forwarded=False))
            return
        call = await self._forward(writer, path, query, headers, body, key, answer_id, turn)
        call.latency_ms = _ms(start)
        self.on_call(call)

    async def _read(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[str, str, str | None, dict[str, str], dict[str, Any]] | None:
        """The request, or None once an error is written. Every header line is checked
        whole: a bare LF or other control character in one would let a value smuggle a
        second request upstream, with the broker's key on it."""
        head = await reader.readuntil(b"\r\n\r\n")
        if len(head) > MAX_HEAD_BYTES:
            writer.write(_error(400, "invalid_request_error", "request head too large"))
            return None
        lines = head.decode("latin-1").split("\r\n")
        method, _, rest = lines[0].partition(" ")
        target = rest.rpartition(" ")[0]
        m = PATH_RE.fullmatch(target)
        if method != "POST" or m is None:
            writer.write(_error(404, "not_found_error", "not found"))
            return None
        answer_id, path, query = m.group(1), m.group(2), m.group(3)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if (
                not sep
                or not HEADER_NAME_RE.fullmatch(name)
                or not HEADER_VALUE_RE.fullmatch(value)
            ):
                writer.write(_error(400, "invalid_request_error", "malformed header"))
                return None
            headers[name.lower()] = value.strip(" \t")
        if "transfer-encoding" in headers or "content-length" not in headers:
            writer.write(_error(411, "invalid_request_error", "a Content-Length is required"))
            return None
        try:
            length = int(headers["content-length"])
        except ValueError:
            writer.write(_error(400, "invalid_request_error", "bad Content-Length"))
            return None
        if length < 0 or length > MAX_REQUEST_BYTES:
            writer.write(_error(413, "request_too_large", "request body too large"))
            await writer.drain()
            # A close with the body unread resets the peer, and the reset can take the 413
            # with it: read and drop a bounded amount first (a lingering close).
            left = min(max(length, 0), LINGER_BYTES)
            while left > 0 and (chunk := await reader.read(min(left, 65536))):
                left -= len(chunk)
            return None
        try:
            body = json.loads(await reader.readexactly(length))
        except (ValueError, RecursionError):  # RecursionError: nested past the parser's depth
            body = None
        if not isinstance(body, dict):
            writer.write(_error(400, "invalid_request_error", "the body must be a JSON object"))
            return None
        return answer_id, path, query, headers, body

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
        out = [f"POST {target} HTTP/1.1", f"host: {up.authority}"]
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
        # Once the request is sent the call is the broker's to account for, however it ends:
        # a client that hangs up mid-stream must not hide a call it was billed for.
        try:
            up_writer.write(("\r\n".join(out) + "\r\n\r\n").encode() + payload)
            await up_writer.drain()
            await asyncio.wait_for(self._relay(up_reader, writer, call), RESPONSE_TIMEOUT_S)
        except TimeoutError:
            call.status = 504 if call.status == 502 else call.status
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # the status the upstream gave, if any; `complete` stays False
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
        try:
            while data := await up.read(65536):
                call.bytes += len(data)
                meter.feed(dechunk.feed(data) if dechunk else data)
                writer.write(data)
                await writer.drain()
            call.complete = True
        finally:
            meter.close()
            call.input_tokens, call.output_tokens = meter.input_tokens, meter.output_tokens


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
