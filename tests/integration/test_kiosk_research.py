"""IT-31 (#142): a real kiosk answers a pooled question entirely through crewkiosk-broker.

The daemon's side is real: a research `KioskSession` frames the answer to its server's research
socket, then reads the broker's audit for it over the control socket. Here a test thread
stands in for systemd's `Accept=yes`: for each connection it starts the real
`packaging/crewkiosk-research-run`, its fixed paths pointed at a temp dir, with that connection
as its stdin and stdout. The runner says hello, starts the real relay, and runs the real
`claude` CLI with no native tools and a strict MCP config naming only the relay. The model is a
stub behind the broker's proxy, scripted as a researcher would be: `corpus_search`, then a
`corpus_read` in each of two repos, then an answer built only from what those tool results
said. So every fact in the answer came through the broker.

No confinement here: IT-31b proves the unit's walls. It needs the `claude` binary and rg, and
skips without either (CI has no Claude Code: scripts/ci/kiosk-up.sh's claude is a stub).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from crewchat.adapters.base import InboundMessage
from crewchat.kiosk import BriefStore, ClaudeCliRunner, KioskSession, Research
from crewchat.kiosk_broker import KioskBrokerClient
from crewkiosk import relay
from crewkiosk.broker import Broker
from crewkiosk.proxy import Upstream

from tests.fakes.forge import write_manifest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "packaging" / "crewkiosk-research-run"
DEV, EXPERT = "dev", "babel"
A, B = "forge.test/org/a", "forge.test/org/b"
COMMIT_A, COMMIT_B = "a" * 40, "b" * 40
STUB_MARK = "crewkiosk-ci-stub"


@pytest.fixture
def claude() -> str:
    path = os.environ.get("CREWKIOSK_CLAUDE") or shutil.which("claude")
    if path is None or STUB_MARK in Path(path).read_text(errors="replace")[:4096]:
        pytest.skip("no claude binary (IT-31 needs the real CLI)")
    return path


@pytest.fixture
def rg() -> str:
    path = shutil.which("rg")
    if path is None:
        if os.environ.get("CREWKIOSK_RG_REQUIRED") == "1":
            pytest.fail("CREWKIOSK_RG_REQUIRED=1, and there is no rg on PATH")
        pytest.skip("no rg on PATH (CI installs it: scripts/ci/rg-install.sh)")
    return path


def text_of(block: dict[str, Any]) -> str:
    content = block.get("content", "")
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


class Model(BaseHTTPRequestHandler):
    """The upstream Messages API, scripted. A call with the corpus tools is one step of the
    research: search, read a, read b, then answer from the tool results alone. Anything else
    (the CLI's own side calls) gets a short text."""

    calls: ClassVar[list[dict[str, Any]]] = []
    # Set, the model is hostile instead: it calls these tools the CLI never offered, one a turn,
    # then answers with what each tool result said.
    rogue: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self.do_HEAD()

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))) or b"{}")
        if self.path.split("?")[0].endswith("/count_tokens"):
            return self.json({"input_tokens": 10})
        self.calls.append(body)
        tools = {t.get("name", ""): t for t in body.get("tools", [])}
        name = {
            short: n
            for n in tools
            for short in ("corpus_search", "corpus_read")
            if n.endswith(short)
        }
        results = [
            text_of(block)
            for msg in body.get("messages", [])
            if msg.get("role") == "user" and isinstance(msg.get("content"), list)
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        if "corpus_search" not in name:
            return self.reply(body, {"type": "text", "text": "ok"}, "end_turn")
        if self.rogue:
            if len(results) < len(self.rogue):
                tool, args = self.rogue[len(results)]
                block = {
                    "type": "tool_use",
                    "id": f"toolu_{len(results)}",
                    "name": tool,
                    "input": args,
                }
                return self.reply(body, block, "tool_use")
            said = " | ".join(r.replace("\n", " ")[:200] for r in results)
            return self.reply(body, {"type": "text", "text": f"ROGUE: {said}"}, "end_turn")
        steps = [
            (name["corpus_search"], {"query": "needle"}),
            (name["corpus_read"], {"repo": A, "path": "README.md"}),
            (name["corpus_read"], {"repo": B, "path": "docs/b.md"}),
        ]
        if len(results) < len(steps):
            tool, args = steps[len(results)]
            block = {"type": "tool_use", "id": f"toolu_{len(results)}", "name": tool, "input": args}
            return self.reply(body, block, "tool_use")
        # The answer, from what the two reads returned and nothing else.
        cites = []
        for repo, path, said in ((A, "README.md", results[1]), (B, "docs/b.md", results[2])):
            fact = re.search(r"(alpha|beta) needle", said)
            commit = re.search(r'"commit"\s*:\s*"([0-9a-f]{40})"', said)
            cites.append(
                f"{repo} {path} says {fact.group(0) if fact else '?'}"
                f" (as of {commit.group(1) if commit else '?'})"
            )
        return self.reply(body, {"type": "text", "text": "; ".join(cites) + "."}, "end_turn")

    def reply(self, req: dict[str, Any], block: dict[str, Any], stop: str) -> None:
        model = req.get("model", "stub")
        usage = {"input_tokens": 20, "output_tokens": 4}
        if not req.get("stream"):
            msg = {"id": "msg_stub", "type": "message", "role": "assistant", "model": model}
            msg |= {"content": [block], "stop_reason": stop, "stop_sequence": None, "usage": usage}
            return self.json(msg)
        start = dict(block, input={}) if block["type"] == "tool_use" else dict(block, text="")
        delta = (
            {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            if block["type"] == "tool_use"
            else {"type": "text_delta", "text": block["text"]}
        )
        message = {"id": "msg_stub", "type": "message", "role": "assistant", "model": model}
        message |= {"content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
        events = [
            ("message_start", {"type": "message_start", "message": message}),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": start},
            ),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        out = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def json(self, obj: dict[str, Any]) -> None:
        out = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: Any) -> None:
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def stack(tmp_path: Path, rg: str, claude: str) -> Iterator[dict[str, Any]]:
    """The broker over a two-repo pool, its scripted model, and the research socket, served
    by the real runner one connection at a time, as systemd's Accept=yes would."""
    corpus = tmp_path / "corpus"
    gen = corpus / DEV / "gen-1"
    for rel, body in {
        f"{A}/README.md": "alpha needle\n",
        f"{B}/docs/b.md": "beta needle\n",
    }.items():
        (gen / rel).parent.mkdir(parents=True, exist_ok=True)
        (gen / rel).write_text(body)
    (corpus / DEV / "current").symlink_to("gen-1")
    (corpus / "status").mkdir()
    status = {"repos": {A: {"commit": COMMIT_A}, B: {"commit": COMMIT_B}}}
    (corpus / "status" / f"{DEV}.json").write_text(json.dumps(status))
    pool = tmp_path / "pool"
    pool.mkdir()
    write_manifest(pool, DEV, repos=[A, B], max_turns=12)
    key = tmp_path / "broker-model.credential"
    key.write_text("sk-it31-broker-key\n")
    key.chmod(0o600)

    Model.calls = []
    Model.rogue = []
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    short = Path(tempfile.mkdtemp(prefix="it31-"))  # short: Unix socket paths
    run, research, control = short / "b", short / "r", short / "c" / "control.sock"
    for d in (run, research, control.parent):
        d.mkdir()
    broker = Broker(
        pool_dir=pool,
        corpus_root=corpus,
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

    # The runner and the relay, as installed, with their fixed paths pointed here.
    relay_exe = short / "crewkiosk-relay"
    shutil.copy(relay.__file__, relay_exe)
    relay_exe.chmod(0o755)
    text = RUNNER.read_text()
    for name, value in (
        ("CLAUDE", claude),
        ("RELAY", str(relay_exe)),
        ("RESEARCH_DIR", str(research)),
        ("BROKER_DIR", str(run)),
        ("RELAY_PORT", str(free_port())),
    ):
        text, n = re.subn(rf"^{name}=.*$", f"{name}={value}", text, flags=re.M)
        assert n == 1, name
    runner = short / "crewkiosk-research-run"
    runner.write_text(text)
    runner.chmod(0o755)

    journal: list[bytes] = []
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(research / f"research-{DEV}.sock"))
    listener.listen(4)

    def accept() -> None:  # systemd's Accept=yes, and the drop-in's argv[1] and CREWKIOSK_UID
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            env = {"PATH": os.environ["PATH"], "CREWKIOSK_UID": str(os.getuid())}
            with conn:
                proc = subprocess.run(
                    [str(runner), DEV], stdin=conn, stdout=conn, stderr=subprocess.PIPE, env=env
                )
            journal.append(proc.stderr)

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield {"research": research, "control": control, "journal": journal}
    finally:
        listener.close()
        asyncio.run_coroutine_threadsafe(broker.stop(), loop).result(30)
        loop.call_soon_threadsafe(loop.stop)
        model.shutdown()
        shutil.rmtree(short, ignore_errors=True)


# The CLI's one non-relay tool entry: an empty, inert schema ("never call this tool") that keeps
# its deferred tool loading on. It grants nothing; test_it31_no_native_tool_runs calls it.
PLACEHOLDER = "DeferredToolPlaceholder"


def research_session(
    stack: dict[str, Any], tmp_path: Path
) -> tuple[KioskSession, KioskBrokerClient]:
    client = KioskBrokerClient(str(stack["control"]))
    research = Research(
        ClaudeCliRunner(research_dir=str(stack["research"]), timeout_s=180), client, lambda: A
    )
    briefs = BriefStore({EXPERT: "We build forge.test/org/a and forge.test/org/b."})
    session = KioskSession(
        EXPERT,
        DEV,
        ClaudeCliRunner(),
        briefs=briefs,
        kiosk_dir=tmp_path / "kiosk",
        research=research,
    )
    return session, client


QUESTION = InboundMessage(
    backend=f"mattermost:{DEV}",
    channel="town-square",
    post_id="post1",
    root_id="",
    author_id="outsider1",
    author_name="visitor",
    text="@babel what do a and b say?",
)


def test_it31_kiosk_answers_from_two_pooled_repos(stack: dict[str, Any], tmp_path: Path) -> None:
    session, client = research_session(stack, tmp_path)
    question = QUESTION
    answer = asyncio.run(session.ask(question, [question]))
    journal = b"".join(stack["journal"]).decode(errors="replace")

    # Cited, both repos, and every fact came from a tool result the broker served.
    assert f"{A} README.md says alpha needle (as of {COMMIT_A})" in answer.text, (answer, journal)
    assert f"{B} docs/b.md says beta needle (as of {COMMIT_B})" in answer.text, answer
    researched = [
        c
        for c in Model.calls
        if any(t["name"].endswith("corpus_search") for t in c.get("tools", []))
    ]
    assert researched, "the CLI never offered the model the corpus tools"
    # The kiosk had no native tools: every tool offered was the relay's, or the inert placeholder.
    offered = {t["name"] for c in researched for t in c["tools"]}
    assert {n for n in offered if not n.startswith("mcp__kiosk__")} <= {PLACEHOLDER}, offered
    assert {n for n in offered if n.startswith("mcp__kiosk__")} == {
        "mcp__kiosk__corpus_search",
        "mcp__kiosk__corpus_read",
        "mcp__kiosk__corpus_list",
    }

    # The broker's audit for this answer: one line per op, and one per model call, in the
    # transcript record.
    assert answer.research is not None
    ops = [(line["op"], line["args"].get("repo")) for line in answer.research]
    corpus = [op for op in ops if op[0] != "model"]
    assert corpus == [("corpus_search", None), ("corpus_read", A), ("corpus_read", B)], ops
    assert ops.count(("model", None)) == len(researched)
    assert all(line["ok"] and line["bytes"] > 0 for line in answer.research)
    session.record(question, answer)
    record = json.loads((tmp_path / "kiosk" / f"{EXPERT}-{DEV}.jsonl").read_text())
    assert record["research"] == answer.research

    # And the overhead metrics `health` reports for the server.
    metrics = asyncio.run(client.metrics())
    assert metrics is not None and DEV in metrics, metrics


def test_it31_no_native_tool_runs(stack: dict[str, Any], tmp_path: Path) -> None:
    """A hostile model calls the CLI's native tools by name, and the placeholder: none runs.
    Each comes back an error, nothing is written, and the broker audits no op at all."""
    marker = tmp_path / "rogue-was-here"
    Model.rogue = [
        ("Bash", {"command": f"touch {marker}", "description": "x"}),
        ("Write", {"file_path": str(marker), "content": "x"}),
        ("Read", {"file_path": "/etc/hostname"}),
        (PLACEHOLDER, {}),
    ]
    session, _client = research_session(stack, tmp_path)
    answer = asyncio.run(session.ask(QUESTION, [QUESTION]))
    assert answer.text.startswith("ROGUE: "), answer
    said = answer.text.removeprefix("ROGUE: ").split(" | ")
    assert len(said) == len(Model.rogue), said
    host = Path("/etc/hostname").read_text().strip()
    for (tool, _args), result in zip(Model.rogue, said, strict=True):
        assert re.search(r"no such tool|not available|error|unknown", result, re.I), (tool, result)
        assert host not in result, (tool, result)
    assert not marker.exists()
    assert answer.research is not None  # the broker served model calls, and no corpus op
    assert {line["op"] for line in answer.research} == {"model"}
