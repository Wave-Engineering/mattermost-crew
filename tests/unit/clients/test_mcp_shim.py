"""Story 2.4: the thin MCP shim (Dev Spec §5.5; R-08, R-11 to R-13, R-15, R-30, R-32).

The shim's daemon is `FakeDaemon`, a transport that records every frame and answers as the
daemon would. Time is an injected clock.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from crewchat_clients import mcp_shim
from crewchat_clients.mcp_shim import (
    DaemonError,
    DaemonUnreachable,
    Shim,
    ShimError,
    SocketTransport,
    build_server,
    is_headless,
)

DEV = "babel"
SECRET = "daemon-held-secret-value-0123456789"


R1 = "r" * 26  # Mattermost-shaped post ids the fake's reads return
R2 = "s" * 26


def post_id(backend: str, n: int) -> str:
    """A post id of the backend's own shape: a snowflake on Discord, 26 characters else."""
    return str(1_300_000_000_000_000_000 + n) if backend == "discord" else f"p{n:0>25}"


class FakeDaemon:
    """Answers as the daemon would. It, not the shim, knows which backend each post is on
    (`where`), and it outlives any one shim instance, as the real daemon does."""

    def __init__(self, backends: Sequence[str] = ("mattermost:ci", "discord")) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self.backends = list(backends)
        self.posts = 0
        self.forget_next = False
        self.where: dict[str, str] = {}  # post id -> backend
        self.uploads: dict[tuple[str, str], bytes] = {}  # in flight, by (dev, name)
        self.finished: dict[str, bytes] = {}  # upload id -> content
        self.secret = SECRET  # what the daemon holds; it never goes to the shim
        self.refuse_texts = False  # refuse a `squawk_ids` that carries any texts

    def ops(self) -> list[str]:
        return [op for op, _ in self.frames]

    def of(self, op: str) -> list[dict[str, Any]]:
        return [args for o, args in self.frames if o == op]

    async def __call__(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        frame = {"proto": 1, "id": "x" * 36, "op": op, "args": args}
        if len(json.dumps(frame).encode()) + 1 > 64 * 1024:  # the daemon's MAX_FRAME_BYTES
            raise DaemonError(op, "bad_request: frame too large")
        if op == "squawk_ids" and args.get("texts") and self.refuse_texts:
            raise DaemonError(op, "bad_request: texts refused")
        self.frames.append((op, json.loads(json.dumps(args))))
        if op == "register":
            # The one backend-name form (§5.4): as calls name them.
            return {"account": f"{DEV}.oaw", "backends": self.backends, "needs_list": "bj:needs"}
        if self.forget_next:
            self.forget_next = False
            raise DaemonError(op, f"not_registered: {DEV}")
        if op in ("send", "dm"):
            for upload_id in args.get("attachments", []):
                if upload_id not in self.finished:
                    raise DaemonError(op, f"not_found: upload {upload_id}")
            self.posts += 1
            post = post_id(args["backend"], self.posts)
            self.where[post] = args["backend"]
            if op == "dm":
                return {"backend": args["backend"], "post_id": post, "channel": "dm1"}
            return {"backend": args["backend"], "post_id": post, "thread": args.get("thread", post)}
        if op in ("read", "thread"):
            for p in (R1, R2):
                self.where[p] = args["backend"]
            msgs = [{"post_id": R1, "root_id": ""}, {"post_id": R2, "root_id": R1}]
            if args.get("post_id"):
                msgs = [m for m in msgs if m["post_id"] == args["post_id"]]
            return {"backend": args["backend"], "messages": msgs}
        if op == "thread_backend":
            found = self.where.get(args["post_id"])
            if found is None:
                raise DaemonError(op, f"not_found: {args['post_id']}")
            return {"backend": found}
        if op == "upload":
            key = (args["dev_name"], args["name"])
            data = base64.b64decode(args["data_b64"])
            if args["seq"] == 0:
                self.uploads[key] = b""
            self.uploads[key] += data
            if not args["last"]:
                return {"seq": args["seq"], "size": len(self.uploads[key])}
            upload_id = f"{len(self.finished):0>32}"
            self.finished[upload_id] = self.uploads.pop(key)
            return {"upload_id": upload_id, "size": len(self.finished[upload_id])}
        if op == "health":
            return {"backends": {b: {"state": "configured", "since": "t"} for b in self.backends}}
        if op in ("list_channels", "resolve", "create_channel", "create_thread"):
            return {"backend": args["backend"], "op": op}
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


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # A unix socket path is limited to 108 bytes; pytest's tmp_path can be longer.
    with tempfile.TemporaryDirectory(prefix="cc-shim-", dir="/tmp") as d:
        yield Path(d)


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


# -- outsider content (Story 3.3) ------------------------------------------------------------
class OutsiderDaemon(FakeDaemon):
    """Marks post R2 as outsider content in every read or thread, as the daemon's pull filter
    does under social."""

    async def __call__(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await super().__call__(op, args)
        if op in ("read", "thread"):
            msgs = [{**m, "text": f"text of {m['post_id']}"} for m in result["messages"]]
            for m in msgs:
                if m["post_id"] == R2:
                    m["outsider"] = True
            result = {**result, "messages": msgs}
        return result


def test_outsider_text_prefixed(tmp_path: Path) -> None:
    daemon = OutsiderDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="mattermost:ci")

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        await shim.start()
        return await shim.chat_read("c9"), await shim.chat_thread(R2)

    read, thread = asyncio.run(scenario())
    marked = f"[from outside the crew] text of {R2}"
    assert [m["text"] for m in read["messages"]] == [f"text of {R1}", marked]
    assert [m["text"] for m in thread["messages"]] == [marked]
    assert mcp_shim.mark_outsiders({"error": "x"}) == {"error": "x"}


# -- register --------------------------------------------------------------------------------
def test_register_sends_identity_and_omits_missing_crew(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon)
    asyncio.run(shim.start())
    ident = {"dev_name": DEV, "session_id": "s1", "dev_team": "oaw", "dev_avatar": "x"}
    # No remote: the project key is the repo's path (#117).
    key = str(shim.repo_root.resolve())
    assert daemon.of("register") == [{**ident, "project_key": key, "repo": ""}]
    assert shim.needs_list == "bj:needs"

    daemon2 = FakeDaemon()
    shim2 = make_shim(tmp_path / "b", daemon2, crew="wave")
    asyncio.run(shim2.start())
    key2 = str(shim2.repo_root.resolve())
    assert daemon2.of("register") == [{**ident, "crew": "wave", "project_key": key2, "repo": ""}]


def test_shim_register_sends_project_key(tmp_path: Path) -> None:
    """#117: `project_key` is `repo_key()`, the forge org, and never the remote URL, which
    can carry a credential."""
    daemon = FakeDaemon()
    root = make_repo(tmp_path)
    remote = "https://user:s3cret-token@gitlab.example.lan/partner-org/sub/repo.git"
    shim = Shim(
        root,
        daemon,
        clock=Clock(),
        headless=False,
        defaults_file=tmp_path / "home" / ".claude" / "chat-defaults.json",
        squawk=Squawks(),
        remote_for=lambda _root: remote,
        session_id="s1",
        environ={},
        legacy_dir=tmp_path / "no-legacy",
    )
    asyncio.run(shim.start())
    (frame,) = daemon.of("register")
    assert frame["project_key"] == "gitlab.example.lan/partner-org"
    # #139: the full repo for the kiosk's corpus pool, still without the credential.
    assert frame["repo"] == "gitlab.example.lan/partner-org/sub/repo"
    assert "s3cret" not in json.dumps(frame) and "repo.git" not in json.dumps(frame)


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
        assert result["post_id"] == post_id("discord", 1)

    asyncio.run(scenario())
    assert len(daemon.of("register")) == 2 and len(daemon.of("send")) == 2


def test_chat_health_reregisters_after_daemon_restart(tmp_path: Path) -> None:
    """`chat_health` names the agent, so a restarted daemon's `not_registered` re-registers
    it, as any other call does (#112)."""
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="discord")

    async def scenario() -> dict[str, Any]:
        await shim.start()
        daemon.forget_next = True
        return await shim.chat_health()

    result = asyncio.run(scenario())
    assert set(result["backends"]) == {"mattermost:ci", "discord"}
    assert len(daemon.of("register")) == 2
    assert daemon.of("health") == [{"dev_name": DEV}] * 2


class RestartingDaemon(FakeDaemon):
    """`ping` as the real daemon answers it: `not_registered` after a `restart()`, until the
    next `register`; `down` makes every frame unreachable; `pings` is `ping`'s own answer."""

    def __init__(self) -> None:
        super().__init__()
        self.holds = False
        self.down = False
        self.pings: Exception | None = None

    def restart(self) -> None:
        self.holds = False

    async def __call__(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.down:
            raise DaemonUnreachable("crewchat daemon at /run/crewchat/crewchat.sock: refused")
        if op == "register":
            self.holds = True
        if op == "ping":
            self.frames.append((op, dict(args)))
            if self.pings is not None:
                raise self.pings
            if not self.holds:
                raise DaemonError(op, f"not_registered: {args['dev_name']}")
            return {}
        return await super().__call__(op, args)


def test_ping_reregisters_an_idle_session_after_a_daemon_restart(tmp_path: Path) -> None:
    """#129: no tool call, only the timer: the ping finds the registration gone, registers
    again and force-pushes the files, so the agent's doorbell can arm."""
    daemon, clock = RestartingDaemon(), Clock()
    shim = make_shim(tmp_path, daemon, clock=clock, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        await shim.tick()  # the first tick pings, and the daemon holds the agent
        assert daemon.of("ping") == [{"dev_name": DEV}] and len(daemon.of("register")) == 1
        pushes = len(daemon.of("kiosk_brief"))
        daemon.restart()
        clock.now += timedelta(seconds=5)
        await shim.tick()  # inside PING_EVERY: no ping yet
        assert len(daemon.of("ping")) == 1
        clock.now += timedelta(seconds=10)
        await shim.tick()  # at PING_EVERY: not_registered, so register and push
        assert len(daemon.of("ping")) == 2 and len(daemon.of("register")) == 2
        assert daemon.holds and len(daemon.of("kiosk_brief")) == pushes + 1

    asyncio.run(scenario())


def test_ping_survives_a_daemon_that_is_down(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon, clock = RestartingDaemon(), Clock()
    shim = make_shim(tmp_path, daemon, clock=clock, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        daemon.down = True
        daemon.restart()
        with caplog.at_level(logging.ERROR, logger="crewchat_clients.mcp_shim"):
            await shim.ping()  # unreachable: logged at debug, nothing raised
        assert not [r for r in caplog.records if "ping" in r.getMessage()]
        daemon.down = False
        clock.now += mcp_shim.PING_EVERY
        await shim.ping()  # back, without the agent: register again
        assert daemon.holds and len(daemon.of("register")) == 2

    asyncio.run(scenario())


def test_ping_keeps_asking_a_daemon_without_the_op(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#137: an older daemon answers `unsupported`, and is asked again every `PING_EVERY`,
    said once at INFO. The upgrade that brings `ping` is a restart, so when the new daemon
    answers `not_registered`, the idle session registers again with no tool call."""
    daemon, clock = RestartingDaemon(), Clock()
    daemon.pings = DaemonError("ping", "unsupported")
    shim = make_shim(tmp_path, daemon, clock=clock, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        with caplog.at_level(logging.INFO, logger="crewchat_clients.mcp_shim"):
            for _ in range(3):
                await shim.ping()
                clock.now += mcp_shim.PING_EVERY
        assert len(daemon.of("ping")) == 3
        said = [r for r in caplog.records if "no ping op" in r.getMessage()]
        assert [r.levelno for r in said] == [logging.INFO]

        daemon.pings = None  # the upgrade: a daemon with `ping`, which lost the agent
        daemon.restart()
        await shim.ping()
        assert daemon.holds and len(daemon.of("register")) == 2

    asyncio.run(scenario())


def test_run_timers_survive_an_unexpected_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing awaits the timer's task: an error that isn't a ShimError is logged, and the
    next tick still runs, so the ping can't stop in silence."""
    daemon, clock = RestartingDaemon(), Clock()
    shim = make_shim(tmp_path, daemon, clock=clock, home_backend="discord")
    ticks = 0

    async def sleep(_seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        clock.now += mcp_shim.PING_EVERY
        if ticks == 1:
            daemon.pings = RuntimeError("boom")
        elif ticks == 2:
            daemon.pings = None
            daemon.restart()
        else:
            raise asyncio.CancelledError

    async def scenario() -> None:
        await shim.start()
        with (
            caplog.at_level(logging.ERROR, logger="crewchat_clients.mcp_shim"),
            pytest.raises(asyncio.CancelledError),
        ):
            await shim.run_timers(interval_s=5.0, sleep=sleep)

    asyncio.run(scenario())
    assert any("tick failed" in r.getMessage() for r in caplog.records)
    assert daemon.holds and len(daemon.of("register")) == 2  # the second tick re-registered


def test_ping_over_a_broken_socket_is_unreachable_and_carries_on(
    tmp_path: Path, short_dir: Path
) -> None:
    """A daemon that accepts and hangs up (one going down) is `DaemonUnreachable`: the ping
    logs it at debug and returns, and the next ping, with the daemon back, registers again."""
    sock = short_dir / "crewchat.sock"
    daemon, clock = RestartingDaemon(), Clock()
    shim = make_shim(tmp_path, daemon, clock=clock, home_backend="discord")

    async def hang_up(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    async def scenario() -> None:
        await shim.start()
        server = await asyncio.start_unix_server(hang_up, path=str(sock))
        try:
            shim.transport = SocketTransport(sock, timeout_s=2.0)
            await shim.ping()  # no reply: unreachable, nothing raised
        finally:
            server.close()
            await server.wait_closed()
        shim.transport = daemon
        daemon.restart()
        clock.now += mcp_shim.PING_EVERY
        await shim.ping()
        assert daemon.holds and len(daemon.of("register")) == 2

    asyncio.run(scenario())


def test_ping_other_daemon_error_is_raised_to_the_tick(tmp_path: Path) -> None:
    daemon, clock = RestartingDaemon(), Clock()
    daemon.pings = DaemonError("ping", "bad_request: dev_name is a required string")
    shim = make_shim(tmp_path, daemon, clock=clock, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        with pytest.raises(DaemonError, match="bad_request"):
            await shim.ping()
        await shim.tick()  # the tick logs it and carries on

    asyncio.run(scenario())
    assert len(daemon.of("register")) == 1


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
        assert daemon.of("squawk_ids") == [{"dev_name": DEV, "ids": ["113"], "texts": {}}]
        await shim.tick()
        assert len(daemon.of("squawk_ids")) == 1
        mine.write_text(json.dumps([{"id": 113}, {"id": 117}]))
        bump_mtime(mine, 5)
        await shim.tick()
        assert daemon.of("squawk_ids")[-1] == {
            "dev_name": DEV,
            "ids": ["113", "117"],
            "texts": {},
        }

    asyncio.run(scenario())
    assert len(daemon.of("squawk_ids")) == 2


def test_pushes_squawk_texts(tmp_path: Path) -> None:
    """#170: each squawk's original text O, for its answer, as `sqtk-doorbell.sh` reads it:
    the drain's baseline file, overlaid by the record's own `.text`, kept to the ids."""
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon)
    claude = shim.repo_root / ".claude"
    mine = claude / "sqtk-mine.json"
    baseline = claude / "sqtk-baseline.json"
    mine.write_text(json.dumps([{"id": 113, "text": "campaign→main fix"}, {"id": 117}]))
    baseline.write_text(
        json.dumps({"113": "older", "117": "q → ", "999": "not mine", "118": 5})
    )

    async def scenario() -> None:
        await shim.start()
        assert daemon.of("squawk_ids")[-1]["texts"] == {
            "113": "campaign→main fix",
            "117": "q → ",
        }
        # The drain writes a baseline: a change to that file alone is pushed too.
        baseline.write_text(json.dumps({"117": "q2 → ", "120": "x" * 5000}))
        bump_mtime(baseline, 5)
        await shim.tick()
        assert daemon.of("squawk_ids")[-1]["texts"] == {
            "113": "campaign→main fix",
            "117": "q2 → ",
        }

    asyncio.run(scenario())
    assert len(daemon.of("squawk_ids")) == 2


def test_squawk_texts_fit_the_frame(tmp_path: Path) -> None:
    """Texts that would overflow the daemon's 64 KiB frame are cut, longest first, so the ids
    still land: an oversized frame is refused whole, and the doorbell would go silent."""
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon)
    # Each `→` is 6 bytes once encoded: 40 near-cap texts are about 960 KiB on the wire.
    records = [{"id": n, "text": "→" * 4000} for n in range(1, 41)]
    records += [{"id": 100, "text": "short → "}, {"id": 101}]
    mine = shim.repo_root / ".claude" / "sqtk-mine.json"
    mine.write_text(json.dumps(records))

    asyncio.run(shim.start())
    (pushed,) = daemon.of("squawk_ids")
    assert pushed["ids"] == [str(n) for n in range(1, 41)] + ["100", "101"]
    assert len(json.dumps(pushed).encode()) <= mcp_shim.SQUAWK_ARGS_BUDGET
    assert pushed["texts"]["100"] == "short → "
    assert 1 <= len(pushed["texts"]) < 41
    assert all(pushed["texts"][i] == "→" * 4000 for i in pushed["texts"] if i != "100")


def test_fit_squawk_texts_drops_the_longest_first() -> None:
    texts = {"1": "a" * 300, "2": "b" * 100, "3": "c" * 200}
    whole = len(json.dumps({"dev_name": DEV, "ids": ["1", "2", "3"], "texts": texts}).encode())
    assert mcp_shim.fit_squawk_texts(DEV, ["1", "2", "3"], texts, whole) == texts
    assert mcp_shim.fit_squawk_texts(DEV, ["1", "2", "3"], texts, whole - 1) == {
        "2": "b" * 100,
        "3": "c" * 200,
    }
    assert mcp_shim.fit_squawk_texts(DEV, ["1", "2", "3"], texts, 10) == {}


def test_squawk_ids_go_alone_when_texts_are_refused(tmp_path: Path) -> None:
    """Should the daemon refuse the push with texts, the ids go again without them."""
    daemon = FakeDaemon()
    daemon.refuse_texts = True
    shim = make_shim(tmp_path, daemon)
    mine = shim.repo_root / ".claude" / "sqtk-mine.json"
    mine.write_text(json.dumps([{"id": 113, "text": "q → "}]))

    asyncio.run(shim.start())
    assert daemon.of("squawk_ids") == [{"dev_name": DEV, "ids": ["113"], "texts": {}}]


def test_missing_files_push_empty_at_start(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    asyncio.run(make_shim(tmp_path, daemon).start())
    assert daemon.of("kiosk_brief") == [{"dev_name": DEV, "text": ""}]
    assert daemon.of("squawk_ids") == [{"dev_name": DEV, "ids": [], "texts": {}}]


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
        # A post seen in a read is placed by the daemon too.
        await shim.chat_read("c9", backend="mattermost:ci")
        again = await shim.chat_send("c9", "on r1", thread=R2)
        assert again["backend"] == "mattermost:ci"
        # A named backend is used as named (the daemon then answers for it).
        named = await shim.chat_send("c9", "x", thread=R1, backend="discord")
        assert named["backend"] == "discord"
        # A post the daemon can't place fails loudly: no fallback to the default.
        with pytest.raises(ShimError, match="never falls back"):
            await shim.chat_send("c9", "lost", thread="q" * 26)

    asyncio.run(scenario())
    sends = daemon.of("send")
    assert sends[0]["off_default"] is True  # named, and not the default (R-13)
    assert "off_default" not in sends[1] and sends[1]["thread"] == post_id("mattermost:ci", 1)
    assert len(sends) == 4 and "lost" not in [s["text"] for s in sends]
    # Each reply that named no backend asked the daemon; the named one didn't.
    asked = [a["post_id"] for a in daemon.of("thread_backend")]
    assert asked == [post_id("mattermost:ci", 1), R2, "q" * 26]


def test_shim_keeps_no_thread_map(tmp_path: Path) -> None:
    """R-12: nothing in the shim maps a post to a backend; the daemon answers."""
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        for i in range(5):
            await shim.chat_send("c1", f"post {i}", backend="mattermost:ci")
        await shim.chat_read("c9", backend="mattermost:ci")

    asyncio.run(scenario())
    posted = set(daemon.where)
    for value in vars(shim).values():
        assert not (isinstance(value, dict) and posted & set(value)), value
    assert not hasattr(shim, "backend_of")


def test_reply_backend_survives_shim_restart(tmp_path: Path) -> None:
    """A new shim instance replies on an old thread on that thread's backend, through
    `thread_backend`: the shim held nothing to lose."""
    daemon = FakeDaemon()
    first = make_shim(tmp_path / "a", daemon, home_backend="discord")
    root = asyncio.run(first.chat_send("town", "a root", backend="mattermost:ci"))
    del first

    second = make_shim(tmp_path / "b", daemon, home_backend="discord")

    async def scenario() -> dict[str, Any]:
        await second.start()
        return await second.chat_send("town", "after the restart", thread=root["post_id"])

    reply = asyncio.run(scenario())
    assert reply["backend"] == "mattermost:ci"
    last = daemon.of("send")[-1]
    assert last["backend"] == "mattermost:ci" and last["thread"] == root["post_id"]
    assert "off_default" not in last
    assert daemon.of("thread_backend")[-1] == {"dev_name": DEV, "post_id": root["post_id"]}


def test_shim_thread_form(tmp_path: Path) -> None:
    """Discord's `<channel>/<message>`: both parts must be snowflakes. A reply or a thread read
    forwards only the thread channel, and the lookup uses the message. Any other `/` fails in
    the shim, before a frame (R-08, #62)."""
    daemon = FakeDaemon()
    chan, msg = "700000000000000001", "1563738882191654913"
    daemon.where[msg] = "discord"
    shim = make_shim(tmp_path, daemon, home_backend="mattermost:ci")

    async def scenario() -> None:
        await shim.start()
        await shim.chat_send("town", "a reply", thread=f"{chan}/{msg}")
        assert daemon.of("thread_backend")[-1] == {"dev_name": DEV, "post_id": msg}
        assert daemon.of("send")[-1]["thread"] == chan
        await shim.chat_thread(f"{chan}/{msg}")
        assert daemon.of("thread")[-1]["post_id"] == chan
        report = tmp_path / "report.txt"
        report.write_text("attached")
        start = len(daemon.frames)
        for bad in ("a/b/c", "../x", f"{chan}/../x", f"{chan}/x", f"{chan}/", f"/{msg}"):
            for call in (
                lambda bad=bad: shim.chat_send("town", "x", thread=bad),
                lambda bad=bad: shim.chat_send("town", "x", thread=bad, attach_path=str(report)),
                lambda bad=bad: shim.chat_send("town", "x", thread=bad, backend="discord"),
                lambda bad=bad: shim.chat_thread(bad, backend="discord"),
                lambda bad=bad: shim.chat_react(bad, "👍"),
                lambda bad=bad: shim.chat_react(bad, "👍", backend="discord"),
            ):
                with pytest.raises(ShimError, match="<channel>/<message>"):
                    await call()
        assert len(daemon.frames) == start  # no frame for any of them

    asyncio.run(scenario())


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
        await shim.chat_thread(R1, backend="mattermost:ci")
        await shim.chat_react(R1, "white_check_mark", backend="mattermost:ci")
        await shim.chat_health()
        assert daemon.ops()[start:] == ["dm", "read", "thread", "react", "health"]
        # Naming no backend, a post's tools ask the daemon where it is first (R-12).
        await shim.chat_thread(R1)
        await shim.chat_react(R2, "+1")
        assert daemon.ops()[-4:] == ["thread_backend", "thread", "thread_backend", "react"]

    asyncio.run(scenario())
    assert daemon.of("read")[0] == {
        "dev_name": DEV,
        "backend": "mattermost:ci",
        "channel": "c1",
        "since": 1.5,
        "limit": 5,
    }


# -- Discord's guild ops (D-108) ---------------------------------------------------------------
GUILD = "900000000000000001"


def test_guild_tools_forward_discord_ops(tmp_path: Path) -> None:
    """Each guild tool sends its daemon op on Discord with today's args, whatever the default
    is, and logs no deprecation; `chat_read(post_id)` reads the one message."""
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="mattermost:ci")

    async def scenario() -> None:
        await shim.start()
        await shim.chat_list_channels(GUILD)
        await shim.chat_resolve("agent-ops", guild_id=GUILD)
        await shim.chat_create_channel(GUILD, "new-room", type="voice")
        await shim.chat_create_thread("#agent-ops", "a thread", message="first")
        await shim.chat_list_channels(GUILD, type="category")
        got = await shim.chat_read("123", backend="discord", post_id=R2)
        assert [m["post_id"] for m in got["messages"]] == [R2]

    asyncio.run(scenario())
    for op in ("list_channels", "resolve", "create_channel", "create_thread"):
        assert daemon.of(op) and all(a["backend"] == "discord" for a in daemon.of(op)), op
        assert all(a["off_default"] is True for a in daemon.of(op))  # the default is Mattermost
    assert daemon.of("list_channels")[0] == {
        "dev_name": DEV,
        "backend": "discord",
        "guild_id": GUILD,
        "type": "text",
        "off_default": True,
    }
    assert daemon.of("list_channels")[1]["type"] == "category"
    assert daemon.of("resolve")[0]["type"] == "channel"
    assert daemon.of("create_channel")[0]["type"] == "voice"
    assert daemon.of("create_thread")[0] == {
        "dev_name": DEV,
        "backend": "discord",
        "channel": "#agent-ops",
        "name": "a thread",
        "message": "first",
        "off_default": True,
    }
    (read,) = daemon.of("read")
    assert read["post_id"] == R2 and "limit" not in read


def test_guild_tools_take_todays_args(tmp_path: Path) -> None:
    """The four guild tools keep today's `disc_*` argument names and defaults, and the MCP
    server offers each with them."""
    import inspect

    guild = {
        "chat_list_channels": {"guild_id", "type"},
        "chat_resolve": {"guild_id", "name", "type", "backend"},  # backend: Mattermost (#145)
        "chat_create_channel": {"guild_id", "name", "type"},
        "chat_create_thread": {"channel_id", "name", "message"},
    }
    shim = make_shim(tmp_path, FakeDaemon(), home_backend="discord")
    for tool, args in guild.items():
        assert set(inspect.signature(getattr(shim, tool)).parameters) == args, tool
    assert inspect.signature(shim.chat_list_channels).parameters["type"].default == "text"
    assert inspect.signature(shim.chat_resolve).parameters["type"].default == "channel"
    assert inspect.signature(shim.chat_create_channel).parameters["type"].default == "text"
    server = build_server(shim, timers=False)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    for tool, args in guild.items():
        assert args <= set(tools[tool].inputSchema["properties"]), tool
    assert "post_id" in tools["chat_read"].inputSchema["properties"]


def test_chat_resolve_without_guild_routes_to_mattermost(tmp_path: Path) -> None:
    """#145: with no `guild_id`, `chat_resolve` goes to the default backend, or the one
    named, and never carries a guild."""
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="mattermost:ci")

    async def scenario() -> None:
        await shim.start()
        await shim.chat_resolve("#precheck")
        await shim.chat_resolve("Roll Call", backend="discord")

    asyncio.run(scenario())
    first, second = daemon.of("resolve")
    assert first == {
        "dev_name": DEV,
        "backend": "mattermost:ci",
        "name": "#precheck",
        "type": "channel",
    }
    assert second["backend"] == "discord" and second["off_default"] is True
    assert "guild_id" not in first and "guild_id" not in second


def test_chat_send_to_discord_carries_attach_and_embed(tmp_path: Path) -> None:
    """`chat_send(attach_path, embed)` on Discord streams an `upload`, then a `send` with
    `attachments` and `embed`; neither is dropped."""
    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="discord")
    report = tmp_path / "report.txt"
    report.write_bytes(b"status: all green\n")

    async def scenario() -> dict[str, Any]:
        await shim.start()
        return await shim.chat_send("agent-ops", "see attached", attach_path=str(report),
                                    embed="Build:green")

    asyncio.run(scenario())
    ups = daemon.of("upload")
    assert [(u["name"], u["seq"], u["last"]) for u in ups] == [("report.txt", 0, True)]
    (sent,) = daemon.of("send")
    (upload_id,) = sent["attachments"]
    assert daemon.finished[upload_id] == report.read_bytes()
    assert sent["embed"] == "Build:green" and sent["backend"] == "discord"
    assert sent["to"] == "agent-ops"  # a name: the daemon resolves it
    assert daemon.ops().index("upload") < daemon.ops().index("send")

    # chat_dm takes both too.
    asyncio.run(shim.chat_dm("kessel", "psst", attach_path=str(report), embed="Note:x"))
    dm = daemon.of("dm")[-1]
    assert len(dm["attachments"]) == 1 and dm["embed"] == "Note:x"


def test_upload_chunks_fit_frame(tmp_path: Path) -> None:
    """Every `upload` frame is under the daemon's MAX_FRAME_BYTES; a file over 8 MiB fails
    loudly and sends no `send` (nor any upload)."""
    from crewchat.protocol import MAX_FRAME_BYTES

    daemon = FakeDaemon()
    shim = make_shim(tmp_path, daemon, home_backend="discord")
    # A long non-ASCII name: JSON escapes each character to six bytes in the frame.
    big = tmp_path / ("é" * 120 + ".bin")
    big.write_bytes(os.urandom(300 * 1024 + 7))

    async def scenario() -> None:
        await shim.start()
        await shim.chat_send("123", "big file", attach_path=str(big))

    asyncio.run(scenario())
    ups = daemon.of("upload")
    assert len(ups) > 1 and [u["seq"] for u in ups] == list(range(len(ups)))
    assert [u["last"] for u in ups] == [False] * (len(ups) - 1) + [True]
    for args in ups:
        frame = {"proto": 1, "id": "0" * 36, "op": "upload", "args": args}
        assert len(json.dumps(frame).encode()) < MAX_FRAME_BYTES
    assert daemon.finished[daemon.of("send")[0]["attachments"][0]] == big.read_bytes()

    # Over the cap: loud, before any frame.
    huge = tmp_path / "huge.bin"
    with open(huge, "wb") as fh:
        fh.truncate(8 * 1024 * 1024 + 1)
    before = len(daemon.frames)
    with pytest.raises(ShimError, match="8 MiB"):
        asyncio.run(shim.chat_send("123", "too big", attach_path=str(huge)))
    with pytest.raises(ShimError, match="attach_path"):
        asyncio.run(shim.chat_send("123", "missing", attach_path=str(tmp_path / "nope")))
    with pytest.raises(ShimError, match="not a regular file"):
        asyncio.run(shim.chat_send("123", "a dir", attach_path=str(tmp_path)))
    assert len(daemon.frames) == before

    # The daemon's refusal (e.g. too_large) fails the call loudly too, and sends no `send`.
    async def refuse(op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "upload":
            raise DaemonError(op, "too_large: over 8 MiB")
        return await daemon(op, args)

    shim.transport = refuse
    sends = len(daemon.of("send"))
    with pytest.raises(ShimError, match="too_large"):
        asyncio.run(shim.chat_send("123", "refused", attach_path=str(big)))
    assert len(daemon.of("send")) == sends


def test_mcp_server_lists_every_tool(tmp_path: Path) -> None:
    server = build_server(make_shim(tmp_path, FakeDaemon()), timers=False)
    names = {t.name for t in asyncio.run(server.list_tools())}
    chat = {"chat_send", "chat_read", "chat_thread", "chat_dm", "chat_react", "chat_health"}
    guild = {"chat_list_channels", "chat_resolve", "chat_create_channel", "chat_create_thread"}
    assert names == chat | {"chat_set_default"} | guild
    assert not any(n.startswith("disc_") for n in names)  # removed before the first deploy


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
        elif frame["op"] == "big":  # the daemon's reply to a frame it couldn't read
            reply = {"proto": 1, "id": None, "ok": False, "error": "bad_request: frame too large"}
        elif frame["op"] == "stray":
            reply = {"proto": 1, "id": "someone-else", "ok": False, "error": "unsupported"}
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
            # An error with no id is the one frame's: a frame too large is a DaemonError.
            with pytest.raises(DaemonError) as exc:
                await t("big", {})
            assert exc.value.code == "bad_request"
            with pytest.raises(DaemonUnreachable, match="mismatched"):
                await t("stray", {})
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


# -- logging (#144) ---------------------------------------------------------------------------
@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    """The root logger, put back as it was, with the shim's package logger: `configure_logging`
    adds handlers to one and sets the level of both."""
    root, ours = logging.getLogger(), logging.getLogger("crewchat_clients")
    handlers, levels = list(root.handlers), (root.level, ours.level)
    yield root
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
            stream = getattr(handler, "stream", None)
            if stream is not None and stream is not mcp_shim.sys.stderr:
                stream.close()  # a StreamHandler never closes its own stream
    # Only those still attached: pytest removes its own setup-phase handler between phases.
    root.handlers[:] = [h for h in handlers if h in root.handlers]
    root.setLevel(levels[0])
    ours.setLevel(levels[1])


def added(root: logging.Logger, before: list[logging.Handler]) -> list[logging.Handler]:
    return [h for h in root.handlers if h not in before]


def shim_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "crewchat_clients.mcp_shim" and r.levelno == logging.WARNING
    ]


@pytest.mark.parametrize(
    ("value", "level", "warned"),
    [
        (None, logging.INFO, False),
        ("debug", logging.DEBUG, False),
        ("INFO", logging.INFO, False),
        ("Warning", logging.WARNING, False),
        ("loud", logging.INFO, True),
    ],
)
def test_shim_log_level_switch(
    root_logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
    value: str | None,
    level: int,
    warned: bool,
) -> None:
    """`CREWCHAT_SHIM_LOG` sets the shim's own level, in any case; the root never goes below
    INFO. Unset is today's INFO on stderr, and an unknown value is INFO with one WARNING."""
    before = list(root_logger.handlers)
    mcp_shim.configure_logging({} if value is None else {"CREWCHAT_SHIM_LOG": value})
    assert logging.getLogger("crewchat_clients").level == level
    assert root_logger.level == max(level, logging.INFO)
    (stderr,) = added(root_logger, before)
    assert isinstance(stderr, logging.StreamHandler) and stderr.stream is mcp_shim.sys.stderr
    assert len(shim_warnings(caplog)) == (1 if warned else 0)


def test_shim_log_file(
    tmp_path: Path, root_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    """The file gets the timer's DEBUG `tick` and `ping:` lines, at mode 0600, beside stderr,
    and never the MCP library's DEBUG, which carries message text."""
    path = tmp_path / "shim.log"
    before = list(root_logger.handlers)
    env = {"CREWCHAT_SHIM_LOG": "debug", "CREWCHAT_SHIM_LOG_FILE": str(path)}
    mcp_shim.configure_logging(env)
    assert len(added(root_logger, before)) == 2 and not shim_warnings(caplog)
    daemon, clock = RestartingDaemon(), Clock()
    shim = make_shim(tmp_path, daemon, clock=clock, home_backend="discord")

    async def scenario() -> None:
        await shim.start()
        await shim.tick()  # what the timer runs: `tick`, then the ping

    asyncio.run(scenario())
    logging.getLogger("mcp.server.lowlevel.server").debug("Received message: private text")
    text = path.read_text()
    assert " tick" in text and f"ping: the daemon holds {DEV}" in text
    assert "private text" not in text
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("where", ["relative", "unopenable"])
def test_shim_log_file_bad_path_falls_back(
    tmp_path: Path, root_logger: logging.Logger, caplog: pytest.LogCaptureFixture, where: str
) -> None:
    """A relative path, or one that can't be opened, logs one WARNING and keeps stderr only."""
    path = "shim.log" if where == "relative" else str(tmp_path / "missing" / "shim.log")
    before = list(root_logger.handlers)
    mcp_shim.configure_logging({"CREWCHAT_SHIM_LOG_FILE": path})
    (stderr,) = added(root_logger, before)
    assert isinstance(stderr, logging.StreamHandler) and stderr.stream is mcp_shim.sys.stderr
    assert len(shim_warnings(caplog)) == 1
    assert not Path(path).exists()
