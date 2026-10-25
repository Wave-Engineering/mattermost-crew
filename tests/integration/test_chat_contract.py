"""Stories 2.4 and 2.4c: the thin MCP shim over the daemon's socket. IT-04, IT-05, IT-15,
IT-16 and IT-25.

The daemon side is wired as `crewchat serve` wires it, on a real unix socket: the registry, the
token store, one `MattermostBackend` per configured server (over in-process brokers, as in
`test_mattermost_adapter`), the Discord adapter over the scream-hole and Discord REST fake, and
`build_handlers` plus `build_chat_handlers`. The shim is the real `Shim`, talking to that socket
through `SocketTransport`, as an agent's session does.

- IT-04 (R-11): the default comes from the identity file, then the defaults file; with neither,
  a headless send fails loudly, leaves a squawk on the crew's needs list, and sends nothing.
- IT-05 (R-12, R-13, R-15): a reply follows its thread's backend, whatever the default, through
  the daemon's `thread_backend` (the shim keeps no map); a named backend is used, and the daemon
  logs `off-default`. `chat_send` with `attach_path` and `embed` on Mattermost posts a file and
  a message attachment, checked through `GET /api/v4/posts/{id}`.
- IT-15 (R-15): today's Discord operations, through `chat_*` and through each of the nine
  `disc_*` aliases: `disc_send` (with an attachment and an embed), `disc_read` (with
  `message_id`), `disc_list`, `disc_resolve`, `disc_create_channel`, `disc_create_thread`,
  and Story 2.4's `disc_thread`, `disc_dm` and `disc_react`. Each forwards and warns once.
- IT-16 (R-09, R-10): one agent registered on two Mattermost servers, each with its own
  (crew, server) broker and token, plus Discord, sends and receives on all of them through the
  one contract. CI has one Mattermost per leg, so the two server ids point at that one server;
  each has its own broker instance, state, token and websocket.
- IT-25 (R-15, D-106): with the scream-hole fake failing its health check, `chat_read` and
  `disc_read` on Discord answer `unavailable: scream-hole unhealthy` and the Discord REST fake
  sees no read; `chat_send` still posts, through the direct fallback; once the fake is healthy
  again, the next read is served through scream-hole.

IT-15 and IT-25 need no Mattermost and always run; the others skip without one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from crewbroker import mint
from crewbroker.config import CrewConfig
from crewbroker.mattermost import MattermostClient as BrokerMattermost
from crewbroker.service import Broker
from crewchat.adapters.discord import DiscordBackend
from crewchat.adapters.mattermost import MattermostBackend
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.config import parse_config
from crewchat.filters import SwitchCache
from crewchat.registry import Registry
from crewchat.server import DaemonServer, build_handlers
from crewchat.threads import MyThreads
from crewchat.tokens import TokenStore
from crewchat.uploads import UploadStore
from crewchat_clients.mcp_shim import ALIASES, Shim, ShimError, SocketTransport

from tests.fakes.screamhole import (
    BOT_TOKEN,
    DISCORD_API,
    GUILD_ID,
    SCREAMHOLE_URL,
    FakeDiscord,
)
from tests.integration.mmhttp import mm_request
from tests.integration.test_mattermost_adapter import (  # noqa: F401 (`setup` is a fixture)
    Inbox,
    InProcessBroker,
    as_admin_post,
    setup,
)

DEV = "shimmy"
IT16_DEV = "shim-it16"
HUMAN = "500000000000000001"


class Squawks:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Sequence[str], cwd: Path) -> int:
        self.calls.append(list(argv))
        return 0


class Recording:
    """The shim's transport, with every op it sent recorded."""

    def __init__(self, inner: SocketTransport) -> None:
        self.inner = inner
        self.ops: list[str] = []

    async def __call__(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.ops.append(op)
        return await self.inner(op, args)


class Host:
    """One host's daemon on a socket: Mattermost servers `server_ids`, and Discord."""

    def __init__(
        self,
        root: Path,
        discord: FakeDiscord,
        *,
        mattermost: Any = None,
        setup: dict[str, str] | None = None,  # noqa: F811 (the module fixture's value)
        server_ids: Sequence[str] = (),
        now: Callable[[], float] | None = None,
    ) -> None:
        mint.clear_expiry_cache()
        self.root = root
        self.discord = discord
        self.mattermost = mattermost
        self.setup = setup
        crew = setup["crew"] if setup else "oaw"
        self.crew = crew
        brokers: dict[tuple[str, str], InProcessBroker] = {}
        self.brokers = brokers
        crew_table: dict[str, Any] = {"needs_list": f"{crew}:needs"}
        for sid in server_ids:
            assert mattermost is not None and setup is not None
            broker = Broker(
                CrewConfig(
                    crew=crew,
                    server_id=sid,
                    server_url=mattermost.url,
                    human_user_ids=[setup["partner_id"]],
                    trusted=[],
                    broker_user_id=setup["broker_id"],
                ),
                BrokerMattermost(mattermost.url, setup["broker_token"]),
                root / f"broker-{sid}",
            )
            brokers[(crew, sid)] = InProcessBroker(broker)
            crew_table[sid] = {
                "broker_socket": "/nonexistent/broker.sock",
                "broker_user_id": setup["broker_id"],
            }
        config = parse_config(
            {
                "default_crew": crew,
                "mattermost": [{"id": sid, "url": mattermost.url} for sid in server_ids],
                "discord": {"enabled": True},
                "crews": {crew: crew_table},
            }
        )
        self.store = TokenStore(root / "tokens", brokers)
        self.mm = {
            sid: MattermostBackend(sid, mattermost.url, self.store.token_for, refresher=self.store)
            for sid in server_ids
        }
        self.store.adapters.extend(self.mm.values())
        cred = root / "discord.credential"
        cred.write_text(BOT_TOKEN + "\n")
        cred.chmod(0o600)
        self.dc = DiscordBackend(
            SCREAMHOLE_URL,
            cred,
            discord_api=DISCORD_API,
            http_client=discord.http_client(),
            guild_id=GUILD_ID,
            **({"now": now} if now is not None else {}),
        )
        self.registry = Registry(config, brokers, self.store, SwitchCache(brokers))
        backends: dict[str, Any] = {b.name: b for b in self.mm.values()}
        backends[self.dc.name] = self.dc
        # As `crewchat serve` wires them: uploads beside the socket, Discord's own health.
        self.uploads = UploadStore(root / "uploads")
        self.threads = MyThreads()
        handlers = build_handlers(config, self.registry, {self.dc.name: self.dc})
        handlers.update(
            build_chat_handlers(
                self.registry, backends, self.threads, AgentContext(), self.uploads
            )
        )
        self.server = DaemonServer(handlers)

    @asynccontextmanager
    async def serving(self, sock: Path) -> AsyncIterator[None]:
        listener = await self.server.start(sock, group=None)
        try:
            yield
        finally:
            listener.close()
            await listener.wait_closed()
            for b in self.mm.values():
                await b.aclose()
            await self.dc.aclose()

    def seat(self, account: str) -> str:
        """Seat the agent's bot in the module's team and channel. Its user ID."""
        assert self.mattermost is not None and self.setup is not None
        user_id = str(mm_request(self.mattermost, "GET", f"/users/username/{account}").json()["id"])
        team, channel = self.setup["team_id"], self.setup["channel_id"]
        resp = mm_request(
            self.mattermost,
            "POST",
            f"/teams/{team}/members",
            json={"team_id": team, "user_id": user_id},
        )
        assert resp.status_code == 201, resp.text
        resp = mm_request(
            self.mattermost, "POST", f"/channels/{channel}/members", json={"user_id": user_id}
        )
        assert resp.status_code == 201, resp.text
        return user_id


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="cc-", dir="/tmp") as d:
        yield Path(d)


def make_repo(root: Path, dev: str = DEV, **identity: Any) -> Path:
    """The agent's repo. The module's crew is shared, so each test uses its own Dev-Name:
    a fresh broker state would see another test's bot account as squatted."""
    repo = root / "repo"
    (repo / ".claude").mkdir(parents=True, exist_ok=True)
    ident = {"dev_name": dev, "dev_team": "oaw", **identity}
    (repo / ".claude" / "agent-identity.json").write_text(json.dumps(ident))
    return repo


def make_shim(
    repo: Path, sock: Path, home: Path, *, headless: bool = True, squawks: Squawks | None = None
) -> tuple[Shim, Recording]:
    transport = Recording(SocketTransport(sock, timeout_s=30))
    shim = Shim(
        repo,
        transport,
        headless=headless,
        defaults_file=home / ".claude" / "chat-defaults.json",
        squawk=squawks or Squawks(),
        remote_for=lambda _root: "git@github.com:Wave-Engineering/mattermost-crew.git",
        session_id="it-session",
        environ={},
    )
    return shim, transport


def post(mattermost: Any, post_id: str) -> dict[str, Any]:
    resp = mm_request(mattermost, "GET", f"/posts/{post_id}")
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()
    return data


# -- IT-04 -------------------------------------------------------------------------------------
def test_it04_default_resolution_and_headless_squawk(
    mattermost: Any, setup: dict[str, str], tmp_path: Path, sock_dir: Path  # noqa: F811
) -> None:
    """IT-04 (R-11)."""
    channel = setup["channel_id"]
    sock = sock_dir / "crewchat.sock"
    host = Host(tmp_path, FakeDiscord(), mattermost=mattermost, setup=setup, server_ids=["ci"])
    home = tmp_path / "home"
    key = "github.com/Wave-Engineering"

    async def scenario() -> None:
        async with host.serving(sock):
            # (3) No default at all, headless: a loud failure, a squawk, and nothing sent.
            squawks = Squawks()
            shim, transport = make_shim(
                make_repo(tmp_path / "a", "shim-it04"), sock, home, squawks=squawks
            )
            await shim.start()
            host.seat(shim.registration["account"])
            with pytest.raises(ShimError, match="NO DEFAULT CHAT BACKEND") as exc:
                await shim.chat_send(channel, "it04 must not be sent")
            assert "no fallback" in str(exc.value)
            assert squawks.calls and squawks.calls[0][:2] == ["add", f"{host.crew}:needs"]
            assert "it04 must not be sent" not in squawks.calls[0][2]
            assert "send" not in transport.ops
            assert host.discord.messages == {}  # no Discord fallback

            # (2) The human's answer, in the defaults file: the send goes there.
            await shim.chat_set_default(key, "mattermost:ci")
            sent = await shim.chat_send(channel, "it04 via the defaults file")
            assert sent["backend"] == "mattermost:ci"
            assert post(mattermost, sent["post_id"])["message"] == "it04 via the defaults file"

            # (1) The identity file's home_backend beats the defaults file.
            shim2, _ = make_shim(
                make_repo(tmp_path / "b", "shim-it04", home_backend="discord"), sock, home
            )
            chan = host.discord.add_channel("agent-ops")
            sent = await shim2.chat_send(chan, "it04 via home_backend")
            assert sent["backend"] == "discord"
            assert host.discord.messages[chan][-1]["content"].startswith("it04 via home_backend")

    asyncio.run(scenario())


# -- IT-05 -------------------------------------------------------------------------------------
def test_it05_reply_follows_thread_and_named_backend_is_logged(
    mattermost: Any,
    setup: dict[str, str],  # noqa: F811
    tmp_path: Path,
    sock_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """IT-05 (R-12, R-13)."""
    channel = setup["channel_id"]
    sock = sock_dir / "crewchat.sock"
    host = Host(tmp_path, FakeDiscord(), mattermost=mattermost, setup=setup, server_ids=["ci"])
    caplog.set_level(logging.INFO, logger="crewchat.chat")

    async def scenario() -> None:
        async with host.serving(sock):
            repo = make_repo(tmp_path, "shim-it05", home_backend="discord")
            shim, _ = make_shim(repo, sock, tmp_path)
            await shim.start()
            host.seat(shim.registration["account"])

            # R-13: a named backend is used, and the daemon logs it as off-default.
            root = await shim.chat_send(channel, "it05 root", backend="mattermost:ci")
            assert root["backend"] == "mattermost:ci"
            logged = [r.getMessage() for r in caplog.records]
            assert "off-default: shim-it05 mattermost:ci" in logged

            # R-12: the reply goes to the thread's backend and thread, not the default.
            reply = await shim.chat_send(channel, "it05 reply", thread=root["post_id"])
            assert reply["backend"] == "mattermost:ci" and reply["thread"] == root["post_id"]
            assert post(mattermost, reply["post_id"])["root_id"] == root["post_id"]
            thread = await shim.chat_thread(root["post_id"])
            assert [m["text"] for m in thread["messages"]] == ["it05 root", "it05 reply"]
            assert host.discord.messages == {}
            # A reply is not an off-default choice: one off-default line, for the root only.
            assert sum("off-default" in r.getMessage() for r in caplog.records) == 1

            # A thread the agent only read is followed too: a human's root on Mattermost,
            # which the thread set never recorded, placed by the daemon's probe (R-12).
            human = as_admin_post(mattermost, channel, "it05 human root")
            await shim.chat_read(channel, limit=5, backend="mattermost:ci")
            answer = await shim.chat_send(channel, "it05 answer", thread=human["id"])
            assert answer["backend"] == "mattermost:ci"
            assert post(mattermost, answer["post_id"])["root_id"] == human["id"]
            # A new shim instance (a session restart) replies on the old thread the same way:
            # the shim kept nothing to lose.
            again, transport = make_shim(repo, sock, tmp_path)
            await again.start()
            late = await again.chat_send(channel, "it05 after restart", thread=root["post_id"])
            assert late["backend"] == "mattermost:ci"
            assert post(mattermost, late["post_id"])["root_id"] == root["post_id"]
            assert "thread_backend" in transport.ops

            # R-15: attach_path and embed on Mattermost: a file, and a message attachment.
            report = tmp_path / "it05-report.txt"
            report.write_bytes(b"it05 attachment bytes\n")
            rich = await shim.chat_send(
                channel,
                "it05 with a file",
                backend="mattermost:ci",
                attach_path=str(report),
                embed="IT-05: file: and embed",
            )
            stored = post(mattermost, rich["post_id"])
            assert len(stored["file_ids"]) == 1
            info = mm_request(mattermost, "GET", f"/files/{stored['file_ids'][0]}/info").json()
            assert info["name"] == "it05-report.txt" and info["size"] == len(report.read_bytes())
            assert stored["props"]["attachments"][0]["title"] == "IT-05"
            assert stored["props"]["attachments"][0]["text"] == "file: and embed"
            assert stored["props"]["crew_proto"] == 1
            assert list((tmp_path / "uploads").iterdir()) == []  # deleted after use

    asyncio.run(scenario())


# -- IT-15 -------------------------------------------------------------------------------------
def test_it15_discord_through_chat_and_aliases(
    tmp_path: Path, sock_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """IT-15 (R-15): send, read, thread, DM and react on Discord, through `chat_*` and
    through each deprecated `disc_*` alias, with reads only through scream-hole."""
    discord = FakeDiscord()
    chan = discord.add_channel("agent-ops")
    sock = sock_dir / "crewchat.sock"
    host = Host(tmp_path, discord)
    caplog.set_level(logging.WARNING, logger="crewchat_clients.mcp_shim")
    human = {"id": HUMAN, "username": "bj", "bot": False}

    async def scenario() -> None:
        async with host.serving(sock):
            shim, _ = make_shim(make_repo(tmp_path, home_backend="discord"), sock, tmp_path)
            await shim.start()
            assert shim.backends() == ["discord"]

            # chat_*
            sent = await shim.chat_send(chan, "it15 chat send")
            stored = discord.messages[chan][-1]
            assert stored["id"] == sent["post_id"]
            assert stored["content"].startswith("it15 chat send\n\n— **shimmy**")
            assert stored["content"].endswith("·p1")
            theirs = discord.add_message(chan, "it15 from a human", author=human)
            read = await shim.chat_read(chan)
            assert [m["post_id"] for m in read["messages"]][-2:] == [sent["post_id"], theirs["id"]]
            assert read["messages"][-1]["author_name"] == "bj"
            assert read["messages"][-2]["proto"] == 1 and read["messages"][-1]["proto"] == 0
            tid = discord.add_channel("it15-thread", type=11)
            discord.add_message(tid, "it15 thread starter", author=human)
            reply = await shim.chat_send(chan, "it15 thread reply", thread=tid)
            assert discord.messages[tid][-1]["id"] == reply["post_id"]
            thread = await shim.chat_thread(tid)
            assert thread["messages"][0]["text"] == "it15 thread starter"
            dm = await shim.chat_dm(HUMAN, "it15 chat dm")
            assert discord.messages[dm["channel"]][-1]["content"].startswith("it15 chat dm")
            await shim.chat_react(sent["post_id"], "👍")
            assert discord.reactions[-1] == (chan, sent["post_id"], "👍")

            # disc_* aliases: the same operations, each forwarding with backend=discord, with
            # today's argument names and shapes.
            report = tmp_path / "it15-report.txt"
            report.write_bytes(b"it15 attachment bytes\n")
            a = await shim.disc_send(
                "#agent-ops",  # a name, resolved by the daemon against the guild
                "it15 alias send",
                attach_path=str(report),
                embed="IT-15: all: green",
            )
            stored = discord.messages[chan][-1]
            assert stored["id"] == a["post_id"]
            assert stored["embeds"] == [{"title": "IT-15", "description": "all: green"}]
            assert [x["filename"] for x in stored["attachments"]] == ["it15-report.txt"]
            assert discord.files[(a["post_id"], "it15-report.txt")] == report.read_bytes()
            assert list((tmp_path / "uploads").iterdir()) == []  # deleted after use
            await shim.disc_send("agent-ops", content="it15 alias content")
            got = await shim.disc_read(chan, limit=2)
            assert [m["text"].split("\n")[0] for m in got["messages"]] == [
                "it15 alias send",
                "it15 alias content",
            ]
            one = await shim.disc_read("agent-ops", message_id=sent["post_id"])
            assert [m["post_id"] for m in one["messages"]] == [sent["post_id"]]
            listed = await shim.disc_list(GUILD_ID)
            assert {"id": chan, "name": "agent-ops", "type": "text"} in listed["channels"]
            found = await shim.disc_resolve(GUILD_ID, "#agent-ops")
            assert found["id"] == chan
            made = await shim.disc_create_channel(GUILD_ID, "IT15 Room")
            assert discord.channels[made["id"]]["name"] == "it15-room"
            thr = await shim.disc_create_thread("agent-ops", "it15 thread", message="first")
            assert discord.channels[thr["id"]]["parent_id"] == chan
            assert discord.messages[thr["id"]][-1]["content"].startswith("first")
            with pytest.raises(ShimError, match="bad_request"):
                await shim.disc_list("900000000000000999")  # another guild: refused
            with pytest.raises(ShimError, match="unknown channel 'no-such-room'"):
                await shim.disc_send("no-such-room", "never sent")
            t = await shim.disc_thread(tid)
            assert [m["post_id"] for m in t["messages"]][-1] == reply["post_id"]
            d = await shim.disc_dm(HUMAN, "it15 alias dm")
            assert discord.messages[d["channel"]][-1]["id"] == d["post_id"]
            await shim.disc_react(a["post_id"], "✅", channel_id=chan)
            assert discord.reactions[-1] == (chan, a["post_id"], "✅")
            await shim.disc_send(chan, "it15 alias send again")

    asyncio.run(scenario())
    assert discord.direct_reads == []  # D-013, D-106: reads never hit Discord
    assert {s for s, _m, _p in discord.requests} == {"scream-hole"}  # healthy: all through it
    warnings = [r.getMessage() for r in caplog.records if "deprecated" in r.getMessage()]
    assert len(ALIASES) == 9
    assert sorted(w.split()[1] for w in warnings) == sorted(ALIASES)  # each warns, once


# -- IT-25 -------------------------------------------------------------------------------------
def test_it25_screamhole_unhealthy_refuses_reads(tmp_path: Path, sock_dir: Path) -> None:
    """IT-25 (R-15, D-106): no Discord read ever reaches Discord; writes fall back."""
    clock = [1_800_000_000.0]
    discord = FakeDiscord(now=lambda: clock[0])
    chan = discord.add_channel("agent-ops")
    early = discord.add_message(chan, "it25 before", author={"id": HUMAN, "username": "bj"})
    sock = sock_dir / "crewchat.sock"
    host = Host(tmp_path, discord, now=lambda: clock[0])

    async def scenario() -> None:
        async with host.serving(sock):
            shim, _ = make_shim(make_repo(tmp_path, "shim-it25", home_backend="discord"), sock,
                                tmp_path)
            await shim.start()
            assert (await shim.chat_health())["backends"]["discord"]["state"] == "ok"

            discord.screamhole_healthy = False
            clock[0] += host.dc.healthy_recheck_s  # the next call re-checks scream-hole
            mark = len(discord.requests)
            with pytest.raises(ShimError, match="unavailable: scream-hole unhealthy"):
                await shim.chat_read(chan)
            with pytest.raises(ShimError, match="unavailable: scream-hole unhealthy"):
                await shim.disc_read(chan)
            with pytest.raises(ShimError, match="unavailable: scream-hole unhealthy"):
                await shim.disc_read(chan, message_id=early["id"])
            health = (await shim.chat_health())["backends"]["discord"]
            assert health["state"] == "degraded"
            # A write still posts, straight to the Discord REST API.
            sent = await shim.chat_send(chan, "it25 while scream-hole is down")
            assert discord.messages[chan][-1]["id"] == sent["post_id"]
            during = discord.requests[mark:]
            assert ("discord", "POST", f"/channels/{chan}/messages") in during
            assert [r for r in during if r[0] == "discord" and r[1] == "GET"] == []

            # Healthy again: once the backoff is up, the next read is served by scream-hole.
            discord.screamhole_healthy = True
            clock[0] += 60.0
            mark = len(discord.requests)
            got = await shim.chat_read(chan)
            assert [m["post_id"] for m in got["messages"]][-1] == sent["post_id"]
            assert any(
                s == "scream-hole" and m == "GET" and f"/channels/{chan}/messages" in p
                for s, m, p in discord.requests[mark:]
            )
            assert (await shim.chat_health())["backends"]["discord"]["state"] == "ok"

    asyncio.run(scenario())
    assert discord.direct_reads == []  # the Discord REST fake saw no read, ever
    assert not [r for r in discord.requests if r[0] == "discord" and r[1] == "GET"]


# -- IT-16 -------------------------------------------------------------------------------------
def test_it16_one_contract_across_two_mattermost_servers_and_discord(
    mattermost: Any, setup: dict[str, str], tmp_path: Path, sock_dir: Path  # noqa: F811
) -> None:
    """IT-16 (R-09, R-10)."""
    channel = setup["channel_id"]
    sock = sock_dir / "crewchat.sock"
    discord = FakeDiscord()
    chan = discord.add_channel("agent-ops")
    servers = ["ci-a", "ci-b"]
    host = Host(tmp_path, discord, mattermost=mattermost, setup=setup, server_ids=servers)
    # Both server ids are this leg's one Mattermost, so there is one bot account for the agent.
    # On two real servers each broker creates its own; here ci-a's broker creates it, and
    # ci-b's broker records it as minted, as its own create would have.
    account = f"{IT16_DEV}.{host.crew}"
    first, second = (host.brokers[(host.crew, sid)].broker for sid in servers)
    bot_id = mint.ensure_bot(first.client, account, first.accounts, setup["broker_id"])
    second.accounts.add_minted(account, bot_id)

    async def scenario() -> None:
        async with host.serving(sock):
            repo = make_repo(tmp_path, IT16_DEV, home_backend="mattermost:ci-a")
            shim, _ = make_shim(repo, sock, tmp_path)
            await shim.start()
            assert shim.backends() == ["mattermost:ci-a", "mattermost:ci-b", "discord"]
            assert shim.registration["account"] == account
            host.seat(account)
            # Each (crew, server) broker minted its own token for the agent.
            ta, tb = (host.store.token_for(IT16_DEV, sid) for sid in servers)
            assert ta and tb and ta != tb
            for t in (ta, tb):
                me = mm_request(mattermost, "GET", "/users/me", token=t)
                assert me.status_code == 200

            inboxes = {sid: Inbox(host.mm[sid], IT16_DEV) for sid in ("ci-a", "ci-b")}
            try:
                for inbox in inboxes.values():
                    await inbox.connected()
                # Send on each backend through the one contract.
                for name in ("mattermost:ci-a", "mattermost:ci-b"):
                    sent = await shim.chat_send(channel, f"it16 on {name}", backend=name)
                    assert sent["backend"] == name
                    assert post(mattermost, sent["post_id"])["message"] == f"it16 on {name}"
                    got = await shim.chat_read(channel, limit=5, backend=name)
                    assert sent["post_id"] in {m["post_id"] for m in got["messages"]}
                    assert all(m["backend"] == name for m in got["messages"])
                d = await shim.chat_send(chan, "it16 on discord", backend="discord")
                assert discord.messages[chan][-1]["id"] == d["post_id"]
                # Receive on each Mattermost server's own websocket, on its own token.
                text = as_admin_post(mattermost, channel, "it16 to everyone")["message"]
                for sid, inbox in inboxes.items():
                    got_msgs = await inbox.until(text)
                    assert got_msgs[-1].backend == f"mattermost:{sid}"
                    agent = host.mm[sid]._agents[IT16_DEV]
                    assert agent.ws_token == host.store.token_for(IT16_DEV, sid)
            finally:
                for inbox in inboxes.values():
                    await inbox.close()

    asyncio.run(scenario())
