"""Story 2.4: the thin MCP shim (Dev Spec §5.5; R-08, R-11 to R-13, R-15, R-30, R-32).

The shim's daemon is `FakeDaemon`, a transport that records every frame and answers as the
daemon would. Time is an injected clock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from crewchat_clients import mcp_shim
from crewchat_clients.mcp_shim import (
    ALIASES,
    DaemonError,
    Shim,
    ShimError,
    SocketTransport,
    build_server,
    is_headless,
)

DEV = "babel"
SECRET = "daemon-held-secret-value-0123456789"


class FakeDaemon:
    def __init__(self, backends: Sequence[str] = ("mattermost:ci", "discord")) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self.backends = list(backends)
        self.posts = 0
        self.forget_next = False
        self.secret = SECRET  # what the daemon holds; it never goes to the shim

    def ops(self) -> list[str]:
        return [op for op, _ in self.frames]

    def of(self, op: str) -> list[dict[str, Any]]:
        return [args for o, args in self.frames if o == op]

    async def __call__(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.frames.append((op, json.loads(json.dumps(args))))
        if op == "register":
            # As the daemon lists them: a Mattermost server by its id.
            listed = [b.removeprefix("mattermost:") for b in self.backends]
            return {"account": f"{DEV}.oaw", "backends": listed, "needs_list": "bj:needs"}
        if self.forget_next:
            self.forget_next = False
            raise DaemonError(op, f"not_registered: {DEV}")
        if op == "send":
            self.posts += 1
            post = f"p{self.posts}"
            return {"backend": args["backend"], "post_id": post, "thread": args.get("thread", post)}
        if op == "dm":
            self.posts += 1
            return {"backend": args["backend"], "post_id": f"p{self.posts}", "channel": "dm1"}
        if op in ("read", "thread"):
            msgs = [{"post_id": "r1", "root_id": ""}, {"post_id": "r2", "root_id": "r1"}]
            return {"backend": args["backend"], "messages": msgs}
        if op == "health":
            return {"backends": {b: {"state": "configured", "since": "t"} for b in self.backends}}
        return {}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Squawks:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.code = code

    async def __call__(self, argv: Sequence[str], cwd: Path) -> int:
        self.calls.append((list(argv), cwd))
        return self.code


def make_repo(tmp_path: Path, **identity: Any) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    ident = {"dev_name": DEV, "dev_team": "oaw", "dev_avatar": "x", **identity}
    ident = {k: v for k, v in ident.items() if v is not None}  # None: leave the field out
    (root / ".claude" / "agent-identity.json").write_text(json.dumps(ident))
    return root


def make_shim(
    tmp_path: Path,
    daemon: FakeDaemon,
    *,
    headless: bool = False,
    squawks: Squawks | None = None,
    clock: Clock | None = None,
    **identity: Any,
) -> Shim:
    return Shim(
        make_repo(tmp_path, **identity),
        daemon,
        clock=clock or Clock(),
        headless=headless,
        defaults_file=tmp_path / "home" / ".claude" / "chat-defaults.json",
        squawk=squawks or Squawks(),
        remote_for=lambda _root: None,
        session_id="s1",
        environ={},
        legacy_dir=tmp_path / "no-legacy",
    )


def bump_mtime(path: Path, seconds: int) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


# -- register --------------------------------------------------------------------------------
def test_register_sends_identity_and_omits_missing_crew(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon)
    asyncio.run(shim.start())
    ident = {"dev_name": DEV, "session_id": "s1", "dev_team": "oaw", "dev_avatar": "x"}
    assert daemon.of("register") == [ident]
    assert shim.needs_list == "bj:needs"

    daemon2 = FakeDaemon()
    shim2 = make_shim(tmp_path / "b", daemon2, crew="wave")
    asyncio.run(shim2.start())
    assert daemon2.of("register") == [{**ident, "crew": "wave"}]


def test_shim_register_reads_identity(tmp_path: Path) -> None:
    """D-103: dev_team/dev_avatar from agent-identity.json, then the legacy /tmp file, then ""."""
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()

    def frame(sub: str, **identity: Any) -> dict[str, Any]:
        daemon = FakeDaemon()
        root = make_repo(tmp_path / sub, **identity)
        shim = Shim(
            root,
            daemon,
            clock=Clock(),
            headless=False,
            defaults_file=tmp_path / sub / "chat-defaults.json",
            squawk=Squawks(),
            remote_for=lambda _root: None,
            session_id="s1",
            environ={},
            legacy_dir=legacy_dir,
        )
        asyncio.run(shim.register())
        (args,) = daemon.of("register")
        return args

    # (1) The identity file, with the field names /name writes.
    got = frame("a", dev_team="oaw", dev_avatar="📡")
    assert (got["dev_team"], got["dev_avatar"]) == ("oaw", "📡")

    # (2) Missing there: the legacy /tmp/claude-agent-<md5(repo_root)>.json.
    root_b = (tmp_path / "b" / "repo").absolute()
    digest = hashlib.md5(str(root_b).encode()).hexdigest()
    (legacy_dir / f"claude-agent-{digest}.json").write_text(
        json.dumps({"dev_name": DEV, "dev_team": "legacy-team", "dev_avatar": "🦊"})
    )
    got = frame("b", dev_team=None, dev_avatar=None)
    assert (got["dev_team"], got["dev_avatar"]) == ("legacy-team", "🦊")

    # (3) Neither: "".
    got = frame("c", dev_team=None, dev_avatar=None)
    assert (got["dev_team"], got["dev_avatar"]) == ("", "")

    # The daily re-register sends them too.
    daemon, clock = FakeDaemon(), Clock()
    shim = make_shim(tmp_path / "d", daemon, clock=clock, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        clock.now += timedelta(hours=24)
        await shim.tick()

    asyncio.run(scenario())
    assert [(a["dev_team"], a["dev_avatar"]) for a in daemon.of("register")] == [("oaw", "x")] * 2


def test_shim_reregisters_daily(tmp_path: Path) -> None:
    daemon, clock = FakeDaemon(), Clock()
    shim = make_shim(tmp_path, daemon, clock=clock, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        assert len(daemon.of("register")) == 1
        clock.now += timedelta(hours=23)
        await shim.tick()  # the timer, before 24 h: nothing
        await shim.chat_send("c1", "hi")  # a call, before 24 h: nothing
        assert len(daemon.of("register")) == 1
        clock.now += timedelta(hours=1)
        await shim.tick()  # the timer, at 24 h: register again
        assert len(daemon.of("register")) == 2
        # Before a call, when the last register is older than 24 h.
        clock.now += timedelta(hours=25)
        await shim.chat_send("c1", "again")
        assert daemon.ops()[-2:] == ["register", "send"]
        assert len(daemon.of("register")) == 3

    asyncio.run(scenario())


def test_run_timers_ticks_on_the_interval(tmp_path: Path) -> None:
    daemon, clock = FakeDaemon(), Clock()
    shim = make_shim(tmp_path, daemon, clock=clock)
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.now += timedelta(hours=12)
        if len(waits) > 3:
            raise asyncio.CancelledError

    async def scenario() -> None:
        await shim.start()
        with pytest.raises(asyncio.CancelledError):
            await shim.run_timers(interval_s=5.0, sleep=sleep)

    asyncio.run(scenario())
    assert waits == [5.0] * 4
    assert len(daemon.of("register")) == 2  # at start, then at 24 h


def test_not_registered_reregisters_and_retries_once(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        daemon.forget_next = True
        result = await shim.chat_send("c1", "hi")
        assert result["post_id"] == "p1"

    asyncio.run(scenario())
    assert len(daemon.of("register")) == 2 and len(daemon.of("send")) == 2


# -- pushes ----------------------------------------------------------------------------------
def test_pushes_brief_on_mtime_change(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon)
    seed = shim.repo_root / ".claude" / "reseed-revive.md"
    seed.write_text("## Plan\nprivate\n\n## Kiosk\nfirst brief\n")

    async def scenario() -> None:
        await shim.start()
        assert daemon.of("kiosk_brief") == [{"dev_name": DEV, "text": "first brief"}]
        await shim.tick()  # unchanged: nothing new
        assert len(daemon.of("kiosk_brief")) == 1
        seed.write_text("## Kiosk\nsecond brief\n\n## Volatile\nD-1 private\n")
        bump_mtime(seed, 5)
        await shim.tick()
        await shim.tick()
        assert daemon.of("kiosk_brief")[1:] == [{"dev_name": DEV, "text": "second brief"}]

    asyncio.run(scenario())
    sent = json.dumps(daemon.of("kiosk_brief"))
    assert "private" not in sent and "Plan" not in sent


def test_pushes_squawk_ids(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon)
    mine = shim.repo_root / ".claude" / "sqtk-mine.json"
    mine.write_text(json.dumps([{"list": "bj:needs", "seq": 1, "id": 113}]))

    async def scenario() -> None:
        await shim.start()
        assert daemon.of("squawk_ids") == [{"dev_name": DEV, "ids": ["113"]}]
        await shim.tick()
        assert len(daemon.of("squawk_ids")) == 1
        mine.write_text(json.dumps([{"id": 113}, {"id": 117}]))
        bump_mtime(mine, 5)
        await shim.tick()
        assert daemon.of("squawk_ids")[-1] == {"dev_name": DEV, "ids": ["113", "117"]}

    asyncio.run(scenario())
    assert len(daemon.of("squawk_ids")) == 2


def test_missing_files_push_empty_at_start(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    asyncio.run(make_shim(tmp_path, daemon).start())
    assert daemon.of("kiosk_brief") == [{"dev_name": DEV, "text": ""}]
    assert daemon.of("squawk_ids") == [{"dev_name": DEV, "ids": []}]


# -- default resolution (R-11) ----------------------------------------------------------------
def test_headless_fails_loud_and_squawks(tmp_path: Path) -> None:
    daemon, squawks = FakeDaemon(), Squawks()
    shim = make_shim(tmp_path, daemon, headless=True, squawks=squawks)

    async def scenario() -> None:
        await shim.start()
        with pytest.raises(ShimError, match="NO DEFAULT CHAT BACKEND"):
            await shim.chat_send("c1", "do not leak this message text")
        with pytest.raises(ShimError, match="already squawked"):
            await shim.chat_send("c1", "again")

    asyncio.run(scenario())
    assert daemon.of("send") == []  # nothing was sent: no Discord fallback
    assert len(squawks.calls) == 1
    argv, cwd = squawks.calls[0]
    assert argv[:2] == ["add", "bj:needs"] and cwd == shim.repo_root
    assert "chat_set_default" in argv[2] and "leak" not in argv[2]


def test_headless_squawk_failure_is_loud(tmp_path: Path) -> None:
    shim = make_shim(tmp_path, FakeDaemon(), headless=True, squawks=Squawks(code=1))

    async def scenario() -> None:
        await shim.start()
        with pytest.raises(ShimError, match="FAILED"):
            await shim.chat_send("c1", "hi")

    asyncio.run(scenario())


def test_interactive_returns_a_question(tmp_path: Path) -> None:
    daemon, squawks = FakeDaemon(), Squawks()
    shim = make_shim(tmp_path, daemon, squawks=squawks)

    async def scenario() -> None:
        await shim.start()
        got = await shim.chat_send("c1", "hi")
        assert got["needs_human"] is True
        assert got["key"] == str(shim.repo_root.resolve())
        assert got["options"] == ["mattermost:ci", "discord"]
        await shim.chat_set_default(got["key"], "mattermost:ci")
        sent = await shim.chat_send("c1", "hi")
        assert sent["backend"] == "mattermost:ci"

    asyncio.run(scenario())
    assert squawks.calls == [] and len(daemon.of("send")) == 1
    assert "off_default" not in daemon.of("send")[0]


def test_set_default_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon)
    asyncio.run(shim.start())
    before = len(daemon.frames)
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: Any, dst: Any) -> None:
        replaced.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    asyncio.run(shim.chat_set_default("github.com/Wave-Engineering", "discord"))
    asyncio.run(shim.chat_set_default("/some/repo", "mattermost:ci"))

    path = shim.defaults_file
    assert json.loads(path.read_text()) == {
        "github.com/Wave-Engineering": "discord",
        "/some/repo": "mattermost:ci",
    }
    # Written to a temp file in the same directory, then renamed over the file.
    assert len(replaced) == 2
    assert all(Path(src).parent == path.parent and dst == str(path) for src, dst in replaced)
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]  # no temp left
    assert len(daemon.frames) == before  # no frame to the daemon
    with pytest.raises(ShimError):
        asyncio.run(shim.chat_set_default("k", "irc"))


# -- routing (R-12, R-13) --------------------------------------------------------------------
def test_reply_follows_thread_backend(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        root = await shim.chat_send("town-square", "a root", backend="mattermost:ci")
        assert root["backend"] == "mattermost:ci"
        reply = await shim.chat_send("town-square", "a reply", thread=root["post_id"])
        assert reply["backend"] == "mattermost:ci"  # not the default, discord
        # A post seen in a read is remembered too.
        await shim.chat_read("c9", backend="mattermost:ci")
        again = await shim.chat_send("c9", "on r1", thread="r2")
        assert again["backend"] == "mattermost:ci"
        with pytest.raises(ShimError, match="follows its thread"):
            await shim.chat_send("c9", "x", thread="r1", backend="discord")

    asyncio.run(scenario())
    sends = daemon.of("send")
    assert sends[0]["off_default"] is True  # named, and not the default (R-13)
    assert "off_default" not in sends[1] and sends[1]["thread"] == "p1"
    assert len(sends) == 3


def test_named_default_is_not_off_default(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="discord")
    asyncio.run(shim.chat_send("c1", "hi", backend="discord"))
    assert "off_default" not in daemon.of("send")[0]
    with pytest.raises(ShimError, match="not mattermost"):
        asyncio.run(shim.chat_send("c1", "hi", backend="slack"))


def test_every_chat_tool_forwards_one_frame(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="mattermost:ci")

    async def scenario() -> None:
        await shim.start()
        start = len(daemon.frames)
        await shim.chat_dm("kessel", "hello")
        await shim.chat_read("c1", since=1.5, limit=5)
        await shim.chat_thread("r1")
        await shim.chat_react("r1", "white_check_mark")
        await shim.chat_health()
        assert daemon.ops()[start:] == ["dm", "read", "thread", "react", "health"]

    asyncio.run(scenario())
    assert daemon.of("read")[0] == {
        "dev_name": DEV,
        "backend": "mattermost:ci",
        "channel": "c1",
        "since": 1.5,
        "limit": 5,
    }


# -- aliases (R-15) --------------------------------------------------------------------------
def test_disc_alias_maps_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="mattermost:ci")
    caplog.set_level(logging.WARNING, logger="crewchat_clients.mcp_shim")

    async def scenario() -> None:
        await shim.start()
        await shim.disc_send("123", "one")
        await shim.disc_send("123", content="two")
        await shim.disc_read("123", limit=5)
        got = await shim.disc_read("123", message_id="r2")
        assert [m["post_id"] for m in got["messages"]] == ["r2"]
        await shim.disc_thread("456")
        await shim.disc_dm("789", "hi")
        await shim.disc_react("111", "👍", channel_id="123")

    asyncio.run(scenario())
    for op in ("send", "read", "thread", "dm", "react"):
        assert daemon.of(op) and all(a["backend"] == "discord" for a in daemon.of(op))
    assert [a["text"] for a in daemon.of("send")] == ["one", "two"]
    assert daemon.of("react")[0]["post_id"] == "123/111"
    warnings = [r.getMessage() for r in caplog.records if "deprecated" in r.getMessage()]
    assert len(warnings) == 5  # one per alias, though disc_send and disc_read ran twice
    for alias in ALIASES:
        assert sum(alias in w for w in warnings) == 1


def test_mcp_server_lists_every_tool(tmp_path: Path) -> None:
    server = build_server(make_shim(tmp_path, FakeDaemon()), timers=False)
    names = {t.name for t in asyncio.run(server.list_tools())}
    chat = {"chat_send", "chat_read", "chat_thread", "chat_dm", "chat_react", "chat_health"}
    assert names == chat | {"chat_set_default"} | set(ALIASES)


# -- no credentials (R-08) ---------------------------------------------------------------------
def walk(value: Any, seen: set[int] | None = None) -> list[str]:
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in walk(k, seen) + walk(v, seen)]
    if isinstance(value, list | tuple | set | frozenset):
        return [s for v in value for s in walk(v, seen)]
    if hasattr(value, "__dict__") and not callable(value):
        return walk(vars(value), seen)
    return []


def test_shim_holds_no_credentials(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        await shim.chat_send("c1", "hi")
        await shim.chat_read("c1")
        await shim.chat_set_default("/repo", "discord")

    asyncio.run(scenario())
    state = [s for k, v in vars(shim).items() if k != "transport" for s in walk(v)]
    assert not any(SECRET in s for s in state)
    assert not any("token" in k.lower() for k in vars(shim))
    # No frame the shim sent carries one, and no file it wrote holds one.
    assert SECRET not in json.dumps(daemon.frames)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="replace")
    # The shim reads no credential from its environment: none of its code names one.
    src = Path(mcp_shim.__file__).parent
    for py in src.glob("*.py"):
        text = py.read_text().lower()
        assert "token" not in text and "password" not in text, py.name


# -- the session's kind, and the socket --------------------------------------------------------
def test_is_headless() -> None:
    assert is_headless({"CLAUDE_CODE_ENTRYPOINT": "cli"}, ["claude"]) is False
    assert is_headless({"CLAUDE_CODE_ENTRYPOINT": "cli"}, ["claude", "-p", "go"]) is True
    assert is_headless({"CLAUDE_CODE_ENTRYPOINT": "cli"}, ["claude", "--print"]) is True
    assert is_headless({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, ["claude"]) is True
    assert is_headless({}, []) is True


def test_socket_transport(tmp_path: Path) -> None:
    import tempfile

    short = Path(tempfile.mkdtemp(prefix="cc", dir="/tmp"))
    sock = short / "s.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        frame = json.loads(await reader.readline())
        if frame["op"] == "health":
            reply = {"proto": 1, "id": frame["id"], "ok": True, "result": {"backends": {}}}
        else:
            reply = {"proto": 1, "id": frame["id"], "ok": False, "error": "unsupported"}
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
        writer.close()

    async def scenario() -> None:
        server = await asyncio.start_unix_server(handle, path=str(sock))
        try:
            t = SocketTransport(sock, timeout_s=5)
            assert await t("health", {}) == {"backends": {}}
            with pytest.raises(DaemonError) as exc:
                await t("nope", {})
            assert exc.value.code == "unsupported"
        finally:
            server.close()
            await server.wait_closed()
        with pytest.raises(mcp_shim.DaemonUnreachable):
            await SocketTransport(short / "missing.sock", timeout_s=1)("health", {})

    try:
        asyncio.run(scenario())
    finally:
        sock.unlink(missing_ok=True)
        short.rmdir()
