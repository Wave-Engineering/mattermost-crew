"""Story 2.4: the daemon's chat ops and the shim's pushes (§5.3, §5.4; R-11, R-13, R-32)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import crewchat
import pytest
from crewchat.adapters.base import InboundMessage
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.config import ConfigError, parse_config
from crewchat.server import DaemonServer, build_handlers
from crewchat.threads import MyThreads

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
