"""Story 2.4: the thin MCP shim (Dev Spec §5.5; R-08, R-11 to R-13, R-15, R-30, R-32).

The shim's daemon is `FakeDaemon`, a transport that records every frame and answers as the
daemon would. Time is an injected clock.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from crewchat_clients import mcp_shim
from crewchat_clients.mcp_shim import (
    DaemonError,
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

    def ops(self) -> list[str]:
        return [op for op, _ in self.frames]

    def of(self, op: str) -> list[dict[str, Any]]:
        return [args for o, args in self.frames if o == op]

    async def __call__(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
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
    assert daemon.of("register") == [{**ident, "project_key": key}]
    assert shim.needs_list == "bj:needs"

    daemon2 = FakeDaemon()
    shim2 = make_shim(tmp_path / "b", daemon2, crew="wave")
    asyncio.run(shim2.start())
    key2 = str(shim2.repo_root.resolve())
    assert daemon2.of("register") == [{**ident, "crew": "wave", "project_key": key2}]


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
        await shim.chat_resolve(GUILD, "agent-ops")
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
        "chat_resolve": {"guild_id", "name", "type"},
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
