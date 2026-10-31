"""IT-30 (#141): crewkiosk-broker end to end, through crewkiosk-relay.

A real broker serves a temp runtime dir over a fixture corpus, two research servers with their
own pools, and a stub model endpoint on 127.0.0.1. Everything the kiosk would do goes through
the relay, as it would in the kiosk's namespace: MCP over `relay mcp`, model calls over
`relay http`. The daemon's side goes through KioskBrokerClient on the control socket. No
Mattermost: it needs only rg, which CI installs (scripts/ci/rg-install.sh).
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any, ClassVar

import pytest
from crewchat.kiosk_broker import KioskBrokerClient
from crewkiosk import relay
from crewkiosk.broker import Broker
from crewkiosk.proxy import Upstream

from tests.fakes.forge import write_manifest

DEV, OTHER = "dev", "other"
A, B, C = "forge.test/org/a", "forge.test/org/b", "forge.test/org/c"
AID = "0123456789abcdef0123456789abcdef"
AID2 = "fedcba9876543210fedcba9876543210"
POST = "post-it30"
KEY = "sk-it30-broker-key"
COMMIT = "c" * 40
RELAY = Path(relay.__file__)


@pytest.fixture
def rg() -> str:
    path = shutil.which("rg")
    if path is None:
        if os.environ.get("CREWKIOSK_RG_REQUIRED") == "1":
            pytest.fail("CREWKIOSK_RG_REQUIRED=1, and there is no rg on PATH")
        pytest.skip("no rg on PATH (CI installs it: scripts/ci/rg-install.sh)")
    return path


class Model(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, dict[str, str], dict[str, Any]]]] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        out = json.dumps({"usage": {"input_tokens": 20, "output_tokens": 4}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: Any) -> None:
        pass


def tree(root: Path, server: str, files: dict[str, str]) -> None:
    gen = root / server / "gen-1"
    for rel, body in files.items():
        (gen / rel).parent.mkdir(parents=True, exist_ok=True)
        (gen / rel).write_text(body)
    (root / server / "current").symlink_to("gen-1")


@pytest.fixture
def stack(tmp_path: Path, rg: str) -> Iterator[dict[str, Any]]:
    """A running broker, its stub model, and the paths a kiosk and the daemon would use."""
    root = tmp_path / "corpus"
    tree(root, DEV, {f"{A}/README.md": "alpha needle\n", f"{B}/docs/b.md": "beta needle\n"})
    tree(root, OTHER, {f"{C}/c.txt": "gamma needle\n"})
    (root / "status").mkdir()
    (root / "status" / f"{DEV}.json").write_text(json.dumps({"repos": {A: {"commit": COMMIT}}}))
    pool = tmp_path / "pool"
    pool.mkdir()
    write_manifest(pool, DEV, repos=[A, B], max_turns=2)
    write_manifest(pool, OTHER, repos=[C])
    key = tmp_path / "broker-model.credential"
    key.write_text(KEY + "\n")
    key.chmod(0o600)
    (tmp_path / "model.credential").write_text("sk-the-runners-key\n")  # never read

    Model.seen = []
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    run = Path(tempfile.mkdtemp(prefix="it30-"))  # short: Unix socket paths
    control = Path(tempfile.mkdtemp(prefix="it30c-")) / "control.sock"  # its own dir, as in prod
    broker = Broker(
        pool_dir=pool,
        corpus_root=root,
        runtime_dir=run,
        control_path=control,
        state_dir=tmp_path / "state",
        model_conf=tmp_path / "model.conf",
        model_key=key,
        socket_group=None,
        control_uid=os.getuid(),
        upstream=Upstream("http", "127.0.0.1", model.server_address[1], ""),
        rg=rg,
    )
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    asyncio.run_coroutine_threadsafe(broker.reload(), loop).result(30)
    asyncio.run_coroutine_threadsafe(broker.start_control(None), loop).result(30)
    try:
        yield {"run": run, "control": control, "state": tmp_path / "state", "broker": broker}
    finally:
        asyncio.run_coroutine_threadsafe(broker.stop(), loop).result(30)
        loop.call_soon_threadsafe(loop.stop)
        model.shutdown()
        shutil.rmtree(run, ignore_errors=True)
        shutil.rmtree(control.parent, ignore_errors=True)


class Session:
    """The kiosk's MCP client: one `crewkiosk-relay mcp` process, newline-delimited JSON-RPC."""

    def __init__(self, sock: Path, answer_id: str = AID) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(RELAY), "mcp", str(sock), answer_id, POST],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self.next_id = 0

    def ask(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        stdin: IO[bytes] | None = self.proc.stdin
        stdout: IO[bytes] | None = self.proc.stdout
        assert stdin is not None and stdout is not None
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        stdin.write(json.dumps(msg).encode() + b"\n")
        stdin.flush()
        reply: dict[str, Any] = json.loads(stdout.readline())
        assert reply["id"] == self.next_id  # nothing else, not the hello, came first
        return reply

    def tool(self, name: str, **args: Any) -> tuple[bool, Any]:
        result = self.ask("tools/call", {"name": name, "arguments": args})["result"]
        text = result["content"][0]["text"]
        return (not result["isError"]), (json.loads(text) if not result["isError"] else text)

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def http_relay(sock: Path) -> tuple[subprocess.Popen[bytes], int]:
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-I", str(RELAY), "http", str(sock), str(port)])
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=5).close()
            return proc, port
        except OSError:
            assert time.monotonic() < deadline, "the relay never listened"
            time.sleep(0.05)


def message(port: int, answer_id: str, model: str = "claude-opus-whatever") -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    body = json.dumps({"model": model, "max_tokens": 1, "messages": []})
    conn.request(
        "POST",
        f"/a/{answer_id}/v1/messages",
        body,
        {"content-type": "application/json", "x-api-key": "sk-from-the-kiosk"},
    )
    reply = conn.getresponse()
    reply.read()
    conn.close()
    return reply.status


def test_it30_broker_end_to_end(stack: dict[str, Any]) -> None:
    run: Path = stack["run"]
    assert sorted(p.name for p in run.iterdir()) == [
        "dev.model.sock",
        "dev.sock",
        "other.model.sock",
        "other.sock",
    ]

    # -- the ops, scoped by the socket -------------------------------------------------------
    dev = Session(run / "dev.sock")
    init = dev.ask("initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == "crewkiosk-broker"
    names = [t["name"] for t in dev.ask("tools/list")["result"]["tools"]]
    assert names == ["corpus_search", "corpus_read", "corpus_list"]
    ok, found = dev.tool("corpus_search", query="needle")
    assert ok and {(r["repo"], r["path"]) for r in found["results"]} == {
        (A, "README.md"),
        (B, "docs/b.md"),
    }
    ok, read = dev.tool("corpus_read", repo=A, path="README.md")
    assert ok and read["commit"] == COMMIT and read["lines"] == [{"n": 0, "text": "alpha needle"}]
    ok, listed = dev.tool("corpus_list", repo=B)
    assert ok and listed["paths"] == ["docs/b.md"]
    for name, args in (
        ("corpus_list", {"repo": C}),  # the other server's pool
        ("corpus_search", {"query": "needle", "repo": C}),
        ("corpus_read", {"repo": A, "path": "../b/docs/b.md"}),
        ("corpus_read", {"repo": A, "path": "/etc/passwd"}),
        ("corpus_read", {"repo": A, "path": "README.md", "limit": 5000}),
    ):
        ok, why = dev.tool(name, **args)
        assert not ok, (name, args, why)
    dev.close()

    other = Session(run / "other.sock", AID2)
    ok, found = other.tool("corpus_search", query="needle")
    assert ok and [r["repo"] for r in found["results"]] == [C]
    assert not other.tool("corpus_read", repo=A, path="README.md")[0]
    other.close()

    # -- no hello, no session: the broker closes before answering anything -------------------
    with socket.socket(socket.AF_UNIX) as raw:
        raw.connect(str(run / "dev.sock"))
        raw.sendall(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
        raw.settimeout(10)
        assert raw.recv(65536) == b""

    # -- the model, through the http relay ---------------------------------------------------
    proc, port = http_relay(run / "dev.model.sock")
    try:
        assert [message(port, AID) for _ in range(3)] == [200, 200, 429]  # max_turns 2
        # AID2 said hello on the other server: on this one it was never admitted (#142).
        assert message(port, AID2) == 403
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert len(Model.seen) == 2  # the 429 and the 403 never left the broker
    for path, headers, body in Model.seen:
        assert path == "/v1/messages"
        assert headers["x-api-key"] == KEY  # the broker's key, never the kiosk's or the runner's
        assert body["model"] == "claude-haiku-5-5"  # pinned: no model.conf, the default

    # -- the daemon's side: audit by answer, then forgotten; metrics -------------------------
    client = KioskBrokerClient(str(stack["control"]))
    lines = asyncio.run(client.audit(AID))
    ops = [(x["server"], x["op"], x["ok"]) for x in lines]
    assert ops == [
        (DEV, "corpus_search", True),
        (DEV, "corpus_read", True),
        (DEV, "corpus_list", True),
        *[(DEV, name, False) for name in ("corpus_list", "corpus_search", *["corpus_read"] * 3)],
        (DEV, "model", True),
        (DEV, "model", True),
        (DEV, "model", False),
    ]
    assert {x["post_id"] for x in lines} == {POST}
    assert [x["args"]["turn"] for x in lines if x["op"] == "model"] == [1, 2, 3]
    assert asyncio.run(client.audit(AID)) == []
    metrics = asyncio.run(client.metrics())
    assert metrics is not None
    assert metrics[DEV]["ops"] == 8 and metrics[DEV]["model_calls"] == 2  # AID's two
    # AID2's 403 made no record here: one answer on dev, and the 403 only counted as refused.
    assert metrics[DEV]["input_tokens"] == 40 and metrics[DEV]["answers"] == 1
    assert metrics[DEV]["refused"]["model_calls"] == 2  # the 429 and the 403
    assert metrics[OTHER]["ops"] == 2
    audit = stack["state"] / "audit" / f"{DEV}.jsonl"
    assert len(audit.read_text().splitlines()) == 12  # every line, also on disk
