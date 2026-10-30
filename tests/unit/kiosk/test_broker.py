"""#141: crewkiosk-broker's typed ops, model proxy, audit, metrics and control socket, and
crewkiosk-relay's byte pump."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from crewchat.kiosk_broker import KioskBrokerClient
from crewkiosk import broker, ops, proxy, relay
from crewkiosk.broker import Broker, Control, Ledger, OpServer, percentile
from crewkiosk.ops import Corpus, ToolError
from crewkiosk.proxy import Call, ConfError, ModelProxy, Upstream, parse_key, parse_model_conf

from tests.fakes.forge import write_manifest

SERVER = "dev"
OTHER = "other"
A, B, C = "forge.test/org/a", "forge.test/org/b", "forge.test/org/c"
UNBUILT = "forge.test/org/unbuilt"  # in the manifest, never published
AID = "0123456789abcdef0123456789abcdef"
AID2 = "fedcba9876543210fedcba9876543210"
POST = "post-1"
NOON = 1_793_361_600.0  # 2026-10-30T12:00:00Z
COMMIT = "a" * 40
KEY = "sk-broker-test-key"


class Tick:
    """A settable clock."""

    def __init__(self, t: float = NOON) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def sockdir() -> Iterator[Path]:
    """A short directory for Unix sockets: a pytest tmp_path can pass the 108-byte limit."""
    path = Path(tempfile.mkdtemp(prefix="kb-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def rg() -> str:
    path = shutil.which("rg")
    if path is None:
        if os.environ.get("CREWKIOSK_RG_REQUIRED") == "1":
            pytest.fail("CREWKIOSK_RG_REQUIRED=1, and there is no rg on PATH")
        pytest.skip("no rg on PATH (CI installs it: scripts/ci/rg-install.sh)")
    return path


def publish(root: Path, server: str, files: dict[str, str | bytes]) -> Path:
    """`<root>/<server>/current` as #140 leaves it, a link to a generation, holding `files`."""
    gen = root / server / "gen-1"
    for rel, body in files.items():
        path = gen / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body)
    gen.mkdir(parents=True, exist_ok=True)
    (root / server / "current").symlink_to("gen-1")
    return gen


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    publish(
        root,
        SERVER,
        {
            f"{A}/README.md": "hello needle\nsecond line\n",
            f"{A}/src/x.py": "x = 'needle'\n",
            f"{A}/docs/guide.md": "# guide\n",
            f"{B}/notes.txt": "a needle in b\n",
        },
    )
    publish(root, OTHER, {f"{C}/secret.txt": "a needle in c\n"})
    (root / "status").mkdir()
    status = {"repos": {A: {"state": "published", "commit": COMMIT}}}
    (root / "status" / f"{SERVER}.json").write_text(json.dumps(status))
    return root


@pytest.fixture
def corpus(root: Path) -> Corpus:
    return Corpus(SERVER, (A, B, UNBUILT), root)


# -- the op socket: hello, MCP ------------------------------------------------------------------
def hello_line(answer_id: str = AID, post_id: str = POST, **extra: Any) -> bytes:
    msg = {"jsonrpc": "2.0", "method": "crewkiosk/hello", **extra}
    msg["params"] = {"answer_id": answer_id, "post_id": post_id}
    return json.dumps(msg).encode() + b"\n"


def rpc(rid: int, method: str, params: dict[str, Any] | None = None) -> bytes:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode() + b"\n"


def with_server(
    sockdir: Path,
    handler: Callable[..., Awaitable[None]],
    scenario: Callable[[str], Awaitable[Any]],
) -> Any:
    """Run `scenario(path)` against `handler` served on a Unix socket."""

    async def main() -> Any:
        path = str(sockdir / "s.sock")
        server = await asyncio.start_unix_server(handler, path=path, limit=broker.MAX_LINE_BYTES)
        try:
            return await scenario(path)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


async def read_to_close(reader: asyncio.StreamReader) -> bytes:
    """Everything until the peer closes. A close with our bytes still unread is ECONNRESET
    on Linux, and is the same answer: closed."""
    data = b""
    with contextlib.suppress(ConnectionResetError):
        while chunk := await reader.read(65536):
            data += chunk
    return data


async def converse(path: str, *lines: bytes) -> list[dict[str, Any]]:
    """Send `lines`, half-close, and read every reply line until the broker closes."""
    reader, writer = await asyncio.open_unix_connection(path, limit=broker.MAX_LINE_BYTES)
    with contextlib.suppress(ConnectionError):
        writer.write(b"".join(lines))
        await writer.drain()
        writer.write_eof()
    out = [json.loads(line) for line in (await read_to_close(reader)).splitlines()]
    writer.close()
    return out


def op_server(corpus: Corpus, ledger: Ledger | None = None) -> OpServer:
    return OpServer(SERVER, corpus, ledger or Ledger(corpus.root / ".." / "state", clock=Tick()))


@pytest.mark.parametrize(
    "first",
    [
        rpc(1, "initialize", {}),  # no hello at all
        hello_line(answer_id="xyz"),
        hello_line(answer_id=AID.upper()),
        hello_line(answer_id=AID + "0"),
        hello_line(post_id=""),
        hello_line(post_id="a b"),
        hello_line(id=1),  # a request, not a notification
        b"not json\n",
        b"\n",
    ],
)
def test_hello_required(sockdir: Path, corpus: Corpus, first: bytes) -> None:
    """No hello, or a bad one, and the connection is closed: nothing after it is answered."""
    ledger = Ledger(sockdir / "state", clock=Tick())
    replies = with_server(
        sockdir,
        op_server(corpus, ledger).handle,
        lambda path: converse(path, first, rpc(2, "tools/list")),
    )
    assert replies == []
    assert ledger.answers == {}


def test_hello_then_mcp(sockdir: Path, corpus: Corpus) -> None:
    """A valid hello gets no reply, so no stray line reaches the MCP client, and opens the
    session."""
    replies = with_server(
        sockdir,
        op_server(corpus).handle,
        lambda path: converse(
            path,
            hello_line(),
            rpc(1, "initialize", {"protocolVersion": "2025-06-18"}),
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
            rpc(2, "ping"),
            rpc(3, "resources/list"),
        ),
    )
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    assert replies[1]["result"] == {}
    assert replies[2]["error"]["code"] == -32601


def test_tools_list_exact(sockdir: Path, corpus: Corpus) -> None:
    replies = with_server(
        sockdir,
        op_server(corpus).handle,
        lambda path: converse(path, hello_line(), rpc(1, "tools/list")),
    )
    tools = replies[0]["result"]["tools"]
    assert [t["name"] for t in tools] == ["corpus_search", "corpus_read", "corpus_list"]
    by_name = {t["name"]: t["inputSchema"] for t in tools}
    for schema in by_name.values():
        assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(by_name["corpus_search"]["properties"]) == {
        "query",
        "repo",
        "path_glob",
        "max_results",
    }
    assert by_name["corpus_search"]["required"] == ["query"]
    assert set(by_name["corpus_read"]["properties"]) == {"repo", "path", "offset", "limit"}
    assert by_name["corpus_read"]["required"] == ["repo", "path"]
    assert set(by_name["corpus_list"]["properties"]) == {"repo", "glob"}
    assert by_name["corpus_list"]["required"] == ["repo"]
    # Every string and every integer is bounded, as the schema says and as the op enforces.
    for schema in by_name.values():
        for prop in schema["properties"].values():
            assert ("maxLength" in prop) if prop["type"] == "string" else ("minimum" in prop)
    # No other op, and no other argument: not even one the schema could be stretched to take.
    with pytest.raises(ToolError, match="unknown tool"):
        corpus.call("shell", {"cmd": "id"})
    with pytest.raises(ToolError, match="unknown argument 'url'"):
        corpus.call("corpus_read", {"repo": A, "path": "README.md", "url": "http://x"})


def call(sockdir: Path, op: OpServer, name: str, args: Any) -> dict[str, Any]:
    """One tools/call through the socket: its result, `isError` and the payload."""
    replies = with_server(
        sockdir,
        op.handle,
        lambda path: converse(
            path, hello_line(), rpc(1, "tools/call", {"name": name, "arguments": args})
        ),
    )
    result: dict[str, Any] = replies[0]["result"]
    return result


# -- scope ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("corpus_search", {"query": "needle", "repo": C}),
        ("corpus_read", {"repo": C, "path": "secret.txt"}),
        ("corpus_list", {"repo": C}),
        ("corpus_list", {"repo": UNBUILT}),  # pooled, but nothing published
        ("corpus_read", {"repo": "forge.test/org/zzz", "path": "x"}),
        ("corpus_list", {"repo": f"{A}/../c"}),
    ],
)
def test_repo_outside_pool_refused(
    sockdir: Path, corpus: Corpus, name: str, args: dict[str, Any]
) -> None:
    """Each op refuses a repo outside this socket's server's pool, including one in another
    server's pool: the socket, not a parameter, is the scope."""
    ledger = Ledger(sockdir / "state", clock=Tick())
    result = call(sockdir, op_server(corpus, ledger), name, args)
    assert result["isError"] is True
    assert "/corpus/" not in result["content"][0]["text"]  # no host path in a refusal
    [line] = ledger.take(AID)
    assert line["op"] == name and line["ok"] is False and line["server"] == SERVER


def test_search_stays_in_its_pool(corpus: Corpus, rg: str) -> None:
    corpus.rg = rg
    found = corpus.call("corpus_search", {"query": "needle"})
    assert {(r["repo"], r["path"]) for r in found["results"]} == {
        (A, "README.md"),
        (A, "src/x.py"),
        (B, "notes.txt"),
    }
    assert {r["line"] for r in found["results"]} == {1}
    only_a = corpus.call("corpus_search", {"query": "needle", "repo": A, "path_glob": "*.py"})
    assert [(r["repo"], r["path"], r["text"]) for r in only_a["results"]] == [
        (A, "src/x.py", "x = 'needle'")
    ]
    # Fixed strings, not a regex, and the query never becomes a flag.
    assert corpus.call("corpus_search", {"query": "n.edle"})["results"] == []
    assert corpus.call("corpus_search", {"query": "--files"})["results"] == []


def test_read_and_list(corpus: Corpus) -> None:
    read = corpus.call("corpus_read", {"repo": A, "path": "README.md"})
    assert read["commit"] == COMMIT and read["total_lines"] == 2
    assert read["lines"] == [{"n": 0, "text": "hello needle"}, {"n": 1, "text": "second line"}]
    assert read["truncated"] is False
    part = corpus.call("corpus_read", {"repo": A, "path": "README.md", "offset": 1, "limit": 1})
    assert part["lines"] == [{"n": 1, "text": "second line"}]
    listed = corpus.call("corpus_list", {"repo": A})
    assert listed["paths"] == ["README.md", "docs/guide.md", "src/x.py"]
    assert listed["commit"] == COMMIT
    assert corpus.call("corpus_list", {"repo": A, "glob": "docs/*"})["paths"] == ["docs/guide.md"]
    assert corpus.call("corpus_list", {"repo": B})["commit"] is None  # the status doesn't say


@pytest.mark.parametrize(
    "path",
    ["../b/notes.txt", "src/../../b/notes.txt", "/etc/passwd", "a\0b", "", "..", "src/.."],
)
def test_path_traversal_refused(corpus: Corpus, path: str) -> None:
    with pytest.raises(ToolError):
        corpus.call("corpus_read", {"repo": A, "path": path})


def test_realpath_escape_refused(root: Path, corpus: Corpus) -> None:
    """The corpus holds no symlinks (#140); if one appeared, it still leads nowhere."""
    gen = root / SERVER / "gen-1"
    (gen / A / "escape").symlink_to(root / OTHER / "gen-1" / C / "secret.txt")
    (gen / A / "up").symlink_to(gen / B)
    for path in ("escape", "up/notes.txt"):
        with pytest.raises(ToolError, match="outside the repo"):
            corpus.call("corpus_read", {"repo": A, "path": path})
    # corpus_list never follows a link, nor lists one.
    assert "escape" not in corpus.call("corpus_list", {"repo": A})["paths"]
    # A pooled repo that is itself a link out of this server's corpus is not published here.
    (gen / "forge.test" / "org" / "linked").symlink_to(root / OTHER / "gen-1" / C)
    wider = Corpus(SERVER, (A, "forge.test/org/linked"), root)
    with pytest.raises(ToolError, match="not published"):
        wider.call("corpus_list", {"repo": "forge.test/org/linked"})


# -- bounds --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "args", "match"),
    [
        ("corpus_search", {"query": "x" * 201}, "longer than 200"),
        ("corpus_search", {"query": "x", "path_glob": "*" * 101}, "longer than 100"),
        ("corpus_search", {"query": "x", "max_results": 0}, "from 1 to 50"),
        ("corpus_search", {"query": "x", "max_results": 51}, "from 1 to 50"),
        ("corpus_search", {"query": "x", "max_results": True}, "integer"),
        ("corpus_search", {"query": "x", "max_results": "5"}, "integer"),
        ("corpus_search", {"query": 5}, "string"),
        ("corpus_search", {}, "query is required"),
        ("corpus_search", {"query": "a\nb"}, "one line"),
        ("corpus_search", {"query": "  "}, "one line"),
        ("corpus_read", {"repo": A, "path": "README.md", "limit": 0}, "from 1 to 2000"),
        ("corpus_read", {"repo": A, "path": "README.md", "limit": 2001}, "from 1 to 2000"),
        ("corpus_read", {"repo": A, "path": "README.md", "offset": -1}, "from 0"),
        ("corpus_read", {"repo": A, "path": "x" * 1025}, "longer than 1024"),
        ("corpus_list", {"repo": A, "glob": "*" * 101}, "longer than 100"),
        ("corpus_list", {"repo": "r" * 256}, "longer than 255"),
        ("corpus_list", [A], "must be an object"),
    ],
)
def test_bounds(corpus: Corpus, name: str, args: Any, match: str) -> None:
    with pytest.raises(ToolError, match=match):
        corpus.call(name, args)


def test_bounds_at_the_limit(corpus: Corpus) -> None:
    ops.check_args("corpus_search", {"query": "x" * 200, "path_glob": "*" * 100, "max_results": 50})
    ops.check_args("corpus_read", {"repo": A, "path": "a", "offset": 0, "limit": 2000})
    assert ops.check_args("corpus_search", {"query": "x"})["max_results"] == 20
    assert ops.check_args("corpus_read", {"repo": A, "path": "a"}) == {
        "repo": A,
        "path": "a",
        "offset": 0,
        "limit": 400,
    }


def test_response_size_bounds(root: Path, corpus: Corpus, rg: str) -> None:
    """Results are cut to 64 KiB as sent, JSON-encoded: non-ASCII text escapes to 6 bytes a
    character, so it is the encoded size that is bounded, not the raw one."""
    corpus.rg = rg
    gen = root / SERVER / "gen-1" / A
    wide = "é" * 500
    (gen / "wide.txt").write_text("".join(f"{wide} needle {n}\n" for n in range(3000)))
    for n in range(60):
        (gen / f"many/{n:02}.txt").parent.mkdir(exist_ok=True)
        (gen / f"many/{n:02}.txt").write_text(f"{wide} needle\n")
    read = corpus.call("corpus_read", {"repo": A, "path": "wide.txt", "limit": 2000})
    assert read["truncated"] is True and 0 < len(read["lines"]) < 2000
    assert len(json.dumps(read)) <= ops.MAX_RESPONSE_BYTES
    found = corpus.call("corpus_search", {"query": "needle", "repo": A, "max_results": 50})
    assert found["truncated"] is True and 0 < len(found["results"]) < 50
    assert len(json.dumps(found)) <= ops.MAX_RESPONSE_BYTES
    assert all(len(r["text"]) <= ops.TEXT_MAX for r in found["results"])
    few = corpus.call("corpus_search", {"query": "needle", "repo": A, "max_results": 1})
    assert len(few["results"]) == 1 and few["truncated"] is True
    one = corpus.call("corpus_search", {"query": "needle", "path_glob": "*.md"})
    assert [r["path"] for r in one["results"]] == ["README.md"] and one["truncated"] is False


def test_list_bound(root: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops, "LIST_MAX", 2)
    listed = corpus.call("corpus_list", {"repo": A})
    assert listed["paths"] == ["README.md", "docs/guide.md"] and listed["truncated"] is True


def test_search_without_rg(corpus: Corpus) -> None:
    corpus.rg = "/nonexistent/rg"
    with pytest.raises(ToolError, match="no rg"):
        corpus.call("corpus_search", {"query": "needle"})


# -- the model proxy -----------------------------------------------------------------------------
class Upstreamer:
    """A stub model endpoint on 127.0.0.1: records each request, answers with `reply`."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.sse = False
        self.server: asyncio.base_events.Server | None = None

    async def start(self) -> Upstream:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return Upstream("http", "127.0.0.1", port, "/gw")

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in head[1:] if h)
        }
        body = json.loads(await reader.readexactly(int(headers["content-length"])))
        self.requests.append((head[0], headers, body))
        if self.sse:
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
                {"type": "message_delta", "usage": {"output_tokens": 3}},
                {"type": "message_delta", "usage": {"output_tokens": 9}},
            ]
            data = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
            chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(data), data)
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                b"transfer-encoding: chunked\r\n\r\n" + chunked
            )
        else:
            out = json.dumps(
                {"usage": {"input_tokens": 10, "cache_read_input_tokens": 2, "output_tokens": 5}}
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                b"content-length: %d\r\n\r\n%s" % (len(out), out)
            )
        await writer.drain()
        writer.close()


async def http(path: str, raw: bytes) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_unix_connection(path)
    with contextlib.suppress(ConnectionError):  # refused before the body was read
        writer.write(raw)
        await writer.drain()
    data = await read_to_close(reader)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])}
    return int(lines[0].split(" ")[1]), headers, body


def post(
    answer_id: str = AID,
    route: str = "/v1/messages",
    body: dict[str, Any] | bytes | None = None,
    headers: tuple[str, ...] = (),
) -> bytes:
    payload = body if isinstance(body, bytes) else json.dumps(body or {"model": "x"}).encode()
    head = [f"POST /a/{answer_id}{route} HTTP/1.1", "host: localhost"]
    head += [*headers, f"content-length: {len(payload)}"]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + payload


def with_proxy(
    sockdir: Path,
    scenario: Callable[[str, Upstreamer, list[Call]], Awaitable[Any]],
    *,
    max_turns: int = 8,
    key: str | None = KEY,
    sse: bool = False,
) -> Any:
    async def main() -> Any:
        up = Upstreamer()
        up.sse = sse
        calls: list[Call] = []
        mp = ModelProxy(
            upstream=await up.start(),
            model="claude-pinned-1",
            key=lambda: key,
            max_turns=lambda: max_turns,
            on_call=calls.append,
        )
        path = str(sockdir / "m.sock")
        server = await asyncio.start_unix_server(mp.handle, path=path)
        try:
            return await scenario(path, up, calls)
        finally:
            server.close()
            await server.wait_closed()
            await up.stop()

    return asyncio.run(main())


def test_model_proxy_turn_cap(sockdir: Path) -> None:
    """Call 9 with max_turns 8 is a 429 the client won't retry; other answers are unaffected,
    and count_tokens is not a turn."""

    async def scenario(path: str, up: Upstreamer, calls: list[Call]) -> None:
        for _ in range(8):
            assert (await http(path, post()))[0] == 200
        assert (await http(path, post(route="/v1/messages/count_tokens")))[0] == 200
        status, headers, body = await http(path, post())
        assert status == 429 and headers["x-should-retry"] == "false"
        assert json.loads(body) == {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "turn limit reached"},
        }
        assert (await http(path, post(AID2)))[0] == 200
        assert len(up.requests) == 10  # the refused call never left the broker
        assert [c.turn for c in calls] == [1, 2, 3, 4, 5, 6, 7, 8, None, 9, 1]
        assert calls[9].status == 429 and calls[9].forwarded is False

    with_proxy(sockdir, scenario)


def test_model_proxy_turns_kept_a_day(sockdir: Path) -> None:
    """An answer that goes quiet keeps its count, past the audit's 15 minutes; a day later the
    entry is pruned."""
    tick = Tick(0.0)
    mp = ModelProxy(
        upstream=Upstream("http", "127.0.0.1", 9, ""),
        model="m",
        key=lambda: None,
        max_turns=lambda: 1,
        on_call=lambda call: None,
        clock=tick,
    )

    async def main() -> list[int]:
        path = str(sockdir / "m.sock")
        server = await asyncio.start_unix_server(mp.handle, path=path)
        statuses = [(await http(path, post()))[0]]  # turn 1, then no key: 503
        tick.t = broker.AUDIT_TTL_S + 60
        statuses.append((await http(path, post()))[0])
        tick.t = proxy.TURNS_TTL_S + 1
        statuses.append((await http(path, post(AID2)))[0])
        server.close()
        await server.wait_closed()
        return statuses

    assert asyncio.run(main()) == [503, 429, 503]
    assert set(mp.turns) == {AID2}


def test_model_proxy_rewrites(sockdir: Path) -> None:
    """The broker's key is added and the caller's credentials dropped, the model is forced,
    an oversize body is refused, and every other path is 404."""

    async def scenario(path: str, up: Upstreamer, calls: list[Call]) -> None:
        sent = post(
            body={"model": "claude-opus-anything", "max_tokens": 5},
            headers=(
                "x-api-key: sk-caller",
                "authorization: Bearer sk-caller",
                "anthropic-version: 2023-06-01",
                "x-forwarded-for: 1.2.3.4",
            ),
        )
        status, _headers, body = await http(path, sent)
        assert status == 200 and json.loads(body)["usage"]["output_tokens"] == 5
        line, headers, forwarded = up.requests[-1]
        assert line == "POST /gw/v1/messages HTTP/1.1"
        assert headers["x-api-key"] == KEY and "authorization" not in headers
        assert headers["anthropic-version"] == "2023-06-01" and "x-forwarded-for" not in headers
        assert forwarded == {"model": "claude-pinned-1", "max_tokens": 5}
        assert (calls[-1].input_tokens, calls[-1].output_tokens) == (12, 5)

        big = b'{"model":"x","p":"' + b"x" * proxy.MAX_REQUEST_BYTES + b'"}'
        assert (await http(path, post(body=big)))[0] == 413
        for raw in (
            b"GET /a/" + AID.encode() + b"/v1/messages HTTP/1.1\r\n\r\n",
            post(route="/v1/complete"),
            post(route="/v1/messages/../../admin"),
            b"POST /v1/messages HTTP/1.1\r\ncontent-length: 2\r\n\r\n{}",
            post(answer_id="XYZ"),
        ):
            assert (await http(path, raw))[0] == 404, raw
        assert (await http(path, post(body=b"[1]")))[0] == 400
        chunked = b"POST /a/" + AID.encode() + b"/v1/messages HTTP/1.1\r\n"
        chunked += b"transfer-encoding: chunked\r\n\r\n0\r\n\r\n"
        assert (await http(path, chunked))[0] == 411
        assert len(up.requests) == 1

    with_proxy(sockdir, scenario)


def test_model_proxy_streams_and_meters(sockdir: Path) -> None:
    async def scenario(path: str, up: Upstreamer, calls: list[Call]) -> None:
        status, headers, body = await http(path, post())
        assert status == 200 and headers["transfer-encoding"] == "chunked"
        assert b"message_delta" in body  # the upstream's bytes, as they were
        assert (calls[-1].input_tokens, calls[-1].output_tokens) == (7, 9)

    with_proxy(sockdir, scenario, sse=True)


def test_model_proxy_without_key(sockdir: Path) -> None:
    async def scenario(path: str, up: Upstreamer, calls: list[Call]) -> None:
        status, headers, _body = await http(path, post())
        assert status == 503 and headers["x-should-retry"] == "false"
        assert up.requests == [] and calls[-1].forwarded is False

    with_proxy(sockdir, scenario, key=None)


# -- the broker's key and model.conf: crewkiosk-run's rules (test_kiosk_runner's cases) ----------
@pytest.mark.parametrize("text", ["ANTHROPIC_API_KEY=k\n", "k\n", "ANTHROPIC_API_KEY=k", "k\nx"])
def test_broker_key_accepted(tmp_path: Path, text: str) -> None:
    path = tmp_path / "broker-model.credential"
    path.write_text(text)
    assert parse_key(path) == "k"


@pytest.mark.parametrize(
    "text",
    [
        "FOO=k\n",
        "ANTHROPIC_API_KEY=a=b\n",
        "export ANTHROPIC_API_KEY=k\n",
        "k\r\n",
        " k\n",
        "k \n",
        "ANTHROPIC_API_KEY=k\r\n",
        "ANTHROPIC_API_KEY=\n",
        "",
    ],
)
def test_broker_key_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / "broker-model.credential"
    path.write_text(text)
    with pytest.raises(ConfError, match=str(path)):
        parse_key(path)


def test_model_conf_rules(tmp_path: Path) -> None:
    conf = tmp_path / "model.conf"
    assert parse_model_conf(conf) == proxy.ModelConf(proxy.DEFAULT_BASE_URL, proxy.DEFAULT_MODEL)
    conf.write_text("# the gateway\n\nbase_url=https://llm.example.lan\nmodel=haiku-latest\n")
    assert parse_model_conf(conf) == proxy.ModelConf("https://llm.example.lan", "haiku-latest")
    conf.write_text("base_url=https://llm.example.lan\nmodel=haiku\n")
    assert parse_model_conf(conf).model == "haiku"  # a gateway's own name
    for text in (
        "model=haiku\n",
        "api_key=sk-sneaky\n",
        "base_url = https://a.lan\n",
        "# a comment\n\nsk-ant-pasted-by-mistake\n",
        "base_url=http://llm.example.lan\n",
        "base_url=https://llm.example.lan /v1\n",
        "base_url=\n",
        "model=haiku latest\n",
        "model=$(id)\n",
    ):
        conf.write_text(text)
        with pytest.raises(ConfError) as exc:
            parse_model_conf(conf)
        for secret in ("sneaky", "pasted-by-mistake", "a.lan"):
            assert secret not in str(exc.value), text


def test_broker_reads_its_own_key(tmp_path: Path) -> None:
    """The broker's key is broker-model.credential, never the runner's model.credential."""
    assert Path("/etc/crewkiosk/broker-model.credential") == broker.MODEL_KEY
    (tmp_path / "model.credential").write_text("sk-runner\n")
    (tmp_path / "broker-model.credential").write_text("sk-broker\n")
    b = Broker(model_key=tmp_path / "broker-model.credential", model_conf=tmp_path / "none.conf")
    b.load_model()
    assert b.key == "sk-broker"
    (tmp_path / "broker-model.credential").unlink()
    b.load_model()
    assert b.key is None  # the ops are still served; every model call is a 503
    unit = (Path(__file__).parents[3] / "packaging" / "crewkiosk-broker.service").read_text()
    assert "/etc/crewkiosk/broker-model.credential, 0600" in unit
    assert "crewkiosk-broker:crewkiosk-broker" in unit
    assert "-/etc/crewkiosk/model.credential" in unit  # the runner's key: inaccessible


# -- audit and control ---------------------------------------------------------------------------
def model_call(answer_id: str = AID, **kw: Any) -> Call:
    base: dict[str, Any] = {"path": "/v1/messages", "turn": 1, "status": 200}
    base |= {"bytes": 100, "latency_ms": 40, "input_tokens": 10, "output_tokens": 5}
    return Call(answer_id, **(base | kw))


def test_audit_lines_and_control(sockdir: Path, corpus: Corpus) -> None:
    tick = Tick()
    ledger = Ledger(sockdir / "state", clock=tick)
    op = op_server(corpus, ledger)
    with_server(
        sockdir,
        op.handle,
        lambda path: converse(
            path,
            hello_line(),
            rpc(
                1,
                "tools/call",
                {"name": "corpus_read", "arguments": {"repo": A, "path": "README.md"}},
            ),
            rpc(2, "tools/call", {"name": "corpus_list", "arguments": {"repo": C}}),
            rpc(3, "tools/list"),  # not an op: no line
        ),
    )
    ledger.model(SERVER, model_call())
    control = Control(ledger, os.getuid())
    lines = control.answer(json.dumps({"op": "audit", "answer_id": AID}).encode())["lines"]
    assert [(x["op"], x["ok"]) for x in lines] == [
        ("corpus_read", True),
        ("corpus_list", False),
        ("model", True),
    ]
    for x in lines:
        assert set(x) == {
            "ts",
            "server",
            "answer_id",
            "post_id",
            "op",
            "args",
            "bytes",
            "latency_ms",
            "ok",
        }
        assert (x["server"], x["answer_id"], x["post_id"]) == (SERVER, AID, POST)
        assert x["ts"] == "2026-10-30T12:00:00+00:00"
    assert lines[0]["args"] == {"repo": A, "path": "README.md"} and lines[0]["bytes"] > 0
    assert lines[2]["args"]["turn"] == 1 and lines[2]["bytes"] == 100
    # Returned, then forgotten.
    again = control.answer(json.dumps({"op": "audit", "answer_id": AID}).encode())
    assert again == {"ok": True, "lines": []}
    # And on disk, 0600, one line each.
    audit = sockdir / "state" / "audit" / f"{SERVER}.jsonl"
    assert len(audit.read_text().splitlines()) == 3
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600
    # The control socket takes no lines from anyone: there is no op to report one.
    for req in ({"op": "record", "line": lines[0]}, {"op": "audit", "answer_id": "x"}, [1]):
        assert control.answer(json.dumps(req).encode())["ok"] is False
    assert control.answer(b"nope")["ok"] is False


def test_audit_bounded_and_expired(tmp_path: Path) -> None:
    tick = Tick()
    ledger = Ledger(tmp_path / "state", clock=tick)
    for n in range(broker.AUDIT_LINES + 5):
        ledger.op(SERVER, AID, POST, "corpus_list", {"n": n}, 1, 1, True)
    lines = ledger.take(AID)
    assert len(lines) == broker.AUDIT_LINES and lines[-1]["args"] == {"n": broker.AUDIT_LINES + 4}
    ledger.op(SERVER, AID2, POST, "corpus_list", {}, 1, 1, True)
    tick.t += broker.AUDIT_TTL_S + 1
    assert ledger.take(AID2) == []
    assert ledger.metrics()[SERVER]["answers"] == 2  # the rollup keeps the day's answers


def test_control_peer_uid(sockdir: Path) -> None:
    """Only the daemon's uid is served: any other is closed before its request is read."""
    ledger = Ledger(sockdir / "state", clock=Tick())
    ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
    request = json.dumps({"op": "audit", "answer_id": AID}).encode() + b"\n"

    async def ask(path: str) -> bytes:
        reader, writer = await asyncio.open_unix_connection(path)
        with contextlib.suppress(ConnectionError):
            writer.write(request)
            await writer.drain()
        data = await read_to_close(reader)
        writer.close()
        return data

    refused = with_server(sockdir, Control(ledger, os.getuid() + 1).handle, ask)
    assert refused == b""
    assert len(ledger.answers[(SERVER, AID)].lines) == 1  # nothing was taken
    served = with_server(sockdir, Control(ledger, os.getuid()).handle, ask)
    assert [x["op"] for x in json.loads(served)["lines"]] == ["corpus_list"]


def test_kiosk_broker_client(sockdir: Path, caplog: pytest.LogCaptureFixture) -> None:
    ledger = Ledger(sockdir / "state", clock=Tick())
    ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)

    async def scenario(path: str) -> tuple[Any, Any, Any]:
        client = KioskBrokerClient(path)
        return await client.audit(AID), await client.audit(AID), await client.metrics()

    first, second, metrics = with_server(sockdir, Control(ledger, os.getuid()).handle, scenario)
    assert [x["op"] for x in first] == ["corpus_list"] and second == []
    assert metrics[SERVER]["ops"] == 1

    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_to_close(reader)  # never answers; ends when the client gives up
        writer.close()  # the server's wait_closed() waits for every connection

    async def slow(path: str) -> Any:
        return await KioskBrokerClient(path, timeout_s=0.2).audit(AID)

    caplog.set_level(logging.WARNING, logger="crewchat.kiosk_broker")
    assert with_server(sockdir, stall, slow) == []
    missing = KioskBrokerClient(str(sockdir / "absent.sock"))
    assert asyncio.run(missing.audit(AID)) == [] and asyncio.run(missing.metrics()) is None
    assert any("no answer within" in r.message for r in caplog.records)
    assert any("unavailable" in r.message for r in caplog.records)


# -- metrics -------------------------------------------------------------------------------------
def test_percentile() -> None:
    assert percentile([], 50) is None
    assert percentile([5], 95) == 5.0
    values = list(range(1, 101))
    assert (percentile(values, 50), percentile(values, 95)) == (50.0, 95.0)
    assert percentile([1, 2, 3, 4], 50) == 2.0


def test_metrics_rollup(sockdir: Path) -> None:
    tick = Tick()
    ledger = Ledger(sockdir / "state", clock=tick)
    ledger.hello(SERVER, AID, POST)
    for ms in (10, 20, 30, 40):
        ledger.op(SERVER, AID, POST, "corpus_search", {}, 1, ms, True)
    tick.t += 12
    ledger.model(SERVER, model_call(input_tokens=100, output_tokens=50))
    ledger.model(SERVER, model_call(turn=None, path="/v1/messages/count_tokens", input_tokens=0))
    ledger.model(SERVER, model_call(turn=9, status=429, forwarded=False, input_tokens=0))
    ledger.hello(SERVER, AID2, "post-2")
    ledger.op(SERVER, AID2, "post-2", "corpus_read", {}, 1, 1000, True)
    ledger.op(OTHER, AID2, "post-2", "corpus_read", {}, 1, 5, True)

    m = ledger.metrics()
    assert set(m) == {SERVER, OTHER}
    dev = m[SERVER]
    assert dev["date"] == "2026-10-30" and dev["answers"] == 2
    assert dev["ops"] == 5 and dev["model_calls"] == 1  # count_tokens and a refusal aren't calls
    assert (dev["input_tokens"], dev["output_tokens"]) == (100, 60)
    assert dev["op_latency_ms"] == {"p50": 30.0, "p95": 1000.0}
    assert dev["per_answer"]["ops"] == {"p50": 1.0, "p95": 4.0}
    assert dev["per_answer"]["model_calls"] == {"p50": 0.0, "p95": 1.0}
    assert dev["per_answer"]["tokens"] == {"p50": 0.0, "p95": 160.0}
    assert dev["per_answer"]["wall_s"] == {"p50": 0.0, "p95": 12.0}
    on_disk = sockdir / "state" / "metrics" / f"{SERVER}-2026-10-30.json"
    assert json.loads(on_disk.read_text()) == dev
    assert stat.S_IMODE(on_disk.stat().st_mode) == 0o600
    # A new UTC day starts a new rollup.
    tick.t += 86_400
    ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
    assert ledger.metrics()[SERVER]["date"] == "2026-10-31"
    assert ledger.metrics()[SERVER]["ops"] == 1


# -- the service ---------------------------------------------------------------------------------
def test_broker_sockets_per_server(tmp_path: Path, sockdir: Path, root: Path) -> None:
    """One op socket and one model socket per research server, 0660; a server whose manifest
    goes is closed on reload, and a changed max_turns applies to the open socket."""
    pool = tmp_path / "pool"
    pool.mkdir()
    write_manifest(pool, SERVER, repos=[A, B])
    write_manifest(pool, OTHER, repos=[C], max_turns=3)
    run = sockdir / "run"
    run.mkdir()
    b = Broker(
        pool_dir=pool,
        corpus_root=root,
        runtime_dir=run,
        state_dir=tmp_path / "state",
        model_conf=tmp_path / "model.conf",
        model_key=tmp_path / "broker-model.credential",
        socket_group=None,
        control_uid=os.getuid(),
    )

    async def main() -> None:
        await b.reload()
        await b.start_control(None)
        names = sorted(p.name for p in run.iterdir())
        assert names == [
            "control.sock",
            "dev.model.sock",
            "dev.sock",
            "other.model.sock",
            "other.sock",
        ]
        for p in run.iterdir():
            assert stat.S_IMODE(p.stat().st_mode) == 0o660, p
        assert b.sites[OTHER].max_turns == 3 and b.sites[SERVER].corpus.repos == (A, B)
        # The other server's socket serves the other server's pool, and only that.
        replies = await converse(
            str(run / "other.sock"),
            hello_line(),
            rpc(1, "tools/call", {"name": "corpus_list", "arguments": {"repo": A}}),
            rpc(2, "tools/call", {"name": "corpus_list", "arguments": {"repo": C}}),
        )
        assert [r["result"]["isError"] for r in replies] == [True, False]
        (pool / f"{OTHER}.json").unlink()
        write_manifest(pool, SERVER, repos=[A], max_turns=2)
        await b.reload()
        assert sorted(p.name for p in run.iterdir()) == [
            "control.sock",
            "dev.model.sock",
            "dev.sock",
        ]
        assert b.sites[SERVER].max_turns == 2 and b.sites[SERVER].corpus.repos == (A,)
        await b.stop()

    asyncio.run(main())


def test_listened_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LISTEN_PID", raising=False)
    assert broker.listened_socket() is None
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    monkeypatch.setenv("LISTEN_FDS", "1")
    assert broker.listened_socket() is None  # meant for another process


# -- the relay -----------------------------------------------------------------------------------
RELAY = Path(relay.__file__)


def test_relay_is_stdlib_only() -> None:
    """Installed alone: it imports nothing outside the standard library."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(RELAY.read_text())):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported - {"__future__"} <= sys.stdlib_module_names, imported
    assert RELAY.read_text().startswith("#!/usr/bin/python3 -I\n")


def accept_one(listener: socket.socket) -> socket.socket:
    listener.settimeout(10)
    conn, _ = listener.accept()
    conn.settimeout(10)
    return conn


def recv_all(conn: socket.socket) -> bytes:
    chunks = []
    while data := conn.recv(65536):
        chunks.append(data)
    return b"".join(chunks)


def test_relay_pumps_mcp(sockdir: Path) -> None:
    path = str(sockdir / "op.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(RELAY), "mcp", path, AID, POST],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        conn = accept_one(listener)
        sent = b'{"jsonrpc":"2.0","id":1}\n\x00\xff\xfe raw bytes\r\n'
        proc.stdin.write(sent)
        proc.stdin.close()
        got = recv_all(conn)  # the relay half-closes when stdin ends
        first, _, rest = got.partition(b"\n")
        assert json.loads(first) == {
            "jsonrpc": "2.0",
            "method": "crewkiosk/hello",
            "params": {"answer_id": AID, "post_id": POST},
        }
        assert rest == sent
        reply = b'{"jsonrpc":"2.0","id":1,"result":{}}\n\x80\x81'
        conn.sendall(reply)
        conn.close()
        assert proc.stdout.read() == reply
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        listener.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_relay_pumps_http(sockdir: Path) -> None:
    path = str(sockdir / "model.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(4)
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-I", str(RELAY), "http", path, str(port)])
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=10)
                break
            except OSError:
                assert time.monotonic() < deadline, "the relay never listened"
                time.sleep(0.05)
        result: dict[str, bytes] = {}

        def upstream() -> None:
            conn = accept_one(listener)
            result["got"] = recv_all(conn)
            conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n\x00\xffback")
            conn.close()

        t = threading.Thread(target=upstream)
        t.start()
        sent = b"POST /a/" + AID.encode() + b"/v1/messages HTTP/1.1\r\n\r\n\x00\xff"
        client.sendall(sent)
        client.shutdown(socket.SHUT_WR)
        assert recv_all(client) == b"HTTP/1.1 200 OK\r\n\r\n\x00\xffback"
        t.join(10)
        assert result["got"] == sent
        client.close()
    finally:
        proc.kill()
        proc.wait(timeout=10)
        listener.close()
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        pytest.fail("the relay still listens after it was killed")


def test_relay_usage() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(RELAY), "shell", "id"], capture_output=True, timeout=30
    )
    assert proc.returncode == 64 and b"usage" in proc.stderr
