"""Stories 2.4 and 2.4c: the daemon's chat ops, uploads, `thread_backend` and the shim's
pushes (§5.3, §5.4; R-11 to R-13, R-15, R-32)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import stat
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import crewchat
import pytest
from crewchat.adapters.base import BackendUnavailable, InboundMessage, NotFound
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.config import ConfigError, parse_config
from crewchat.server import DaemonServer, build_handlers
from crewchat.threads import MyThreads
from crewchat.uploads import UploadError, UploadStore

AGENT_FILES = ("agent-identity.json", "chat-defaults.json", "reseed-revive.md", "sqtk-mine.json")


class FakeBackend:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[Any, ...]] = []
        self.n = 0
        self.fail: Exception | None = None

    def _msg(self, channel: str, text: str, root: str = "") -> InboundMessage:
        self.n += 1
        return InboundMessage(
            backend=self.name,
            channel=channel,
            post_id=f"{self.name}-{self.n}",
            root_id=root,
            author_id="me",
            author_name="babel",
            text=text,
            proto=1,
        )

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def send(
        self, dev_name: str, channel: str, text: str, thread: str | None = None
    ) -> InboundMessage:
        self._check()
        self.calls.append(("send", dev_name, channel, text, thread))
        return self._msg(channel, text, thread or "")

    async def read(
        self, dev_name: str, channel: str, since: float | None = None, limit: int | None = None
    ) -> list[InboundMessage]:
        self.calls.append(("read", dev_name, channel, since, limit))
        return [self._msg(channel, "old")]

    async def thread(self, dev_name: str, post_id: str) -> list[InboundMessage]:
        self.calls.append(("thread", dev_name, post_id))
        return [self._msg("c", "root"), self._msg("c", "reply", post_id)]

    async def dm(self, dev_name: str, to: str, text: str) -> InboundMessage:
        self.calls.append(("dm", dev_name, to, text))
        return self._msg("dm-chan", text)

    async def react(self, dev_name: str, post_id: str, emoji: str) -> None:
        self.calls.append(("react", dev_name, post_id, emoji))

    async def stream(self, dev_name: str) -> AsyncIterator[InboundMessage]:
        if False:
            yield self._msg("c", "never")


class Harness:
    def __init__(self, daemon: Any) -> None:
        self.mm = FakeBackend("mattermost:dev")
        self.discord = FakeBackend("discord")
        self.threads = MyThreads()
        self.context = AgentContext()
        handlers = build_handlers(daemon.config, daemon.registry)
        handlers.update(
            build_chat_handlers(
                daemon.registry,
                {self.mm.name: self.mm, self.discord.name: self.discord},
                self.threads,
                self.context,
            )
        )
        self.server = DaemonServer(handlers)
        self.n = 0

    def call(self, op: str, **args: Any) -> dict[str, Any]:
        self.n += 1
        frame = {"proto": 1, "id": f"r{self.n}", "op": op, "args": args}
        return asyncio.run(self.server.handle_line(json.dumps(frame).encode()))


@pytest.fixture
def h(daemon: Any) -> Harness:
    asyncio.run(daemon.registry.register("babel", None, "s1"))
    return Harness(daemon)


def test_send_goes_to_the_named_backend_and_records_the_thread(h: Harness) -> None:
    got = h.call("send", dev_name="babel", backend="mattermost:dev", to="town", text="hi")
    assert got["ok"] is True
    assert got["result"] == {
        "backend": "mattermost:dev",
        "post_id": "mattermost:dev-1",
        "thread": "mattermost:dev-1",
    }
    assert h.mm.calls == [("send", "babel", "town", "hi", None)] and h.discord.calls == []
    reply = h.call(
        "send", dev_name="babel", backend="discord", to="c", text="re", thread="root9"
    )
    assert reply["result"]["thread"] == "root9"
    assert h.threads.roots("babel") == {"mattermost:dev-1", "root9"}


def test_off_default_is_logged(h: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="crewchat.chat")
    h.call("send", dev_name="babel", backend="discord", to="c", text="a")
    assert not [r for r in caplog.records if "off-default" in r.getMessage()]
    h.call("send", dev_name="babel", backend="discord", to="c", text="b", off_default=True)
    assert [r.getMessage() for r in caplog.records if "off-default" in r.getMessage()] == [
        "off-default: babel discord"
    ]


def test_every_op_forwards(h: Harness) -> None:
    base = {"dev_name": "babel", "backend": "mattermost:dev"}
    dm = h.call("dm", **base, to="kessel", text="psst")
    assert dm["result"] == {
        "backend": "mattermost:dev",
        "post_id": "mattermost:dev-1",
        "channel": "dm-chan",
    }
    read = h.call("read", **base, channel="c", since=1.5, limit=3)
    assert read["result"]["messages"][0]["text"] == "old"
    assert read["result"]["messages"][0]["backend"] == "mattermost:dev"
    thread = h.call("thread", **base, post_id="p1")
    assert [m["text"] for m in thread["result"]["messages"]] == ["root", "reply"]
    assert h.call("react", **base, post_id="p1", emoji="+1")["result"] == {}
    assert [c[0] for c in h.mm.calls] == ["dm", "read", "thread", "react"]
    assert h.mm.calls[1] == ("read", "babel", "c", 1.5, 3)


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"dev_name": "nobody", "backend": "discord", "to": "c", "text": "x"}, "not_registered"),
        ({"dev_name": "babel", "backend": "mattermost:zz", "to": "c", "text": "x"}, "unknown"),
        ({"dev_name": "babel", "to": "c", "text": "x"}, "bad_request"),
        ({"dev_name": "babel", "backend": "discord", "to": "c", "text": " "}, "bad_request"),
        ({"dev_name": "babel", "backend": "discord", "text": "x"}, "bad_request"),
    ],
)
def test_send_refusals(h: Harness, args: dict[str, Any], error: str) -> None:
    got = h.call("send", **args)
    assert got["ok"] is False and got["error"].startswith(error)
    assert h.mm.calls == [] and h.discord.calls == []


def test_backend_failure_is_reported_not_raised(h: Harness) -> None:
    h.discord.fail = RuntimeError("boom with detail")
    got = h.call("send", dev_name="babel", backend="discord", to="c", text="x")
    assert got["ok"] is False and got["error"] == "backend_error: discord: RuntimeError"
    assert "boom" not in got["error"]  # the detail is logged, not returned
    h.discord.fail = ValueError("a Discord DM needs a user ID")
    got = h.call("send", dev_name="babel", backend="discord", to="c", text="x")
    assert got["error"].startswith("bad_request: discord: a Discord DM")


def test_not_on_backend(daemon: Any, make_daemon: Any, servers_toml: dict[str, Any]) -> None:
    servers_toml["discord"] = {"enabled": False}
    d = make_daemon(servers_toml)
    asyncio.run(d.registry.register("babel", None, "s1"))
    got = Harness(d).call("send", dev_name="babel", backend="discord", to="c", text="x")
    assert got["error"].startswith("not_on_backend")


def test_pushes_are_kept(h: Harness) -> None:
    assert h.call("kiosk_brief", dev_name="babel", text="I do chat.")["ok"] is True
    assert h.call("squawk_ids", dev_name="babel", ids=[113, "117"])["ok"] is True
    assert h.context.briefs == {"babel": "I do chat."}
    assert h.context.squawk_ids == {"babel": ["113", "117"]}
    assert h.call("kiosk_brief", dev_name="babel")["ok"] is False
    assert h.call("squawk_ids", dev_name="babel", ids=[True])["ok"] is False
    assert h.call("squawk_ids", dev_name="babel", ids="113")["ok"] is False


def test_daemon_reads_no_agent_files() -> None:
    """R-11, R-32, §5.3: the daemon package never names a file on the agent's side."""
    root = Path(crewchat.__file__).parent
    sources = sorted(root.rglob("*.py"))
    assert sources
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for name in AGENT_FILES:
            assert name not in text, f"{path.relative_to(root)} names {name}"


def test_discord_settings_parse(servers_toml: dict[str, Any]) -> None:
    servers_toml["discord"] = {
        "enabled": True,
        "screamhole_url": "http://127.0.0.1:8787",
        "guild_id": "900000000000000001",
    }
    cfg = parse_config(servers_toml)
    assert cfg.discord_screamhole_url == "http://127.0.0.1:8787"
    assert str(cfg.discord_credential) == "/etc/crewchat/discord.credential"
    assert cfg.discord_guild_id == "900000000000000001"
    for bad in ({"credential": "relative"}, {"guild_id": "abc"}, {"screamhole_url": ""}):
        raw = {**servers_toml, "discord": {"enabled": True, **bad}}
        with pytest.raises(ConfigError):
            parse_config(raw)


# -- Story 2.4c: uploads ---------------------------------------------------------------------
class ManualClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_upload_store_mode_and_expiry(tmp_path: Path) -> None:
    """Each upload is a 0600 file in a 0700 directory, deleted after use, and expired after
    10 minutes (an injected clock); over 8 MiB is `too_large`."""
    clock = ManualClock()
    store = UploadStore(tmp_path / "uploads", clock=clock)
    assert store.chunk("babel", "a.txt", 0, b64(b"hello "), False) == {"seq": 0, "size": 6}
    done = store.chunk("babel", "a.txt", 1, b64(b"world"), True)
    assert done["size"] == 11
    path = tmp_path / "uploads" / done["upload_id"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "uploads").stat().st_mode) == 0o700
    assert path.stat().st_uid == os.geteuid()  # the daemon's own user (crewchat)
    assert path.read_bytes() == b"hello world"

    # Only its own agent can take it; taken, it is deleted after use.
    with pytest.raises(UploadError) as exc:
        store.take("kessel", [done["upload_id"]])
    assert exc.value.code == "not_found"
    (att,) = store.take("babel", [done["upload_id"]])
    assert (att.name, att.size, att.path) == ("a.txt", 11, path)
    store.release([att])
    assert not path.exists()
    with pytest.raises(UploadError):
        store.take("babel", [done["upload_id"]])

    # Expiry: finished or not, 10 minutes after it started.
    kept = store.chunk("babel", "b.txt", 0, b64(b"b"), True)
    store.chunk("babel", "c.txt", 0, b64(b"c"), False)  # never finished
    clock.now += timedelta(minutes=9, seconds=59)
    assert store.sweep() == 0
    clock.now += timedelta(seconds=1)
    assert store.sweep() == 2
    assert list((tmp_path / "uploads").iterdir()) == []
    with pytest.raises(UploadError) as exc:
        store.take("babel", [kept["upload_id"]])
    assert exc.value.code == "not_found"

    # The cap: over 8 MiB total refuses with too_large and leaves nothing behind.
    small = UploadStore(tmp_path / "small", clock=clock, max_bytes=10)
    small.chunk("babel", "big", 0, b64(b"x" * 6), False)
    with pytest.raises(UploadError) as exc:
        small.chunk("babel", "big", 1, b64(b"x" * 5), True)
    assert exc.value.code == "too_large"
    assert list((tmp_path / "small").iterdir()) == []
    assert UploadStore().max_bytes == 8 * 1024 * 1024

    # Malformed chunks.
    for args in (
        ("../../etc/passwd", 0, b64(b"x"), True),  # the name keeps its last part only
        ("ok", 1, b64(b"x"), True),  # seq 1 with nothing in flight
        ("ok", 0, "not base64!", True),
        ("ok", -1, b64(b"x"), True),
        ("", 0, b64(b"x"), True),
    ):
        try:
            got = store.chunk("babel", *args)
        except UploadError as err:
            assert err.code == "bad_request", args
        else:
            assert args[0].endswith("passwd") and store.take("babel", [got["upload_id"]])[
                0
            ].name == "passwd"


class RichBackend(FakeBackend):
    """A FakeBackend that records attachments and embeds, reads one post and answers probes."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.rich: list[dict[str, Any]] = []
        self.posts: set[str] = set()

    async def send(  # type: ignore[override]
        self,
        dev_name: str,
        channel: str,
        text: str,
        thread: str | None = None,
        **kw: Any,
    ) -> InboundMessage:
        self.rich.append(
            {
                "attachments": [(a.name, a.read()) for a in kw.get("attachments", ())],
                "embed": kw.get("embed"),
            }
        )
        return await super().send(dev_name, channel, text, thread)

    async def dm(self, dev_name: str, to: str, text: str, **kw: Any) -> InboundMessage:  # type: ignore[override]
        self.rich.append({"attachments": [a.name for a in kw.get("attachments", ())]})
        return await super().dm(dev_name, to, text)

    async def read_one(self, dev_name: str, channel: str, post_id: str) -> InboundMessage:
        self.calls.append(("read_one", dev_name, channel, post_id))
        return self._msg(channel, f"just {post_id}")

    async def has_post(self, dev_name: str, post_id: str) -> bool:
        self.calls.append(("has_post", dev_name, post_id))
        return post_id in self.posts


class RichHarness(Harness):
    def __init__(self, daemon: Any, uploads: UploadStore, names: list[str]) -> None:
        self.backends = {n: RichBackend(n) for n in names}
        self.threads = MyThreads()
        handlers = build_handlers(daemon.config, daemon.registry)
        handlers.update(
            build_chat_handlers(
                daemon.registry, self.backends, self.threads, AgentContext(), uploads
            )
        )
        self.server = DaemonServer(handlers)
        self.n = 0


def test_send_and_dm_carry_uploads_and_embeds(daemon: Any, tmp_path: Path) -> None:
    asyncio.run(daemon.registry.register("babel", None, "s1"))
    uploads = UploadStore(tmp_path / "uploads")
    h = RichHarness(daemon, uploads, ["mattermost:dev", "discord"])
    up = h.call("upload", dev_name="babel", name="r.txt", seq=0, data_b64=b64(b"R"), last=True)
    assert up["ok"] is True
    upload_id = up["result"]["upload_id"]
    sent = h.call(
        "send",
        dev_name="babel",
        backend="discord",
        to="123456789012345678",
        text="see",
        attachments=[upload_id],
        embed="Title:the body",
    )
    assert sent["ok"] is True
    rich = h.backends["discord"].rich[-1]
    assert rich["attachments"] == [("r.txt", b"R")]
    assert (rich["embed"].title, rich["embed"].body) == ("Title", "the body")
    assert list((tmp_path / "uploads").iterdir()) == []  # deleted after use

    # A used, unknown or other agent's upload is refused loudly; nothing is sent.
    calls = len(h.backends["discord"].calls)
    for ids in ([upload_id], ["0" * 32], "nope"):
        got = h.call(
            "send", dev_name="babel", backend="discord", to="c", text="x", attachments=ids
        )
        assert got["ok"] is False and got["error"].split(":")[0] in ("not_found", "bad_request")
    assert len(h.backends["discord"].calls) == calls

    up = h.call("upload", dev_name="babel", name="d.txt", seq=0, data_b64=b64(b"D"), last=True)
    dm = h.call(
        "dm",
        dev_name="babel",
        backend="mattermost:dev",
        to="kessel",
        text="psst",
        attachments=[up["result"]["upload_id"]],
    )
    assert dm["ok"] is True and h.backends["mattermost:dev"].rich[-1]["attachments"] == ["d.txt"]

    # An upload needs a registered agent; one past the cap is too_large.
    got = h.call("upload", dev_name="nobody", name="x", seq=0, data_b64=b64(b"x"), last=True)
    assert got["error"].startswith("not_registered")
    small = RichHarness(daemon, UploadStore(tmp_path / "small", max_bytes=3), ["discord"])
    got = small.call("upload", dev_name="babel", name="x", seq=0, data_b64=b64(b"xxxx"), last=True)
    assert got["ok"] is False and got["error"].startswith("too_large")

    # Without an upload store, attachments are refused, never dropped.
    plain = Harness(daemon)
    got = plain.call(
        "send", dev_name="babel", backend="discord", to="c", text="x", attachments=[upload_id]
    )
    assert got["ok"] is False and got["error"].startswith("unsupported")


def test_read_one_post(daemon: Any, tmp_path: Path) -> None:
    asyncio.run(daemon.registry.register("babel", None, "s1"))
    h = RichHarness(daemon, UploadStore(tmp_path / "u"), ["mattermost:dev", "discord"])
    got = h.call("read", dev_name="babel", backend="discord", channel="c", post_id="42")
    assert [m["text"] for m in got["result"]["messages"]] == ["just 42"]
    assert h.backends["discord"].calls == [("read_one", "babel", "c", "42")]


def test_guild_ops_are_discords(daemon: Any) -> None:
    """A Mattermost backend answers `unsupported`; Discord's errors map to the frame's."""
    asyncio.run(daemon.registry.register("babel", None, "s1"))
    h = Harness(daemon)
    for op, args in (
        ("list_channels", {}),
        ("resolve", {"name": "x"}),
        ("create_channel", {"name": "x"}),
        ("create_thread", {"channel": "c", "name": "x"}),
    ):
        got = h.call(op, dev_name="babel", backend="mattermost:dev", **args)
        assert got["ok"] is False and got["error"].startswith("unsupported"), op


def test_unavailable_and_not_found_map(daemon: Any) -> None:
    asyncio.run(daemon.registry.register("babel", None, "s1"))
    h = Harness(daemon)
    h.discord.fail = BackendUnavailable("scream-hole unhealthy")
    got = h.call("send", dev_name="babel", backend="discord", to="c", text="x")
    assert got["error"] == "unavailable: scream-hole unhealthy"
    h.discord.fail = NotFound("message 1")
    got = h.call("send", dev_name="babel", backend="discord", to="c", text="x")
    assert got["error"].startswith("not_found")


# -- Story 2.4c: thread_backend (R-12) -------------------------------------------------------
SNOWFLAKE = "1300000000000000001"
MM_ID = "abcdefghijklmnopqrstuvwxyz"
MM_ID2 = "zyxwvutsrqponmlkjihgfedcba"


def two_servers(raw: dict[str, Any]) -> dict[str, Any]:
    raw["mattermost"].append({"id": "prod", "url": "http://mm2.invalid"})
    raw["crews"]["oaw"]["prod"] = {
        "broker_socket": "/run/crewbroker/oaw-prod.sock",
        "broker_user_id": "b-oaw-prod",
    }
    return raw


def test_thread_backend_order(
    make_daemon: Any, servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    """The shape picks the kind first, then the thread set, then a probe of only the agent's
    registered servers; an unknown shape is `bad_request`; nothing found is `not_found`."""
    d = make_daemon(two_servers(servers_toml))
    asyncio.run(d.registry.register("babel", None, "s1"))
    names = ["mattermost:dev", "mattermost:prod", "mattermost:other", "discord"]
    h = RichHarness(d, UploadStore(tmp_path / "u"), names)
    dev, prod, other = (h.backends[n] for n in names[:3])

    def tb(post_id: str) -> dict[str, Any]:
        return h.call("thread_backend", dev_name="babel", post_id=post_id)

    # (1) The shape: a snowflake is Discord, no probe at all.
    assert tb(SNOWFLAKE)["result"] == {"backend": "discord"}
    for bad in ("p1", "12345", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "x" * 27, "c1/123"):
        got = tb(bad)
        assert got["ok"] is False and got["error"].startswith("bad_request"), bad
    assert not [c for b in h.backends.values() for c in b.calls if c[0] == "has_post"]

    # (2) The thread set picks the server, without a probe.
    h.threads.add("babel", MM_ID, "mattermost:prod")
    assert tb(MM_ID)["result"] == {"backend": "mattermost:prod"}
    assert not [c for b in h.backends.values() for c in b.calls if c[0] == "has_post"]

    # (3) Otherwise a probe, on only the servers the agent is registered on; the one that
    # holds the post (200) answers. `mattermost:other` holds it too, but babel isn't on it.
    other.posts.add(MM_ID2)
    prod.posts.add(MM_ID2)
    assert tb(MM_ID2)["result"] == {"backend": "mattermost:prod"}
    assert [c[0] for c in dev.calls] == ["has_post"]
    assert other.calls == []

    # Nothing anywhere: not_found, loudly.
    got = tb("q" * 26)
    assert got["ok"] is False and got["error"].startswith("not_found")
    assert tb(MM_ID)["result"]["backend"] == "mattermost:prod"
    assert h.call("thread_backend", dev_name="nobody", post_id=MM_ID)["error"].startswith(
        "not_registered"
    )

    # Discord without a Discord registration is not_found too, never a guess.
    servers_toml["discord"] = {"enabled": False}
    nd = make_daemon(servers_toml)
    asyncio.run(nd.registry.register("babel", None, "s1"))
    got = RichHarness(nd, UploadStore(tmp_path / "v"), names).call(
        "thread_backend", dev_name="babel", post_id=SNOWFLAKE
    )
    assert got["error"].startswith("not_found")


def test_create_thread_with_a_first_message(daemon: Any, tmp_path: Path) -> None:
    asyncio.run(daemon.registry.register("babel", None, "s1"))

    class Guild(RichBackend):
        async def create_thread(self, dev_name: str, channel: str, name: str) -> dict[str, Any]:
            self.calls.append(("create_thread", dev_name, channel, name))
            return {"id": "9001", "name": name, "channel": "100"}

        async def list_channels(self, *a: Any) -> list[dict[str, Any]]:
            return []

        async def resolve(self, *a: Any) -> dict[str, Any]:
            return {}

        async def create_channel(self, *a: Any) -> dict[str, Any]:
            return {}

    h = RichHarness(daemon, UploadStore(tmp_path / "u"), ["mattermost:dev"])
    guild = Guild("discord")
    h.backends["discord"] = guild
    handlers = build_handlers(daemon.config, daemon.registry)
    handlers.update(
        build_chat_handlers(daemon.registry, h.backends, h.threads, AgentContext(), None)
    )
    h.server = DaemonServer(handlers)
    got = h.call(
        "create_thread", dev_name="babel", backend="discord", channel="ops", name="t", message="hi"
    )
    assert got["ok"] is True and got["result"]["id"] == "9001" and got["result"]["post_id"]
    assert guild.calls[-1] == ("send", "babel", "100", "hi", "9001")
    assert h.threads.backend_of("babel", "9001") == "discord"
