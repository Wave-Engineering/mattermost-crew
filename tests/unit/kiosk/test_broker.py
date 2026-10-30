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
from collections import deque
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
        self.slow = False
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
        if self.slow:
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n\r\n")
            start = {"type": "message_start", "message": {"usage": {"input_tokens": 11}}}
            writer.write(b"data: " + json.dumps(start).encode() + b"\n\n")
            with contextlib.suppress(ConnectionError):
                for _ in range(200):
                    await writer.drain()
                    await asyncio.sleep(0.01)
                    writer.write(b"data: {}\n\n" + b" " * 65536)
            writer.close()
            return
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
    slow: bool = False,
) -> Any:
    async def main() -> Any:
        up = Upstreamer()
        up.sse, up.slow = sse, slow
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


def test_model_proxy_count_tokens_cap(sockdir: Path) -> None:
    """count_tokens has its own cap of max_turns per answer, refused like the turn cap: past
    it the answer's turns are still served, and another answer's counts too."""

    async def scenario(path: str, up: Upstreamer, calls: list[Call]) -> None:
        count = post(route="/v1/messages/count_tokens")
        assert [(await http(path, count))[0] for _ in range(2)] == [200, 200]
        status, headers, body = await http(path, count)
        assert status == 429 and headers["x-should-retry"] == "false"
        assert json.loads(body)["error"]["message"] == "count_tokens limit reached"
        assert [(await http(path, post()))[0] for _ in range(2)] == [200, 200]
        assert (await http(path, post(AID2, route="/v1/messages/count_tokens")))[0] == 200
        assert len(up.requests) == 5  # the refused count never left the broker
        assert calls[2].turn is None and calls[2].forwarded is False

    with_proxy(sockdir, scenario, max_turns=2)


def test_model_proxy_turns_kept_a_day(sockdir: Path) -> None:
    """An answer that goes quiet keeps its count, past the audit's 15 minutes; a day later the
    entry is pruned."""
    tick = Tick(0.0)
    mp = ModelProxy(
        upstream=Upstream("http", "127.0.0.1", 9, ""),
        model="m",
        key=lambda: KEY,
        max_turns=lambda: 1,
        on_call=lambda call: None,
        clock=tick,
    )

    async def main() -> list[int]:
        path = str(sockdir / "m.sock")
        server = await asyncio.start_unix_server(mp.handle, path=path)
        statuses = [(await http(path, post()))[0]]  # turn 1, to a closed port: 502
        tick.t = broker.AUDIT_TTL_S + 60
        statuses.append((await http(path, post()))[0])
        tick.t = proxy.TURNS_TTL_S + 1
        statuses.append((await http(path, post(AID2)))[0])
        server.close()
        await server.wait_closed()
        return statuses

    assert asyncio.run(main()) == [502, 429, 502]
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
        assert up.server is not None
        port = up.server.sockets[0].getsockname()[1]
        assert headers["host"] == f"127.0.0.1:{port}"  # the stub's port isn't http's own
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
        assert (await http(path, post(body=b"[" * 100_000 + b"]" * 100_000)))[0] == 400
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
    """With no key every call is a 503, and none spends a turn: past max_turns it is still a
    503, never the turn cap's 429."""

    async def scenario(path: str, up: Upstreamer, calls: list[Call]) -> None:
        for _ in range(3):
            status, headers, _body = await http(path, post())
            assert status == 503 and headers["x-should-retry"] == "false"
        assert up.requests == [] and calls[-1].forwarded is False
        assert [c.turn for c in calls] == [0, 0, 0]

    with_proxy(sockdir, scenario, key=None, max_turns=1)


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
    base |= {"complete": True}
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
    assert len(ledger.lines[(SERVER, AID)]) == 1  # nothing was taken
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
    refused = model_call(turn=9, status=429, forwarded=False, input_tokens=0, output_tokens=0)
    ledger.model(SERVER, refused)
    ledger.hello(SERVER, AID2, "post-2")
    ledger.op(SERVER, AID2, "post-2", "corpus_read", {}, 1, 1000, True)
    ledger.op(OTHER, AID2, "post-2", "corpus_read", {}, 1, 5, True)

    m = ledger.metrics()
    assert set(m) == {SERVER, OTHER}
    dev = m[SERVER]
    assert dev["date"] == "2026-10-30" and dev["answers"] == 2
    assert dev["ops"] == 5 and dev["model_calls"] == 1  # count_tokens and a refusal aren't calls
    assert (dev["input_tokens"], dev["output_tokens"]) == (100, 55)
    assert dev["refused"] == {"hellos": 0, "ops": 0, "model_calls": 1}
    assert dev["op_latency_ms"] == {"p50": 30.0, "p95": 1000.0}
    assert dev["per_answer"]["ops"] == {"p50": 1.0, "p95": 4.0}
    assert dev["per_answer"]["model_calls"] == {"p50": 0.0, "p95": 1.0}
    assert dev["per_answer"]["tokens"] == {"p50": 0.0, "p95": 155.0}
    assert dev["per_answer"]["wall_s"] == {"p50": 0.0, "p95": 12.0}
    on_disk = sockdir / "state" / "metrics" / f"{SERVER}-2026-10-30.json"
    ledger.flush()  # the file trails the lines by up to METRICS_WRITE_S
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
    control = sockdir / "ctl" / "control.sock"
    b = Broker(
        pool_dir=pool,
        corpus_root=root,
        runtime_dir=run,
        control_path=control,
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
            "dev.model.sock",
            "dev.sock",
            "other.model.sock",
            "other.sock",
        ]
        for p in [*run.iterdir(), control]:
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
        assert sorted(p.name for p in run.iterdir()) == ["dev.model.sock", "dev.sock"]
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


# -- the precheck review's findings (#161) -------------------------------------------------------
@pytest.mark.parametrize(
    "header",
    [
        "anthropic-beta: x\nContent-Length: 0\n\nPOST /v1/files HTTP/1.1",  # bare LF smuggling
        "anthropic-beta: x\rinjected: y",
        "accept: a\x00b",
        "bad name: x",
        "no-colon-here",
    ],
)
def test_model_proxy_refuses_malformed_headers(sockdir: Path, header: str) -> None:
    """A control character in a passed-through header would carry a second request upstream,
    with the broker's key appended to it: refused before anything is forwarded."""

    async def scenario(path: str, up: Upstreamer, calls: list[Call]) -> None:
        assert (await http(path, post(headers=(header,))))[0] == 400
        assert up.requests == [] and calls == []

    with_proxy(sockdir, scenario)


def test_model_proxy_audits_a_call_the_client_abandons(sockdir: Path) -> None:
    """A client that hangs up mid-stream still leaves its call accounted for: forwarded,
    counted as a turn, its tokens so far metered, and marked incomplete."""

    async def scenario(path: str, up: Upstreamer, calls: list[Call]) -> None:
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(post())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        writer.close()
        for _ in range(500):
            if calls:
                break
            await asyncio.sleep(0.01)
        [call] = calls
        assert call.forwarded and call.turn == 1 and call.status == 200
        assert call.complete is False and call.input_tokens == 11
        assert len(up.requests) == 1

    with_proxy(sockdir, scenario, slow=True)
    ledger = Ledger(sockdir / "state", clock=Tick())
    ledger.model(SERVER, model_call(complete=False))
    [line] = ledger.take(AID)
    assert line["ok"] is False and line["args"]["complete"] is False


def test_idle_connections_are_closed_and_capped(
    sockdir: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection that sends nothing is closed, and the sockets refuse past their cap, so no
    kiosk can hold the broker's descriptors."""
    monkeypatch.setattr(proxy, "REQUEST_TIMEOUT_S", 0.2)
    monkeypatch.setattr(broker, "HELLO_TIMEOUT_S", 0.2)

    async def idle(path: str) -> bytes:
        reader, writer = await asyncio.open_unix_connection(path)
        data = await asyncio.wait_for(read_to_close(reader), 5)
        writer.close()
        return data

    assert with_server(sockdir, op_server(corpus).handle, idle) == b""
    with_proxy(sockdir, lambda path, up, calls: idle(path))

    monkeypatch.setattr(proxy, "MAX_CONNECTIONS", 2)
    monkeypatch.setattr(proxy, "REQUEST_TIMEOUT_S", 5)

    async def crowd(path: str, up: Upstreamer, calls: list[Call]) -> int:
        held = [await asyncio.open_unix_connection(path) for _ in range(2)]
        await asyncio.sleep(0.1)
        status = (await http(path, post()))[0]
        for _, w in held:
            w.close()
        return status

    assert with_proxy(sockdir, crowd) == 503


def test_audit_args_are_bounded(sockdir: Path, corpus: Corpus) -> None:
    """A refused call's raw arguments never reach the audit: only the op's own keys, cut
    short, and the names of any others. 200 lines always fit the daemon's reply limit."""
    from crewchat.kiosk_broker import MAX_REPLY_BYTES

    ledger = Ledger(sockdir / "state", clock=Tick())
    big = {"repo": "r" * 5000, "glob": "g" * 300, "x" * 500: "y" * 1_000_000, "n": [1] * 9}
    call(sockdir, op_server(corpus, ledger), "corpus_list", big)
    [line] = ledger.take(AID)
    assert line["args"]["repo"] == "r" * broker.AUDIT_STR_MAX
    assert line["args"]["unknown_keys"] == ["x" * 64, "n"]
    assert "y" * 100 not in json.dumps(line)
    assert broker.audit_args("corpus_list", {"repo": "é" * 256, "glob": "é" * 256}) == {
        "truncated": True
    }
    assert broker.audit_args("shell", {"cmd": "id"}) == {"unknown_keys": ["cmd"]}
    for _ in range(broker.AUDIT_LINES):
        ledger.op(
            SERVER, AID, POST, "corpus_list", broker.audit_args("corpus_list", big), 1, 1, False
        )
    reply = Control(ledger, os.getuid()).answer(
        json.dumps({"op": "audit", "answer_id": AID}).encode()
    )
    assert len(broker.jsonl(reply)) < MAX_REPLY_BYTES


def test_expired_lines_are_freed_and_a_resumed_answer_keeps_its_day(tmp_path: Path) -> None:
    tick = Tick()
    ledger = Ledger(tmp_path / "state", clock=tick)
    ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 10, True)
    answer = ledger.days[(SERVER, "2026-10-30")].answers[AID]
    tick.t += broker.AUDIT_TTL_S + 1
    assert len(ledger.lines[(SERVER, AID)]) == 1
    ledger.op(SERVER, AID2, POST, "corpus_list", {}, 1, 10, True)  # expires AID
    assert (SERVER, AID) not in ledger.lines  # gone from memory, not just from reach
    ledger.op(SERVER, AID, POST, "corpus_read", {}, 1, 30, True)  # AID comes back
    m = ledger.metrics()[SERVER]
    assert m["answers"] == 2 and m["ops"] == 3
    assert ledger.days[(SERVER, "2026-10-30")].answers[AID] is answer and answer.ops == 2
    assert list(ledger.days[(SERVER, "2026-10-30")].op_latency_ms) == [10, 10, 30]


def test_server_ids_that_collide_with_sockets_are_skipped(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    for server in (SERVER, "control", "dev.model"):
        write_manifest(pool, server, repos=[A])
    b = Broker(pool_dir=pool, corpus_root=tmp_path, socket_group=None)
    assert set(b.manifests()) == {SERVER}


def test_one_answer_cannot_hold_every_op_slot(
    sockdir: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer gets MAX_PER_ANSWER sessions; past that it is refused, and another answer
    is still served. An idle session is closed after OP_IDLE_S."""
    op = op_server(corpus)

    async def scenario(path: str) -> None:
        held = []
        for _ in range(broker.MAX_PER_ANSWER):
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(hello_line() + rpc(1, "ping"))
            await writer.drain()
            assert json.loads(await reader.readline())["result"] == {}
            held.append(writer)
        assert await converse(path, hello_line(), rpc(1, "ping")) == []
        assert [r["id"] for r in await converse(path, hello_line(AID2), rpc(1, "ping"))] == [1]
        for writer in held:
            writer.close()

    with_server(sockdir, op.handle, scenario)
    monkeypatch.setattr(broker, "OP_IDLE_S", 0.2)

    async def idle(path: str) -> bytes:
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(hello_line())
        await writer.drain()
        data = await asyncio.wait_for(read_to_close(reader), 5)
        writer.close()
        return data

    assert with_server(sockdir, op.handle, idle) == b""
    assert op.slots.total == 0 and op.slots.per == {}


def test_one_answer_cannot_hold_every_model_slot(sockdir: Path) -> None:
    async def scenario(path: str, up: Upstreamer, calls: list[Call]) -> None:
        held = []
        for _ in range(proxy.MAX_PER_ANSWER):
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(post())
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")  # in flight: the upstream streams slowly
            held.append(writer)
        status, _, body = await http(path, post())
        assert status == 429 and b"at once" in body
        refused = calls[-1]  # audited, as every refusal is, and no turn spent
        assert (refused.status, refused.forwarded, refused.turn) == (429, False, 0)
        assert (await http(path, post(AID2)))[0] == 200  # another answer is still served
        for writer in held:
            writer.close()
        for _ in range(500):
            if len(calls) == proxy.MAX_PER_ANSWER + 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == proxy.MAX_PER_ANSWER + 2

    with_proxy(sockdir, scenario, slow=True, max_turns=50)


def test_take_is_capped_across_servers(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state", clock=Tick())
    for n in range(8):
        for _ in range(broker.AUDIT_LINES):
            ledger.op(f"s{n}", AID, POST, "corpus_list", {}, 1, 1, True)
    assert len(ledger.take(AID)) == broker.TAKE_MAX


def test_stop_ends_open_sessions(tmp_path: Path, sockdir: Path, root: Path) -> None:
    """On 3.12 a server's wait_closed() waits for every connection: removing a server, or
    stopping, ends its open sessions instead of hanging on them."""
    pool = tmp_path / "pool"
    pool.mkdir()
    write_manifest(pool, SERVER, repos=[A])
    run = sockdir / "run"
    run.mkdir()
    b = Broker(
        pool_dir=pool,
        corpus_root=root,
        runtime_dir=run,
        state_dir=tmp_path / "state",
        model_conf=tmp_path / "model.conf",
        model_key=tmp_path / "none",
        socket_group=None,
        control_uid=os.getuid(),
    )

    async def main() -> bytes:
        await b.reload()
        reader, writer = await asyncio.open_unix_connection(str(run / "dev.sock"))
        writer.write(hello_line())
        await writer.drain()
        await asyncio.sleep(0.05)
        (pool / f"{SERVER}.json").unlink()
        await asyncio.wait_for(b.reload(), 5)
        data = await asyncio.wait_for(read_to_close(reader), 5)
        writer.close()
        await b.stop()
        return data

    assert asyncio.run(main()) == b""
    assert SERVER not in b.sites


def test_slots_admit_nothing_once_closed() -> None:
    """A connection accepted just before a stop, whose handler runs after close_all(), is
    never admitted, so wait_closed() can't wait on it."""

    class Writer:
        closed = False

        def close(self) -> None:
            self.closed = True

    slots = proxy.Slots("t")
    early, late = Writer(), Writer()
    assert slots.admit(early, 2)  # type: ignore[arg-type]
    slots.close_all()
    assert early.closed
    assert not slots.admit(late, 2)  # type: ignore[arg-type]
    assert slots.total == 1 and late not in slots.clients


# -- the units -----------------------------------------------------------------------------------
PACKAGING = Path(__file__).parents[3] / "packaging"


def unit_keys(name: str) -> dict[str, list[str]]:
    """A unit file's `Key=value` lines, every value of each key, comments and sections aside."""
    keys: dict[str, list[str]] = {}
    for line in (PACKAGING / name).read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.startswith(("#", ";", "[")):
            keys.setdefault(key.strip(), []).append(value.strip())
    return keys


def test_control_socket_is_outside_the_runtime_directory() -> None:
    """The service's RuntimeDirectory= is chowned, recursively, at every start: a control
    socket inside it would lose its crewchat group at every boot, and the daemon its audit."""
    from crewchat import kiosk_broker

    service = unit_keys("crewkiosk-broker.service")
    socket_unit = unit_keys("crewkiosk-broker-control.socket")
    [listen] = socket_unit["ListenStream"]
    runtime = [Path("/run", d) for v in service["RuntimeDirectory"] for d in v.split()]
    assert runtime and all(not Path(listen).is_relative_to(d) for d in runtime), listen
    assert listen == str(broker.CONTROL_SOCKET) == kiosk_broker.CONTROL_SOCKET
    assert socket_unit["SocketGroup"] == ["crewchat"] and socket_unit["SocketMode"] == ["0660"]
    assert "RuntimeDirectoryPreserve" not in service
    assert service["MemoryMax"] == ["512M"]


def deep_size(obj: object, seen: set[int] | None = None) -> int:
    """An object's memory with everything it holds, each object counted once."""
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(deep_size(k, seen) + deep_size(v, seen) for k, v in obj.items())
    elif isinstance(obj, list | tuple | set | deque):
        size += sum(deep_size(v, seen) for v in obj)
    for name in getattr(type(obj), "__slots__", ()):
        size += deep_size(getattr(obj, name), seen)
    if hasattr(obj, "__dict__"):  # a plain dataclass, such as Day
        size += deep_size(vars(obj), seen)
    return size


def test_the_ledger_fits_in_half_the_units_memory(tmp_path: Path) -> None:
    """The ledger at its worst: MAX_LIVE_ANSWERS answers of AUDIT_LINES of the largest lines
    the broker writes, and a full day of answer records for every server the unit's
    LimitNOFILE has room for. It fits in half the unit's MemoryMax: the rest is the ops',
    the proxy's and the interpreter's. Both are read from the unit, so neither can drift."""
    service = unit_keys("crewkiosk-broker.service")
    [memory_max] = service["MemoryMax"]
    assert memory_max.endswith("M")
    limit = int(memory_max[:-1]) * 1024 * 1024
    # Each server's op and model sockets, and the model's upstreams: MAX_CONNECTIONS each.
    servers = int(service["LimitNOFILE"][0]) // (3 * broker.MAX_CONNECTIONS)
    longest = broker.SUN_PATH_MAX - len(os.fsencode(broker.RUNTIME_DIR / ".model.sock"))
    server, post = "s" * longest, "p" * 64  # the longest POST_ID_RE allows
    ledger = Ledger(tmp_path / "state", clock=Tick(NOON + 0.123456))
    # One astral character makes a string 4 bytes a character: the most audit_args keeps.
    for name, (_doc, fields) in ops.TOOLS.items():
        args = {f: "\U0001f600" + "a" * 10_000 for f in fields}
        args |= {f"k{n:02d}" + "x" * 100: 1 for n in range(20)}
        shown = "o" * ops.NAME_MAX
        ledger.op(server, AID, post, shown, broker.audit_args(name, args), 0, 0, False)
    big = 10**15
    call = model_call(input_tokens=big, output_tokens=big, bytes=big, latency_ms=big)
    ledger.model(server, call)
    line = max(deep_size(x) for x in ledger.lines[(server, AID)])
    lines = sys.getsizeof(deque([None] * broker.AUDIT_LINES, maxlen=broker.AUDIT_LINES))
    day = broker.Day("2026-10-30")
    for n in range(broker.MAX_ANSWERS_PER_DAY):
        # Each record its own post_id, floats and counts, as the broker makes them: shared
        # objects would be counted once for all of them.
        refused = dict.fromkeys(("ops", "model_calls"), day.date)
        counts = [big + n * 10 + k for k in range(5)]
        a = broker.Answer(f"{n:064d}", NOON + n + 0.25, NOON + n + 0.5, *counts, refused)
        day.answers[aid(n)] = a
    record = deep_size(day.answers[aid(0)])
    day_size = deep_size(day)
    # A full day really was measured, never just the Day object around it, and each record
    # on its own objects: about 700 B each, where shared objects measure about 220.
    assert day_size >= broker.MAX_ANSWERS_PER_DAY * 500, day_size
    worst = broker.MAX_LIVE_ANSWERS * (broker.AUDIT_LINES * line + lines + record)
    # A sizing assumption, not a bound the broker enforces: the servers the unit's LimitNOFILE
    # has room for. The broker opens as many as there are manifests.
    worst += servers * day_size
    assert servers >= 40 and line > 4096  # what the caps were sized against
    assert worst <= limit // 2, (worst, limit)
    # An OOM-killed rg fails its own search; the default policy would stop the whole broker.
    assert service["OOMPolicy"] == ["continue"]


# -- corpus_search streams rg ----------------------------------------------------------------------
FAKE_RG = """\
import json, os, pathlib, sys
here = pathlib.Path(sys.argv[0]).resolve().parent
(here / "pid").write_text(str(os.getpid()))
mode = (here / "mode").read_text()


def match(n, text="needle"):
    data = {"path": {"text": f"./f{n}.txt"}, "line_number": n, "lines": {"text": text + "\\n"}}
    return json.dumps({"type": "match", "data": data}) + "\\n"


if mode == "noise":  # never a match, never an end
    while True:
        sys.stdout.write(json.dumps({"type": "begin", "data": {"path": {"text": "./x"}}}) + "\\n")
elif mode == "matches":  # matches without end
    n = 0
    while True:
        n += 1
        sys.stdout.write(match(n))
elif mode == "long":  # a minified line, then an ordinary one
    sys.stdout.write(match(1, "x" * 1_000_000 + "needle"))
    sys.stdout.write(match(2))
elif mode == "huge":  # a minified line past the whole output budget, then an ordinary one
    sys.stdout.write(match(1, "x" * 6_000_000 + "needle"))
    sys.stdout.write(match(2))
elif mode == "sleep":  # says nothing, and never ends
    import time
    time.sleep(60)
elif mode == "argv":
    (here / "argv").write_text(json.dumps(sys.argv[1:]))
"""


@pytest.fixture
def fake_rg(tmp_path: Path) -> Callable[[str], str]:
    """`fake_rg(mode)`: the path of an rg that writes as `mode` says, and records its pid."""
    home = tmp_path / "fake-rg"
    home.mkdir()
    rg = home / "rg"
    rg.write_text(f"#!{sys.executable} -I\n{FAKE_RG}")
    rg.chmod(0o755)

    def make(mode: str) -> str:
        (home / "mode").write_text(mode)
        return str(rg)

    return make


def gone(pid_file: Path) -> bool:
    try:
        os.kill(int(pid_file.read_text()), 0)
    except ProcessLookupError:
        return True
    return False


def test_search_stops_rg_at_its_output_budget(
    corpus: Corpus, fake_rg: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An rg that writes without end is killed once the search has read its budget, and the
    result is what fit, marked truncated: its output never lands in memory whole."""
    monkeypatch.setattr(ops, "RG_OUTPUT_MAX", 1024 * 1024)
    corpus.rg = fake_rg("noise")
    start = time.monotonic()
    found = corpus.call("corpus_search", {"query": "needle", "repo": A})
    assert found == {"query": "needle", "results": [], "truncated": True}
    assert time.monotonic() - start < ops.RG_TIMEOUT_S  # the budget stopped it, not the timer
    assert gone(Path(corpus.rg).parent / "pid")


def test_search_kills_rg_once_it_has_its_results(
    corpus: Corpus, fake_rg: Callable[[str], str]
) -> None:
    corpus.rg = fake_rg("matches")
    found = corpus.call("corpus_search", {"query": "needle", "repo": A, "max_results": 3})
    assert [r["path"] for r in found["results"]] == ["f1.txt", "f2.txt", "f3.txt"]
    assert found["truncated"] is True
    assert gone(Path(corpus.rg).parent / "pid")


def test_search_drops_an_overlong_line(corpus: Corpus, fake_rg: Callable[[str], str]) -> None:
    """rg's JSON printer ignores --max-columns: a line past RG_LINE_MAX is read to its end
    and dropped, never held whole, and the search goes on, saying it isn't complete. A line
    longer than the whole output budget comes from its own allowance, so it ends nothing."""
    for mode in ("long", "huge"):
        corpus.rg = fake_rg(mode)
        found = corpus.call("corpus_search", {"query": "needle", "repo": A})
        assert found["results"] == [{"repo": A, "path": "f2.txt", "line": 2, "text": "needle"}]
        assert found["truncated"] is True, mode


def test_search_times_out_and_kills_rg(
    corpus: Corpus, fake_rg: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops, "RG_TIMEOUT_S", 0.5)
    corpus.rg = fake_rg("sleep")
    with pytest.raises(ToolError, match="timed out"):
        corpus.call("corpus_search", {"query": "needle", "repo": A})
    assert gone(Path(corpus.rg).parent / "pid")


def test_search_argv_bounds_rg(corpus: Corpus, fake_rg: Callable[[str], str]) -> None:
    corpus.rg = fake_rg("argv")
    corpus.call("corpus_search", {"query": "--files", "repo": A, "path_glob": "*.md"})
    argv = json.loads((Path(corpus.rg).parent / "argv").read_text())
    assert argv[argv.index("--threads") + 1] == "2"
    assert argv[argv.index("--max-filesize") + 1] == "5M"
    assert {"--no-config", "--no-follow", "--fixed-strings", "--json"} <= set(argv)
    assert argv[-3:] == ["--", "--files", "."]  # the query is never a flag


# -- the op cap, metrics writes ------------------------------------------------------------------
def test_ops_are_capped_per_answer(sockdir: Path, corpus: Corpus) -> None:
    """Op 201 of one answer is refused without running, audited as refused once a day and
    counted after; another answer is unaffected."""
    tick = Tick()
    ledger = Ledger(sockdir / "state", clock=tick)
    listing = {"name": "corpus_list", "arguments": {"repo": A}}
    lines = [rpc(n, "tools/call", listing) for n in range(1, broker.MAX_OPS_PER_ANSWER + 4)]
    replies = with_server(
        sockdir,
        op_server(corpus, ledger).handle,
        lambda path: converse(path, hello_line(), *lines),
    )
    refused = [r["result"]["isError"] for r in replies]
    assert refused == [False] * broker.MAX_OPS_PER_ANSWER + [True] * 3
    assert "used all 200 of its ops" in replies[-1]["result"]["content"][0]["text"]
    last = ledger.take(AID)[-1]
    assert (last["op"], last["ok"]) == ("corpus_list", False)
    m = ledger.metrics()[SERVER]
    assert m["ops"] == broker.MAX_OPS_PER_ANSWER and m["refused"]["ops"] == 3
    assert (SERVER, AID2) not in ledger.answers
    audit = sockdir / "state" / "audit" / f"{SERVER}.jsonl"

    def refusals() -> int:
        return sum(not json.loads(x)["ok"] for x in audit.read_text().splitlines())

    assert refusals() == 1
    tick.t += 86_400  # a new UTC day: the answer's next refusal is audited again
    ledger.refuse_op(SERVER, AID, POST, "corpus_list", {})
    ledger.refuse_op(SERVER, AID, POST, "corpus_list", {})
    assert refusals() == 2


def test_refusals_are_audited_once_a_day(tmp_path: Path) -> None:
    """A loop of refusals writes one line a kind a day, per answer, or per server with no
    record: the rest only count, so they can't fill the disk or stall the loop."""
    ledger = Ledger(tmp_path / "state", clock=Tick())
    ledger.hello(SERVER, AID, POST)
    turn_cap = model_call(turn=9, status=429, forwarded=False, input_tokens=0, output_tokens=0)
    for _ in range(5):
        ledger.model(SERVER, turn_cap)
        ledger.refuse_op(SERVER, AID, POST, "corpus_list", {})
        ledger.model(SERVER, model_call(AID2, turn=0, status=429, forwarded=False))
    audit = tmp_path / "state" / "audit" / f"{SERVER}.jsonl"
    lines = [json.loads(x) for x in audit.read_text().splitlines()]
    assert [(x["answer_id"], x["op"], x["ok"]) for x in lines] == [
        (AID, "model", False),
        (AID, "corpus_list", False),
        (AID2, "model", False),
    ]
    m = ledger.metrics()[SERVER]
    assert m["refused"] == {"hellos": 0, "ops": 5, "model_calls": 10}
    assert m["model_calls"] == 0 and m["output_tokens"] == 0  # a refused call spends nothing


def test_live_answers_are_capped_across_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """At most MAX_LIVE_ANSWERS answers hold lines at once, across every server: the memory
    limit is the process's. A hello past it is refused, logged once; a live answer is still
    served, and a quiet answer's place is freed at once, not at the next sweep."""
    monkeypatch.setattr(broker, "MAX_LIVE_ANSWERS", 3)
    caplog.set_level(logging.WARNING, logger="crewkiosk.broker")
    tick = Tick()
    ledger = Ledger(tmp_path / "state", clock=tick)
    assert ledger.hello(SERVER, aid(0), POST) and ledger.hello(SERVER, aid(1), POST)
    assert ledger.hello(OTHER, aid(2), POST)
    assert not ledger.hello(OTHER, aid(3), POST)
    assert not ledger.hello(SERVER, aid(4), POST)
    assert ledger.hello(SERVER, aid(0), POST)
    assert [r.getMessage() for r in caplog.records].count(
        "broker: 3 answers live: no more until one goes quiet"
    ) == 1
    assert ledger.metrics()[OTHER]["refused"]["hellos"] == 1
    tick.t += broker.AUDIT_TTL_S + 1
    ledger.swept = tick.t  # the sweep isn't due: the full ledger runs it itself
    assert ledger.hello(SERVER, aid(4), POST)
    assert list(ledger.answers) == [(SERVER, aid(4))]


def test_one_server_holds_at_most_its_share_of_live_answers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A kiosk minting ids on one server fills its MAX_LIVE_PER_SERVER, never the whole pool:
    its next hello is refused, logged once and audited once, while another server's new
    answer is still admitted."""
    caplog.set_level(logging.WARNING, logger="crewkiosk.broker")
    ledger = Ledger(tmp_path / "state", clock=Tick())
    share = broker.MAX_LIVE_PER_SERVER
    assert share < broker.MAX_LIVE_ANSWERS
    assert all(ledger.hello(SERVER, aid(n), POST) for n in range(share))
    assert not ledger.hello(SERVER, aid(1000), POST)
    assert not ledger.hello(SERVER, aid(1001), POST)
    assert ledger.hello(OTHER, aid(2000), POST)
    assert ledger.hello(SERVER, aid(0), POST)  # a live answer is still served
    assert [r.getMessage() for r in caplog.records].count(
        f"broker {SERVER}: {share} answers live: no more until one goes quiet"
    ) == 1
    assert ledger.metrics()[SERVER]["refused"]["hellos"] == 2
    audit = tmp_path / "state" / "audit" / f"{SERVER}.jsonl"
    assert [json.loads(x)["op"] for x in audit.read_text().splitlines()] == ["hello"]


def test_a_server_at_its_share_frees_a_quiet_answer_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server at its MAX_LIVE_PER_SERVER, with the pool far from MAX_LIVE_ANSWERS, frees a
    quiet answer's place on its next hello, not at the next sweep."""
    monkeypatch.setattr(broker, "MAX_LIVE_PER_SERVER", 2)
    tick = Tick()
    ledger = Ledger(tmp_path / "state", clock=tick)
    assert ledger.hello(SERVER, aid(0), POST) and ledger.hello(SERVER, aid(1), POST)
    assert not ledger.hello(SERVER, aid(2), POST)
    assert len(ledger.answers) < broker.MAX_LIVE_ANSWERS  # only the server's share is full
    tick.t += broker.AUDIT_TTL_S + 1
    ledger.swept = tick.t  # the sweep isn't due: the server's full share runs it itself
    assert ledger.hello(SERVER, aid(2), POST)
    assert list(ledger.answers) == [(SERVER, aid(2))]


@pytest.mark.parametrize("ending", ["raises", "cancelled"])
def test_an_op_that_ends_badly_gives_its_place_back(
    tmp_path: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """Whatever ends an op, an error it didn't expect or its session cancelled mid-op, its
    reserved place under MAX_OPS_PER_ANSWER is released: the cap never leaks shut."""
    monkeypatch.setattr(broker, "MAX_OPS_PER_ANSWER", 1)
    ledger = Ledger(tmp_path / "state", clock=Tick())
    op = OpServer(SERVER, corpus, ledger)
    started, gate = threading.Event(), threading.Event()

    def bad(name: str, args: object) -> dict[str, Any]:
        started.set()
        if ending == "raises":
            raise RuntimeError("boom")
        gate.wait(5)
        return {}

    params = {"name": "corpus_list", "arguments": {}}

    async def main() -> dict[str, Any]:
        monkeypatch.setattr(corpus, "call", bad)
        task = asyncio.ensure_future(op._call(params, AID, POST))
        if ending == "cancelled":
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
        with pytest.raises(RuntimeError if ending == "raises" else asyncio.CancelledError):
            await task
        gate.set()
        assert ledger.answers[(SERVER, AID)].pending == 0
        monkeypatch.setattr(corpus, "call", lambda name, args: {})
        return await op._call(params, AID, POST)

    assert asyncio.run(main())["isError"] is False  # the one op is still the answer's to run


def test_the_op_cap_holds_for_sessions_calling_at_once(
    sockdir: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An op's place under the cap is taken before it runs: one answer's four sessions,
    calling at once, run exactly MAX_OPS_PER_ANSWER ops, never a few more."""
    monkeypatch.setattr(broker, "MAX_OPS_PER_ANSWER", 5)
    ran: list[str] = []

    def slow(name: str, args: object) -> dict[str, Any]:
        ran.append(name)
        time.sleep(0.05)
        return {}

    monkeypatch.setattr(corpus, "call", slow)
    ledger = Ledger(sockdir / "state", clock=Tick())
    calls = [rpc(n, "tools/call", {"name": "corpus_list", "arguments": {}}) for n in range(3)]

    async def scenario(path: str) -> None:
        sessions = range(broker.MAX_PER_ANSWER)
        await asyncio.gather(*(converse(path, hello_line(), *calls) for _ in sessions))

    with_server(sockdir, op_server(corpus, ledger).handle, scenario)
    assert len(ran) == 5
    assert ledger.metrics()[SERVER]["refused"]["ops"] == 3 * broker.MAX_PER_ANSWER - 5


def test_a_stop_refuses_the_ops_queued_behind_it(
    sockdir: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close_all() ends the sessions, and an op still queued for a thread is refused, never
    run: a stop waits for the ops already running, and no more."""
    gate, ran = threading.Event(), []

    def held(name: str, args: object) -> dict[str, Any]:
        ran.append(name)
        gate.wait(5)
        return {}

    monkeypatch.setattr(corpus, "call", held)
    ledger = Ledger(sockdir / "state", clock=Tick())
    op = OpServer(SERVER, corpus, ledger, asyncio.Semaphore(1))
    done: list[int] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await op.handle(reader, writer)
        done.append(1)

    listing = rpc(1, "tools/call", {"name": "corpus_list", "arguments": {}})

    async def until(cond: Callable[[], bool]) -> None:
        for _ in range(500):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")

    async def scenario(path: str) -> None:
        first = asyncio.ensure_future(converse(path, hello_line(aid(1)), listing))
        await until(lambda: len(ran) == 1)
        second = asyncio.ensure_future(converse(path, hello_line(aid(2)), listing))
        await until(lambda: op.slots.total == 2)
        await asyncio.sleep(0.05)  # the second op waits on the semaphore
        op.slots.close_all()
        gate.set()
        await until(lambda: len(done) == 2)
        await asyncio.gather(first, second, return_exceptions=True)

    with_server(sockdir, handle, scenario)
    assert ran == ["corpus_list"]


def test_the_flush_sweeps_a_quiet_ledger(tmp_path: Path) -> None:
    """With nothing more coming in, the timer's flush still drops the lines of an answer
    quiet past AUDIT_TTL_S."""
    tick = Tick()
    ledger = Ledger(tmp_path / "state", clock=tick)
    ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
    tick.t += broker.AUDIT_TTL_S + 1
    ledger.flush()
    assert ledger.lines == {} and ledger.answers == {}


def test_metrics_are_written_at_most_once_an_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """However many lines come, a day's metrics file is written once per METRICS_WRITE_S;
    flush() writes only a day that is behind, and a day that ends is written once more."""
    tick = Tick()
    ledger = Ledger(tmp_path / "state", clock=tick)
    writes: list[tuple[str, str]] = []
    real = ledger._write_metrics

    def counted(server: str, date: str, day: broker.Day) -> None:
        writes.append((server, date))
        real(server, date, day)

    monkeypatch.setattr(ledger, "_write_metrics", counted)
    for n in range(broker.LATENCY_SAMPLE + 10):
        ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, n, True)
    assert len(writes) == 1
    day = ledger.days[(SERVER, "2026-10-30")]
    assert len(day.op_latency_ms) == broker.LATENCY_SAMPLE  # a bounded sample, the latest
    tick.t += broker.METRICS_WRITE_S - 1
    ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
    assert len(writes) == 1
    tick.t += 1
    ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
    assert len(writes) == 2
    ledger.flush()
    assert len(writes) == 2  # nothing behind
    ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
    ledger.flush()
    assert len(writes) == 3
    on_disk = tmp_path / "state" / "metrics" / f"{SERVER}-2026-10-30.json"
    assert json.loads(on_disk.read_text())["ops"] == broker.LATENCY_SAMPLE + 13
    # The day's last line, after its last write, still reaches its file when the day ends.
    ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
    tick.t += 86_400
    ledger.op(SERVER, AID2, POST, "corpus_list", {}, 1, 1, True)
    assert json.loads(on_disk.read_text())["ops"] == broker.LATENCY_SAMPLE + 14


# -- hostile input -------------------------------------------------------------------------------
def test_deep_nesting_is_refused_not_raised(sockdir: Path, corpus: Corpus) -> None:
    """JSON nested past the parser's depth is a RecursionError, not a ValueError: each parser
    refuses it as malformed, and the session goes on."""
    deep = b"[" * 100_000 + b"]" * 100_000 + b"\n"
    assert broker._hello(deep) is None
    replies = with_server(
        sockdir,
        op_server(corpus).handle,
        lambda path: converse(path, hello_line(), deep, rpc(1, "ping")),
    )
    assert replies[0]["error"]["code"] == -32700 and replies[1]["result"] == {}
    control = Control(Ledger(sockdir / "state", clock=Tick()), os.getuid())
    assert control.answer(deep)["ok"] is False


def test_refusals_echo_only_cleaned_names(
    sockdir: Path, corpus: Corpus, caplog: pytest.LogCaptureFixture
) -> None:
    """A tool or argument name the kiosk chose is cut to 64 printable ASCII characters
    wherever it is echoed: the refusal, the log line and the audit line."""
    evil = "x\nINJECTED " + "é" * 100
    with pytest.raises(ToolError) as tool:
        corpus.call(evil, {})
    with pytest.raises(ToolError) as arg:
        corpus.call("corpus_list", {"repo": A, evil: 1})
    for exc in (tool.value, arg.value):
        assert "\n" not in str(exc) and "é" not in str(exc)
        assert len(str(exc)) < 64 + 30
    caplog.set_level(logging.INFO, logger="crewkiosk.broker")
    ledger = Ledger(sockdir / "state", clock=Tick())
    call(sockdir, op_server(corpus, ledger), evil, {})
    messages = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert messages and all("\n" not in m and "é" not in m for m in messages)
    [line] = ledger.take(AID)
    assert line["op"] == ops.printable(evil) and len(line["op"]) == 64


# -- reloads -------------------------------------------------------------------------------------
def broker_for(tmp_path: Path, sockdir: Path, root: Path, *servers: str) -> tuple[Broker, Path]:
    pool = tmp_path / "pool"
    pool.mkdir(exist_ok=True)
    for server in servers:
        write_manifest(pool, server, repos=[A])
    run = sockdir / "run"
    run.mkdir(exist_ok=True)
    b = Broker(
        pool_dir=pool,
        corpus_root=root,
        runtime_dir=run,
        control_path=sockdir / "ctl" / "control.sock",
        state_dir=tmp_path / "state",
        model_conf=tmp_path / "model.conf",
        model_key=tmp_path / "none",
        socket_group=None,
        control_uid=os.getuid(),
    )
    return b, run


def test_one_failing_server_never_stops_the_others(
    tmp_path: Path, sockdir: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server whose second socket can't be set up is never registered half-open: its first
    socket is closed again, the others serve, and the next reload tries it afresh."""
    b, run = broker_for(tmp_path, sockdir, root, SERVER, OTHER)
    real_own = b._own

    def own(path: Path) -> None:
        if path.name == f"{OTHER}.model.sock":
            raise PermissionError("chown refused")
        real_own(path)

    async def main() -> None:
        monkeypatch.setattr(b, "_own", own)
        await b.reload()
        assert set(b.sites) == {SERVER}
        assert sorted(p.name for p in run.iterdir()) == ["dev.model.sock", "dev.sock"]
        replies = await converse(str(run / "dev.sock"), hello_line(), rpc(1, "ping"))
        assert replies == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
        monkeypatch.setattr(b, "_own", real_own)
        await b.reload()
        assert set(b.sites) == {SERVER, OTHER}
        await b.stop()

    asyncio.run(main())


def test_reloads_are_serialised_and_none_follows_a_stop(
    tmp_path: Path, sockdir: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b, _run = broker_for(tmp_path, sockdir, root, SERVER)
    real_open = b._open
    active, peak = [0], [0]

    async def tracked(*args: Any) -> None:
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        try:
            await asyncio.sleep(0.02)
            await real_open(*args)
        finally:
            active[0] -= 1

    async def main() -> None:
        monkeypatch.setattr(b, "_open", tracked)
        await asyncio.gather(b.reload(), b.reload(), b.reload())
        assert peak[0] == 1 and len(b.sites[SERVER].servers) == 2
        await b.stop()
        await b.reload()  # a SIGHUP that waited on the stop
        assert b.sites == {}

    asyncio.run(main())


def test_server_ids_too_long_for_a_socket_path_are_skipped(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    runtime = Path("/" + "d" * 89)  # 90 bytes: "/a.model.sock" fits, a longer id does not
    for server in ("a", "abcdefgh"):
        write_manifest(pool, server, repos=[A])
    b = Broker(pool_dir=pool, corpus_root=tmp_path, runtime_dir=runtime, socket_group=None)
    assert set(b.manifests()) == {"a"}


# -- the ledger's memory is bounded --------------------------------------------------------------
def aid(n: int) -> str:
    return f"{n:032x}"


def test_answers_a_day_are_capped(
    sockdir: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Past MAX_ANSWERS_PER_DAY a server's new hello is refused, logged once, audited ok:false
    once a day and counted after, and makes no record; a known answer is still served, and a
    model call past the cap still counts in the day."""
    monkeypatch.setattr(broker, "MAX_ANSWERS_PER_DAY", 3)
    caplog.set_level(logging.WARNING, logger="crewkiosk.broker")
    ledger = Ledger(sockdir / "state", clock=Tick())
    assert all(ledger.hello(SERVER, aid(n), POST) for n in range(3))
    assert not ledger.hello(SERVER, aid(3), POST)
    assert not ledger.hello(SERVER, aid(4), POST)
    assert ledger.hello(SERVER, aid(0), POST)  # already today's: never refused
    assert ledger.hello(OTHER, aid(3), POST)  # the cap is per server
    day = ledger.days[(SERVER, "2026-10-30")]
    assert set(day.answers) == {aid(n) for n in range(3)}
    assert (SERVER, aid(3)) not in ledger.answers
    assert [r.getMessage() for r in caplog.records].count(
        f"broker {SERVER}: 3 answers today: no more until midnight UTC"
    ) == 1
    audit = sockdir / "state" / "audit" / f"{SERVER}.jsonl"
    refused = [json.loads(x) for x in audit.read_text().splitlines()]
    assert [(x["op"], x["answer_id"], x["ok"]) for x in refused] == [("hello", aid(3), False)]
    ledger.model(SERVER, model_call(aid(9), input_tokens=7))
    assert (SERVER, aid(9)) not in ledger.answers
    m = ledger.metrics()[SERVER]
    assert m["answers"] == 3 and m["model_calls"] == 1 and m["input_tokens"] == 7
    assert m["refused"]["hellos"] == 2
    # Through the socket: a refused hello is closed with no reply.
    replies = with_server(
        sockdir,
        op_server(corpus, ledger).handle,
        lambda path: converse(path, hello_line(aid(5)), rpc(1, "ping")),
    )
    assert replies == []


def test_the_op_socket_holds_at_most_max_connections(
    sockdir: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The op socket holds MAX_CONNECTIONS connections, each one answer: the 65th is closed
    before it can make one. (The ledger's share of live answers is lifted out of the way.)"""
    monkeypatch.setattr(broker, "MAX_LIVE_PER_SERVER", broker.MAX_CONNECTIONS + 1)
    ledger = Ledger(sockdir / "state", clock=Tick())
    op = op_server(corpus, ledger)

    async def scenario(path: str) -> list[dict[str, Any]]:
        held = []
        for n in range(broker.MAX_CONNECTIONS):
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(hello_line(aid(n)) + rpc(1, "ping"))
            await writer.drain()
            assert json.loads(await reader.readline())["result"] == {}
            held.append(writer)
        refused = await converse(path, hello_line(aid(999)), rpc(1, "ping"))
        for writer in held:
            writer.close()
        return refused

    assert with_server(sockdir, op.handle, scenario) == []
    assert len(ledger.days[(SERVER, "2026-10-30")].answers) == broker.MAX_CONNECTIONS


def test_ops_share_a_bounded_thread_pool(
    sockdir: Path, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """However many sessions call at once, at most OPS_AT_ONCE ops run in threads: the
    default executor also resolves the model proxy's upstream."""
    lock, running, peak = threading.Lock(), [0], [0]

    def slow(name: str, args: object) -> dict[str, Any]:
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.1)
        with lock:
            running[0] -= 1
        return {}

    monkeypatch.setattr(corpus, "call", slow)
    op = op_server(corpus)
    listing = {"name": "corpus_list", "arguments": {"repo": A}}

    async def scenario(path: str) -> None:
        await asyncio.gather(
            *(converse(path, hello_line(aid(n)), rpc(1, "tools/call", listing)) for n in range(10))
        )

    with_server(sockdir, op.handle, scenario)
    assert peak[0] == broker.OPS_AT_ONCE


def test_a_new_answer_reaches_the_metrics_file(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state", clock=Tick())
    ledger.hello(SERVER, AID, POST)
    ledger.flush()
    on_disk = tmp_path / "state" / "metrics" / f"{SERVER}-2026-10-30.json"
    assert json.loads(on_disk.read_text())["answers"] == 1


def test_a_line_goes_to_its_own_days_file(tmp_path: Path) -> None:
    """A line keyed on a day that the clock has since left is written to that day's file,
    never to today's."""
    ledger = Ledger(tmp_path / "state", clock=Tick())  # 2026-10-30
    day = broker.Day("2026-10-29")
    ledger._line(SERVER, AID, POST, day, None, "corpus_list", {}, 1, 1, True)
    metrics = tmp_path / "state" / "metrics"
    assert sorted(p.name for p in metrics.iterdir()) == [f"{SERVER}-2026-10-29.json"]


def test_the_metrics_flusher_survives_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker, "METRICS_WRITE_S", 0.01)
    calls: list[int] = []

    class Failing:
        def flush(self) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")

    async def main() -> None:
        task = asyncio.ensure_future(broker.flush_metrics(Failing()))  # type: ignore[arg-type]
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(main())
    assert len(calls) >= 2


def test_stop_expires_everything_and_survives_a_failing_server(
    tmp_path: Path, sockdir: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop() drops every answer's lines whatever the sweep's last run, still closes the
    control socket and writes the metrics when one server's close fails."""
    b, _run = broker_for(tmp_path, sockdir, root, SERVER, OTHER)
    real_close = b._close

    async def close(server: str) -> None:
        await real_close(server)
        if server == SERVER:
            raise OSError("unlink refused")

    async def main() -> None:
        await b.reload()
        await b.start_control(None)
        # The first line writes the metrics file; the second, inside METRICS_WRITE_S, leaves
        # it behind, for the stop to bring up to date.
        b.ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
        b.ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
        monkeypatch.setattr(b, "_close", close)
        await b.stop()
        assert b.control is not None and not b.control.is_serving()

    asyncio.run(main())
    assert b.sites == {} and b.ledger.answers == {} and b.ledger.lines == {}
    # The broker's clock is the real one: whatever today is, its file was written.
    [metrics] = (tmp_path / "state" / "metrics").glob(f"{SERVER}-*.json")
    assert json.loads(metrics.read_text())["ops"] == 2


def test_stop_writes_the_metrics_when_the_control_socket_fails(
    tmp_path: Path, sockdir: Path, root: Path
) -> None:
    b, _run = broker_for(tmp_path, sockdir, root, SERVER)

    class Broken:
        def close(self) -> None:
            raise OSError("close refused")

    async def main() -> None:
        await b.reload()
        b.ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
        b.ledger.op(SERVER, AID, POST, "corpus_list", {}, 1, 1, True)
        b.control = Broken()  # type: ignore[assignment]
        with pytest.raises(OSError):
            await b.stop()

    asyncio.run(main())
    [metrics] = (tmp_path / "state" / "metrics").glob(f"{SERVER}-*.json")
    assert json.loads(metrics.read_text())["ops"] == 2
