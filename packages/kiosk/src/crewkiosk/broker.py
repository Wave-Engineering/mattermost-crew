"""`crewkiosk-broker`: the research kiosk's only door (#141; D-111, D-112).

A long-running service, as `crewkiosk-broker`. It alone holds the corpus read access and the
broker's model key. For each research server, one manifest in `/var/lib/crewchat-pool` (#139),
read at start and on SIGHUP, it serves two sockets under `/run/crewkiosk-broker`:

- `<server>.sock` (0660, group `crewkiosk`): newline-delimited JSON-RPC 2.0, the minimal MCP
  server subset (`initialize`, `tools/list`, `tools/call`, `ping`). The first message must be
  the notification `crewkiosk/hello` with `answer_id` (32 hex) and `post_id`; anything else
  closes the connection. A notification gets no reply, so the hello never reaches the MCP
  client through the relay. The server scope is the socket a connection arrived on, never a
  parameter. `tools/list` offers exactly the three ops in `crewkiosk.ops`.
- `<server>.model.sock` (same mode): the model proxy, `crewkiosk.proxy`, with the turn cap
  from the manifest's `max_turns`.

And one for the daemon: `/run/crewkiosk-broker-control/control.sock`, which
`packaging/crewkiosk-broker-control.socket` creates 0660 `crewkiosk-broker:crewchat` and hands
over (socket activation): the broker can't give a socket the daemon's group without joining
it. It has a directory of its own because the service's `RuntimeDirectory=` chowns its
directory, recursively, at every start, and would take the socket's group with it. Whatever
the file mode, a peer whose uid (`SO_PEERCRED`) isn't `crewchat`'s is closed before a byte is
read. One JSON line in, one out: `{"op": "audit", "answer_id"}` returns that answer's audit
lines and forgets them; `{"op": "metrics"}` returns each server's daily summary.

**Audit:** one line per op served and per model call proxied, `{ts, server, answer_id,
post_id, op, args, bytes, latency_ms, ok}`, written by the broker from what it itself relayed,
never from anything the runner or the kiosk reports. Held in memory per answer (200 lines,
dropped 15 minutes after the answer's last line) and appended to
`/var/lib/crewkiosk-broker/audit/<server>.jsonl` (0600). An answer gets MAX_OPS_PER_ANSWER ops;
each one after is refused, and audited as refused. A server gets MAX_ANSWERS_PER_DAY answers a
UTC day: a hello past that is refused and audited, and makes no record, so the ledger's memory
is bounded whoever mints the answer ids.

**Metrics:** per answer, the ops, model calls, input and output tokens and wall time (hello to
last activity), rolled up per server and UTC day as running totals, with op latency p50/p95
over the day's latest LATENCY_SAMPLE ops. The summary is computed when asked for, never per
line. Its copy in `/var/lib/crewkiosk-broker/metrics/<server>-<date>.json` is written at most
every METRICS_WRITE_S, and on stop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import grp
import json
import logging
import os
import pwd
import re
import signal
import socket
import struct
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crewkiosk import ops, proxy
from crewkiosk.corpus import CORPUS_ROOT, POOL_DIR, ManifestError, load_manifest

log = logging.getLogger(__name__)

RUNTIME_DIR = Path("/run/crewkiosk-broker")
# Made by crewkiosk-broker-control.socket, never the service: see the module docstring.
CONTROL_SOCKET = Path("/run/crewkiosk-broker-control/control.sock")
STATE_DIR = Path("/var/lib/crewkiosk-broker")
MODEL_CONF = Path("/etc/crewkiosk/model.conf")
# The broker's own model key (babelfish, 2026-10-30: two keys). Never the runner's
# model.credential, which the broker's unit can't even reach.
MODEL_KEY = Path("/etc/crewkiosk/broker-model.credential")
SOCKET_GROUP = "crewkiosk"
CONTROL_USER = "crewchat"
SOCKET_MODE = 0o660
AUDIT_MODE = 0o600
AUDIT_LINES = 200
AUDIT_TTL_S = 15 * 60
MAX_LINE_BYTES = 1024 * 1024
ANSWER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
POST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MCP_PROTOCOL = "2025-06-18"
VERSION = "0.2.6"
CONTROL_TIMEOUT_S = 5
HELLO_TIMEOUT_S = 10
MAX_CONNECTIONS = 64  # per server's op socket
MAX_PER_ANSWER = 4  # of those, for one answer
OP_IDLE_S = 10 * 60
# The most lines one audit() returns, across servers: the daemon's reply limit holds them.
TAKE_MAX = 1000
# An audit line's args: the op's own arguments, strings cut short, the whole capped, so 200
# lines always fit the daemon's reply limit and a refused call can't bloat the log.
AUDIT_STR_MAX = 256
AUDIT_ARGS_MAX = 2048
# The kiosk is untrusted: an answer's ops are capped, so a loop of them ends, and a server's
# answers a day are capped, so a client minting answer ids can't grow the ledger without end.
MAX_OPS_PER_ANSWER = 200
MAX_ANSWERS_PER_DAY = 10_000
# Expired answers are swept at most this often, never on every line.
EXPIRE_EVERY_S = 30
# Ops run in threads, at most this many at once across every server: rg is the heavy one.
OPS_AT_ONCE = 4
# A day's op latency p50/p95 is over its latest ops, never all of them.
LATENCY_SAMPLE = 1024
METRICS_WRITE_S = 60
# sun_path is 108 bytes on Linux, its NUL included.
SUN_PATH_MAX = 107


def jsonl(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def percentile(values: list[int] | list[float], p: float) -> float | None:
    """The nearest-rank percentile, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-p * len(ordered) // 100))))
    return float(ordered[rank - 1])


# -- audit and metrics ------------------------------------------------------------------------
@dataclass
class Answer:
    """One answer's activity on one server."""

    post_id: str | None
    started: float
    last: float
    ops: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    lines: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=AUDIT_LINES))


@dataclass
class Day:
    """One server's UTC day, as running totals: an audit line costs the same at 23:59 as at
    00:01, however many came before it."""

    date: str  # UTC, as the ledger keys it: a line is written to this day's file, never today's
    answers: dict[str, Answer] = field(default_factory=dict)
    ops: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    op_latency_ms: deque[int] = field(default_factory=lambda: deque(maxlen=LATENCY_SAMPLE))
    dirty: bool = False  # changed since its metrics file was written
    written: float | None = None
    full: bool = False  # reached MAX_ANSWERS_PER_DAY; logged once


class Ledger:
    """The audit lines and the metrics, for every server, kept by the broker alone."""

    def __init__(self, state_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = state_dir
        self.clock = clock
        self.answers: dict[tuple[str, str], Answer] = {}
        # (server, UTC date) -> the day, which keeps its answers for the rollup after their
        # lines go.
        self.days: dict[tuple[str, str], Day] = {}
        self.swept = float("-inf")

    def _answer(
        self, server: str, answer_id: str, post_id: str | None
    ) -> tuple[Day, Answer | None]:
        """The day and the answer's record, made if new. None for the answer once the server's
        day holds MAX_ANSWERS_PER_DAY: past that, a new answer gets no record."""
        now = self.clock()
        if now - self.swept >= EXPIRE_EVERY_S:
            self._expire(now)
        date = self._date(now)
        day = self.days.get((server, date))
        if day is None:
            day = self.days[(server, date)] = Day(date)
        key = (server, answer_id)
        a = self.answers.get(key)
        if a is None:
            # An answer back after going quiet resumes today's record, never replaces it.
            a = day.answers.get(answer_id)
            if a is None:
                if len(day.answers) >= MAX_ANSWERS_PER_DAY:
                    if not day.full:
                        day.full = True
                        log.warning(
                            "broker %s: %d answers today: no more until midnight UTC",
                            server,
                            MAX_ANSWERS_PER_DAY,
                        )
                    return day, None
                a = Answer(post_id, now, now)
                day.dirty = True
            self.answers[key] = a
        # An answer still open across midnight joins the new day too, with its counts so far:
        # that day's per-answer spreads include them, though its totals don't (#142).
        day.answers.setdefault(answer_id, a)
        if post_id is not None and a.post_id is None:
            a.post_id = post_id
        a.last = now
        return day, a

    def hello(self, server: str, answer_id: str, post_id: str) -> bool:
        """Records the answer, or, past MAX_ANSWERS_PER_DAY, audits the refused hello (on disk
        only: it makes no record) and returns False."""
        day, a = self._answer(server, answer_id, post_id)
        if a is None:
            self._line(server, answer_id, post_id, day, None, "hello", None, 0, 0, False)
        return a is not None

    def ops_used(self, server: str, answer_id: str) -> int:
        a = self.answers.get((server, answer_id))
        if a is None:
            day = self.days.get((server, self._date(self.clock())))
            a = day.answers.get(answer_id) if day is not None else None
        return a.ops if a is not None else 0

    def op(
        self,
        server: str,
        answer_id: str,
        post_id: str,
        name: str,
        args: Any,
        nbytes: int,
        latency_ms: int,
        ok: bool,
    ) -> None:
        day, a = self._answer(server, answer_id, post_id)
        if a is not None:
            a.ops += 1
        day.ops += 1
        day.op_latency_ms.append(latency_ms)
        self._line(server, answer_id, post_id, day, a, name, args, nbytes, latency_ms, ok)

    def model(self, server: str, call: proxy.Call) -> None:
        # Past the day's answer cap a call still counts in the day and reaches the audit
        # file; only its per-answer record is skipped.
        day, a = self._answer(server, call.answer_id, None)
        if call.turn is not None and call.forwarded:
            day.model_calls += 1
            if a is not None:
                a.model_calls += 1
        day.input_tokens += call.input_tokens
        day.output_tokens += call.output_tokens
        if a is not None:
            a.input_tokens += call.input_tokens
            a.output_tokens += call.output_tokens
        args = {"path": call.path, "turn": call.turn, "status": call.status}
        args |= {"input_tokens": call.input_tokens, "output_tokens": call.output_tokens}
        args["complete"] = call.complete
        ok = 200 <= call.status < 300 and call.complete
        post_id = a.post_id if a is not None else None
        self._line(
            server, call.answer_id, post_id, day, a, "model", args, call.bytes, call.latency_ms, ok
        )

    def take(self, answer_id: str) -> list[dict[str, Any]]:
        """Every server's audit lines for `answer_id`, and forget them."""
        self._expire(self.clock())
        lines: list[dict[str, Any]] = []
        for (_server, aid), a in list(self.answers.items()):
            if aid == answer_id:
                lines.extend(a.lines)
                a.lines.clear()
        return sorted(lines, key=lambda line: line["ts"])[-TAKE_MAX:]

    def metrics(self) -> dict[str, dict[str, Any]]:
        """Each server's summary for today (UTC)."""
        today = self._date(self.clock())
        out: dict[str, dict[str, Any]] = {}
        for (server, date), day in self.days.items():
            if date == today:
                out[server] = summary(date, day)
        return out

    def flush(self) -> None:
        """Write every day's metrics file that is behind: on a timer, and on stop."""
        for (server, date), day in list(self.days.items()):
            if day.dirty:
                self._write_metrics(server, date, day)

    def close(self) -> None:
        """On stop: every answer's lines dropped, whatever the sweep's last run, and every
        metrics file brought up to date."""
        for a in self.answers.values():
            a.lines.clear()
        self.answers.clear()
        self.flush()

    def _line(
        self,
        server: str,
        answer_id: str,
        post_id: str | None,
        day: Day,
        a: Answer | None,
        name: str,
        args: Any,
        nbytes: int,
        latency_ms: int,
        ok: bool,
    ) -> None:
        """One audit line: kept in memory with the answer's record, if it has one, and always
        appended to the server's audit file."""
        line = {
            "ts": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            "server": server,
            "answer_id": answer_id,
            "post_id": a.post_id if a is not None else post_id,
            "op": name,
            "args": args,
            "bytes": nbytes,
            "latency_ms": latency_ms,
            "ok": ok,
        }
        if a is not None:
            a.lines.append(line)
        day.dirty = True
        try:
            self._append(self.state_dir / "audit" / f"{server}.jsonl", jsonl(line))
        except OSError as exc:
            log.error("broker %s: cannot record the audit: %s", server, exc.strerror)
        now = self.clock()
        if day.written is None or now - day.written >= METRICS_WRITE_S:
            self._write_metrics(server, day.date, day)

    def _append(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path, flags, AUDIT_MODE)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def _write_metrics(self, server: str, date: str, day: Day) -> None:
        """The day's summary to its file, replaced whole. A failure is logged and the day
        stays dirty, so the next line or flush tries again."""
        day.written, day.dirty = self.clock(), False
        path = self.state_dir / "metrics" / f"{server}-{date}.json"
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            try:
                os.fchmod(fd, AUDIT_MODE)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(jsonl(summary(date, day)))
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
        except OSError as exc:
            day.dirty = True
            log.error("broker %s: cannot record the metrics: %s", server, exc.strerror)

    def _expire(self, now: float) -> None:
        """The sweep: answers quiet past AUDIT_TTL_S, and past days. Run at most every
        EXPIRE_EVERY_S from a line, so an answer's lines can outlive AUDIT_TTL_S by that much,
        never more; take() and close() run it, or its effect, at once."""
        self.swept = now
        for key, a in list(self.answers.items()):
            if now - a.last > AUDIT_TTL_S:
                a.lines.clear()  # dropped after 15 minutes, as the audit promises
                del self.answers[key]  # its day keeps its counts for the rollup
        today = self._date(now)
        for key in [k for k in self.days if k[1] < today]:
            day = self.days.pop(key)
            if day.dirty:
                self._write_metrics(*key, day)  # the day's last minute still reaches its file

    @staticmethod
    def _date(ts: float) -> str:
        return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def summary(date: str, day: Day) -> dict[str, Any]:
    """One server's day: totals, op latency p50/p95 over the day's latest LATENCY_SAMPLE ops,
    and per-answer ops, model calls, tokens and wall time (hello to last activity) as p50/p95.
    Computed only when asked for, or when the metrics file is written."""
    per = list(day.answers.values())

    def spread(values: list[int] | list[float]) -> dict[str, float | None]:
        return {"p50": percentile(values, 50), "p95": percentile(values, 95)}

    return {
        "date": date,
        "answers": len(per),
        "ops": day.ops,
        "model_calls": day.model_calls,
        "input_tokens": day.input_tokens,
        "output_tokens": day.output_tokens,
        "op_latency_ms": spread(list(day.op_latency_ms)),
        "per_answer": {
            "ops": spread([a.ops for a in per]),
            "model_calls": spread([a.model_calls for a in per]),
            "tokens": spread([a.input_tokens + a.output_tokens for a in per]),
            "wall_s": spread([round(a.last - a.started, 3) for a in per]),
        },
    }


# -- the op socket ----------------------------------------------------------------------------
def rpc_result(rid: Any, result: Any) -> bytes:
    return jsonl({"jsonrpc": "2.0", "id": rid, "result": result})


def rpc_error(rid: Any, code: int, message: str) -> bytes:
    return jsonl({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


class OpServer:
    """One server's op socket: the MCP subset, after a hello."""

    def __init__(
        self,
        server: str,
        corpus: ops.Corpus,
        ledger: Ledger,
        threads: asyncio.Semaphore | None = None,
    ) -> None:
        self.server = server
        self.corpus = corpus
        self.ledger = ledger
        self.slots = proxy.Slots(f"broker {server} ops")
        # The broker's one semaphore, shared by every server: the default executor also
        # serves the proxy's getaddrinfo, so ops never fill it.
        self.threads = threads or asyncio.Semaphore(OPS_AT_ONCE)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if not self.slots.admit(writer, MAX_CONNECTIONS):
                return
            try:
                await self._serve(reader, writer)
            finally:
                self.slots.release(writer)
        except (
            OSError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ValueError,
            TimeoutError,
        ):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        first = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S)
        hello = _hello(first)
        if hello is None:
            log.warning("broker %s: a connection without a valid hello: closed", self.server)
            return
        answer_id, post_id = hello
        if not self.slots.bind(answer_id, MAX_PER_ANSWER):
            # Debug, not warning: a kiosk can reconnect in a loop, and saturation logs once.
            log.debug(
                "broker %s: answer %s holds %d sessions: one refused",
                self.server,
                answer_id,
                MAX_PER_ANSWER,
            )
            return
        try:
            if not self.ledger.hello(self.server, answer_id, post_id):
                return  # the server's answers for today are used up: audited, closed
            # Idle past OP_IDLE_S, longer than any real gap between an answer's calls: closed.
            while line := await asyncio.wait_for(reader.readline(), OP_IDLE_S):
                reply = await self._message(line, answer_id, post_id)
                if reply is not None:
                    writer.write(reply)
                    await writer.drain()
        finally:
            self.slots.unbind(answer_id)

    async def _message(self, line: bytes, answer_id: str, post_id: str) -> bytes | None:
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):  # RecursionError: nested past the parser's depth
            return rpc_error(None, -32700, "parse error")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return rpc_error(None, -32600, "invalid request")
        rid, method = msg.get("id"), msg.get("method")
        if "id" not in msg:
            return None  # a notification: notifications/initialized, cancellations
        params = msg.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            return rpc_result(
                rid,
                {
                    "protocolVersion": requested if isinstance(requested, str) else MCP_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "crewkiosk-broker", "version": VERSION},
                },
            )
        if method == "ping":
            return rpc_result(rid, {})
        if method == "tools/list":
            return rpc_result(rid, {"tools": ops.tool_list()})
        if method == "tools/call":
            return rpc_result(rid, await self._call(params, answer_id, post_id))
        return rpc_error(rid, -32601, f"method not found: {method}")

    async def _call(self, params: Any, answer_id: str, post_id: str) -> dict[str, Any]:
        start = time.monotonic()
        name = params.get("name") if isinstance(params, dict) else None
        args = params.get("arguments", {}) if isinstance(params, dict) else None
        shown = ops.printable(name)
        try:
            if self.ledger.ops_used(self.server, answer_id) >= MAX_OPS_PER_ANSWER:
                raise ops.ToolError(f"this answer has used all {MAX_OPS_PER_ANSWER} of its ops")
            async with self.threads:
                result = await asyncio.to_thread(self.corpus.call, str(name), args)
            text, ok = json.dumps(result), True
        except ops.ToolError as exc:
            text, ok = str(exc), False
            log.info("broker %s: %s refused: %s", self.server, shown, exc)
        latency = int((time.monotonic() - start) * 1000)
        self.ledger.op(
            self.server,
            answer_id,
            post_id,
            shown,
            audit_args(name, args),
            len(text.encode()),
            latency,
            ok,
        )
        return {"content": [{"type": "text", "text": text}], "isError": not ok}


def audit_args(name: Any, args: Any) -> dict[str, Any] | None:
    """What an audit line records of a call's arguments: the op's own keys, strings cut to
    AUDIT_STR_MAX, the names (only) of any others, and never more than AUDIT_ARGS_MAX."""
    if not isinstance(args, dict):
        return None
    fields = ops.TOOLS[name][1] if isinstance(name, str) and name in ops.TOOLS else {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key in fields:
            if isinstance(value, str):
                value = value[:AUDIT_STR_MAX]
            elif not isinstance(value, int | bool | None):
                value = type(value).__name__
            out[key] = value
    unknown = [ops.printable(k) for k in args if k not in fields]
    if unknown:
        out["unknown_keys"] = unknown[:10]
    if len(json.dumps(out)) > AUDIT_ARGS_MAX:
        return {"truncated": True}
    return out


def _hello(line: bytes) -> tuple[str, str] | None:
    try:
        msg = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(msg, dict) or msg.get("method") != "crewkiosk/hello" or "id" in msg:
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    answer_id, post_id = params.get("answer_id"), params.get("post_id")
    if not isinstance(answer_id, str) or not ANSWER_ID_RE.fullmatch(answer_id):
        return None
    if not isinstance(post_id, str) or not POST_ID_RE.fullmatch(post_id):
        return None
    return answer_id, post_id


# -- the control socket -----------------------------------------------------------------------
def peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return int(uid)


class Control:
    """The daemon's socket: audit and metrics, for the daemon's uid only."""

    def __init__(self, ledger: Ledger, allowed_uid: int) -> None:
        self.ledger = ledger
        self.allowed_uid = allowed_uid

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            uid = peer_uid(writer)
            if uid != self.allowed_uid:
                log.warning("broker: control connection from uid %s refused", uid)
                return  # closed before a byte is read
            line = await asyncio.wait_for(reader.readline(), CONTROL_TIMEOUT_S)
            writer.write(jsonl(self.answer(line)))
            await writer.drain()
        except (OSError, TimeoutError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def answer(self, line: bytes) -> dict[str, Any]:
        try:
            req = json.loads(line)
        except (ValueError, RecursionError):
            return {"ok": False, "error": "not JSON"}
        if not isinstance(req, dict):
            return {"ok": False, "error": "not an object"}
        if req.get("op") == "metrics":
            return {"ok": True, "metrics": self.ledger.metrics()}
        if req.get("op") == "audit":
            aid = req.get("answer_id")
            if not isinstance(aid, str) or not ANSWER_ID_RE.fullmatch(aid):
                return {"ok": False, "error": "answer_id must be 32 hex"}
            return {"ok": True, "lines": self.ledger.take(aid)}
        return {"ok": False, "error": "unknown op"}


# -- the service ------------------------------------------------------------------------------
@dataclass
class Site:
    """One research server's sockets."""

    corpus: ops.Corpus
    max_turns: int
    model: proxy.ModelProxy | None = None
    op: OpServer | None = None
    servers: list[asyncio.base_events.Server] = field(default_factory=list)


class Broker:
    def __init__(
        self,
        *,
        pool_dir: Path = POOL_DIR,
        corpus_root: Path = CORPUS_ROOT,
        runtime_dir: Path = RUNTIME_DIR,
        control_path: Path = CONTROL_SOCKET,
        state_dir: Path = STATE_DIR,
        model_conf: Path = MODEL_CONF,
        model_key: Path = MODEL_KEY,
        socket_group: str | None = SOCKET_GROUP,
        control_uid: int | None = None,
        upstream: proxy.Upstream | None = None,
        rg: str = ops.RG,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.pool_dir = pool_dir
        self.corpus_root = corpus_root
        self.runtime_dir = runtime_dir
        self.control_path = control_path
        self.model_conf = model_conf
        self.model_key = model_key
        self.socket_group = socket_group
        self.control_uid = control_uid
        self.upstream_override = upstream
        self.rg = rg
        self.ledger = Ledger(state_dir, clock=clock)
        self.sites: dict[str, Site] = {}
        self.key: str | None = None
        self.conf = proxy.ModelConf(proxy.DEFAULT_BASE_URL, proxy.DEFAULT_MODEL)
        self.control: asyncio.base_events.Server | None = None
        # One reload at a time: a SIGHUP during a reload, or a stop, waits for it. A reload
        # that was waiting on a stop does nothing.
        self.lock = asyncio.Lock()
        self.stopped = False
        self.threads = asyncio.Semaphore(OPS_AT_ONCE)

    def load_model(self) -> None:
        """model.conf and the key, re-read on each reload. A bad key leaves the proxy
        answering 503, and the ops still served; a bad model.conf keeps the last one."""
        try:
            self.conf = proxy.parse_model_conf(self.model_conf)
        except proxy.ConfError as exc:
            log.error("broker: %s; keeping %s", exc, self.conf.base_url)
        try:
            self.key = proxy.parse_key(self.model_key)
        except proxy.ConfError as exc:
            self.key = None
            log.error("broker: %s: the model proxy refuses every call", exc)

    def manifests(self) -> dict[str, tuple[tuple[str, ...], int]]:
        found: dict[str, tuple[tuple[str, ...], int]] = {}
        try:
            names = sorted(n for n in os.listdir(self.pool_dir) if n.endswith(".json"))
        except OSError as exc:
            log.error("broker: cannot list %s: %s", self.pool_dir, exc.strerror)
            return {s: (site.corpus.repos, site.max_turns) for s, site in self.sites.items()}
        for name in names:
            try:
                m = load_manifest(self.pool_dir / name)
            except ManifestError as exc:
                log.error("broker: skipping a manifest: %s", exc)
                continue
            if m.server == "control" or "." in m.server:
                # Its sockets would be a development control.sock, or another server's
                # <server>.model.sock.
                log.error("broker: server id %r can't have sockets here: skipped", m.server)
                continue
            longest = self.runtime_dir / f"{m.server}.model.sock"
            if len(os.fsencode(longest)) > SUN_PATH_MAX:
                log.warning("broker: server id %r is too long for a socket path: skipped", m.server)
                continue
            found[m.server] = (m.repos, m.max_turns)
        return found

    async def reload(self) -> None:
        async with self.lock:
            if self.stopped:
                return
            self.load_model()
            wanted = self.manifests()
            for server in sorted(set(self.sites) - set(wanted)):
                try:
                    await self._close(server)
                except Exception:  # one server's trouble never stops the others' reload
                    log.exception("broker %s: closing its sockets failed", server)
                    continue
                log.info("broker %s: no manifest: its sockets are closed", server)
            for server, (repos, turns) in sorted(wanted.items()):
                site = self.sites.get(server)
                if site is not None:
                    site.corpus.repos, site.max_turns = repos, turns
                    if site.model is not None:
                        site.model.upstream = self._upstream()
                        site.model.model = self.conf.model
                    continue
                try:
                    await self._open(server, repos, turns)
                except Exception:  # its sockets are gone again; the others still open
                    log.exception("broker %s: opening its sockets failed", server)

    def _upstream(self) -> proxy.Upstream:
        return self.upstream_override or proxy.Upstream.parse(self.conf.base_url)

    async def _open(self, server: str, repos: tuple[str, ...], turns: int) -> None:
        corpus = ops.Corpus(server, repos, self.corpus_root, rg=self.rg)
        site = Site(corpus, turns)
        model = site.model = proxy.ModelProxy(
            upstream=self._upstream(),
            model=self.conf.model,
            key=lambda: self.key,
            max_turns=lambda: site.max_turns,
            on_call=lambda call: self.ledger.model(server, call),
            label=f"broker {server} model",
        )
        op = site.op = OpServer(server, corpus, self.ledger, self.threads)
        try:
            for path, handler in (
                (self.runtime_dir / f"{server}.sock", op.handle),
                (self.runtime_dir / f"{server}.model.sock", model.handle),
            ):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                srv = await asyncio.start_unix_server(handler, path=str(path), limit=MAX_LINE_BYTES)
                site.servers.append(srv)
                self._own(path)
        except BaseException:
            await self._shut(server, site)
            raise
        # Registered only once both sockets serve: a half-open site is never in sites.
        self.sites[server] = site
        log.info("broker %s: serving %d pooled repos", server, len(repos))

    async def _close(self, server: str) -> None:
        await self._shut(server, self.sites.pop(server))

    async def _shut(self, server: str, site: Site) -> None:
        for srv in site.servers:
            srv.close()
        # 3.12's wait_closed() waits for every connection: end the open sessions first.
        for slots in (site.op.slots if site.op else None, site.model.slots if site.model else None):
            if slots is not None:
                slots.close_all()
        for srv in site.servers:
            await srv.wait_closed()
        for suffix in (".sock", ".model.sock"):
            with contextlib.suppress(FileNotFoundError):
                (self.runtime_dir / f"{server}{suffix}").unlink()

    def _own(self, path: Path) -> None:
        if self.socket_group is not None:
            os.chown(path, -1, grp.getgrnam(self.socket_group).gr_gid)
        os.chmod(path, SOCKET_MODE)

    async def start_control(self, sock: socket.socket | None) -> None:
        """The control socket systemd handed over, or (in development) one made here."""
        uid = self.control_uid
        if uid is None:
            uid = pwd.getpwnam(CONTROL_USER).pw_uid
        control = Control(self.ledger, uid)
        if sock is not None:
            self.control = await asyncio.start_unix_server(control.handle, sock=sock)
            return
        path = self.control_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self.control = await asyncio.start_unix_server(control.handle, path=str(path))
        os.chmod(path, SOCKET_MODE)

    async def stop(self) -> None:
        async with self.lock:
            self.stopped = True
            for server in list(self.sites):
                try:
                    await self._close(server)
                except Exception:  # as in reload: one server never keeps the rest open
                    log.exception("broker %s: closing its sockets failed", server)
            if self.control is not None:
                self.control.close()
                await self.control.wait_closed()
            self.ledger.close()


def listened_socket() -> socket.socket | None:
    """The control socket from systemd (`LISTEN_FDS`, sd_listen_fds(3)), if it passed one."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    if int(os.environ.get("LISTEN_FDS", "0")) < 1:
        return None
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    return socket.socket(fileno=3)


async def flush_metrics(ledger: Ledger) -> None:
    """Each day's metrics file, brought up to date every METRICS_WRITE_S, so a quiet spell
    still reaches the disk."""
    while True:
        await asyncio.sleep(METRICS_WRITE_S)
        try:
            ledger.flush()
        except Exception:  # never the end of the metrics: the next round tries again
            log.exception("broker: the metrics flush failed")


async def serve(broker: Broker) -> None:
    await broker.reload()
    await broker.start_control(listened_socket())
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    # The loop holds tasks only weakly: these references keep each reload alive to its end.
    tasks: set[asyncio.Task[None]] = set()

    def spawn(coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGHUP, lambda: spawn(broker.reload()))
    flusher = asyncio.ensure_future(flush_metrics(broker.ledger))
    try:
        await stop.wait()
    finally:
        flusher.cancel()
        await broker.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crewkiosk-broker")
    parser.add_argument("--pool-dir", type=Path, default=POOL_DIR)
    parser.add_argument("--corpus-root", type=Path, default=CORPUS_ROOT)
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR)
    # Only when systemd hands over no socket, as in development.
    parser.add_argument("--control-socket", type=Path, default=CONTROL_SOCKET)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    broker = Broker(
        pool_dir=ns.pool_dir,
        corpus_root=ns.corpus_root,
        runtime_dir=ns.runtime_dir,
        control_path=ns.control_socket,
        state_dir=ns.state_dir,
    )
    asyncio.run(serve(broker))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
