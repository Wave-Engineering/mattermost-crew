"""The two-crew world the end-to-end flows run in (Story 3.8; E2E-01, E2E-02, E2E-03).

`scripts/ci/mattermost-up.sh` starts the server and exports `MM_URL` and `MM_ADMIN_TOKEN`;
without them every E2E test skips, as the integration tests do.

**The world** (`e2e`, one per session):

- Two crews, `oaw<run>` and `synth<run>`. The `<run>` suffix keeps a second run against the
  same server off the first run's bot accounts (`<dev>.<crew>` would be squatted). Each crew
  has its own human partner and its own broker instance, `<crew>-e2e`, laid out as an install
  (`<instance>.toml` and `<instance>.credential` in a config dir, a state root and a run
  dir) and started as `crewbroker serve` starts one: `crewbroker.config.load_config` with a
  client, `Broker`, `BrokerServer` on its unix socket, and the `DMListener` holding the
  broker account's websocket, so the partners flip the switch by DM over the real server.
  Both brokers run on this host, in this process; the one difference from `serve` is the
  peer UID the socket admits, which is this process's (it plays `crewchat`) instead of the
  `crewchat` user's. Their clock is the injected `Clock`, so social can lapse without waiting.
- Each crew lists the other in `trusted = [...]` by name, and also `hung<run>`: a third
  "broker" whose socket accepts and never answers, to prove a hung broker never stalls the
  others and that the other crews still resolve.
- `servers.toml` has a `[crews.<crew>.e2e]` table for all three, so the daemon fetches each
  crew's `crew_info` directly from that crew's own broker.
- An unlisted outsider (a human), and an outsider bot account named `x.<oaw crew>` that the
  admin owns, not the `oaw` broker: membership must ignore the suffix.
- Two agents per crew (`AGENTS`). Bot accounts are the brokers' and outlive a daemon, so each
  test's `Daemon` registers the agents it uses through its socket, as the shim does.

**The daemon** (`Daemon`, one per test) is wired as `crewchat serve` wires it, from that
`servers.toml`: `broker_clients` over the real broker sockets, the token store, the
Mattermost adapter, the switch cache (its 30 s refresh loop running), the registry, the
persisted my-threads, the filters, the doorbell with its cursors on disk, the kiosk (on a stub
model runner, since CI has no Claude Code), the social-engaged kiosk review, and every op on a
real `DaemonServer` socket. Agents register, send and push their kiosk brief over that socket,
and wake on the real `crewchat-doorbell` one-shot. It does not run the daily token rotation.

Every HTTP response the daemon and the brokers see goes through a `RateLimitCounter`, so a
test can assert how many `429`s the server sent (E2E-03).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from crewbroker import config as broker_cfg
from crewbroker import mint
from crewbroker.mattermost import MattermostClient as BrokerMattermost
from crewbroker.service import (
    BROKER_PROP,
    Broker,
    BrokerServer,
    DMListener,
    start_dm_listener,
)
from crewchat.__main__ import call
from crewchat.adapters.mattermost import MattermostBackend
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.config import load_config
from crewchat.doorbell import CursorStore, Doorbell, DoorbellEvent, build_doorbell_handlers
from crewchat.filters import Filters, KioskQueue, SwitchCache
from crewchat.kiosk import Kiosk
from crewchat.registry import Registry
from crewchat.server import DaemonServer, build_handlers
from crewchat.threads import MyThreads
from crewchat.tokens import TokenStore, broker_clients

from tests.integration.conftest import MattermostServer, mattermost_from_env
from tests.integration.mmhttp import mm_request
from tests.integration.test_filters import broker_account, login_user

SID = "e2e"
MM = f"mattermost:{SID}"
# Every daemon -> broker call's timeout. Long enough for a mint whose broker is waiting out
# a 429 (E2E-03 registers 30 agents through one broker account), and the bound on how long
# the hung broker can hold any one refresh.
BROKER_TIMEOUT_S = 10.0
AGENTS = {"oaw": ("babel", "kessel"), "synth": ("zed", "quill")}
EVENT_TIMEOUT_S = 40.0  # one doorbell wait: past a cache-miss round on the hung broker
QUIET_S = 3.0  # how long a doorbell must stay silent to count as "nothing more"
REPLY_TIMEOUT_S = 20.0
FAKE_BROKER_ID = "h" * 26  # the hung crew's broker account: never on the server
DOORBELL = Path(sys.executable).parent / "crewchat-doorbell"
EXIT_EVENT, EXIT_TIMEOUT = 0, 3  # crewchat_clients.doorbell's exit codes


class Clock:
    """The injected wall clock: the brokers', the switch cache's and the filters'."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class RateLimitCounter:
    """Counts the `429 Too Many Requests` responses the server sent, by who saw them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_source: dict[str, int] = {}

    def _seen(self, source: str, resp: httpx.Response) -> None:
        if resp.status_code == 429:
            with self._lock:
                self.by_source[source] = self.by_source.get(source, 0) + 1

    def sync_hook(self, source: str) -> Any:
        def hook(resp: httpx.Response) -> None:
            self._seen(source, resp)

        return hook

    def async_hook(self, source: str) -> Any:
        async def hook(resp: httpx.Response) -> None:
            self._seen(source, resp)

        return hook

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.by_source)

    def since(self, before: dict[str, int]) -> dict[str, int]:
        """The 429s seen since `before`, by source; only the sources that saw any."""
        now = self.snapshot()
        return {k: v - before.get(k, 0) for k, v in now.items() if v - before.get(k, 0)}


class HungBroker:
    """A broker socket that accepts every connection and never answers a frame."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.unlink(missing_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(64)
        self.sock.settimeout(0.2)
        self.held: list[socket.socket] = []
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, name="hung-broker", daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.accepted += 1
            self.held.append(conn)  # read nothing, answer nothing, close nothing

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        for conn in self.held:
            with contextlib.suppress(OSError):
                conn.close()
        self.sock.close()
        self.path.unlink(missing_ok=True)


class RunningBroker:
    """One broker instance, started from its config dir as `crewbroker serve` starts it."""

    def __init__(
        self,
        instance: str,
        *,
        config_dir: Path,
        state_root: Path,
        run_dir: Path,
        clock: Clock,
        counter: RateLimitCounter,
    ) -> None:
        self.instance = instance
        credential = broker_cfg.credential_path(instance, config_dir).read_text().strip()
        self.client = BrokerMattermost(broker_cfg.read_server_url(instance, config_dir), credential)
        self.client._http.event_hooks["response"].append(counter.sync_hook(f"broker {instance}"))
        config = broker_cfg.load_config(instance, config_dir=config_dir, client=self.client)
        state = broker_cfg.state_dir(instance, state_root)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.broker = Broker(config, self.client, state, clock=clock)
        self.socket = broker_cfg.socket_path(instance, run_dir)
        self.server = BrokerServer(self.socket, self.broker, os.getuid())
        self.stop = threading.Event()
        self._serve = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name=f"crewbroker-{instance}",
            daemon=True,
        )
        self._serve.start()
        self.listener = DMListener(self.broker, config.server_url, credential)
        self._dm = start_dm_listener(self.listener, self.stop)
        deadline = time.monotonic() + REPLY_TIMEOUT_S
        while not self.listener.connection_id:  # the server's `hello`: switch DMs are heard
            assert time.monotonic() < deadline, f"{instance}'s websocket never said hello"
            time.sleep(0.1)

    def close(self) -> None:
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self._dm.join(timeout=10)
        self.socket.unlink(missing_ok=True)
        self.client.close()


@dataclass
class Crew:
    key: str  # "oaw" or "synth"
    name: str  # the crew's name on the server: `<key><run>`
    instance: str
    broker_id: str
    partner_id: str
    partner_token: str
    agents: tuple[str, ...]
    running: RunningBroker

    @property
    def broker(self) -> Broker:
        return self.running.broker

    @property
    def socket(self) -> Path:
        return self.running.socket


@dataclass
class World:
    mattermost: MattermostServer
    root: Path
    run: str
    clock: Clock
    counter: RateLimitCounter
    team_id: str
    crews: dict[str, Crew]
    hung_crew: str
    hung: HungBroker
    outsider_id: str
    outsider_token: str
    spoof_id: str
    spoof_token: str
    servers_toml: Path
    channels: list[str] = field(default_factory=list)
    _daemons: int = 0

    # -- server helpers ---------------------------------------------------------------------
    def channel(self, label: str) -> str:
        """A new channel in the team, with both partners, the outsider and the spoofed bot."""
        name = f"{label}-{self.run}-{len(self.channels)}"
        resp = mm_request(
            self.mattermost,
            "POST",
            "/channels",
            json={"team_id": self.team_id, "name": name, "display_name": name, "type": "O"},
        )
        assert resp.status_code == 201, resp.text
        channel_id = str(resp.json()["id"])
        self.channels.append(channel_id)
        members = [c.partner_id for c in self.crews.values()]
        for user_id in [*members, self.outsider_id, self.spoof_id]:
            self.seat(user_id, channel_id)
        return channel_id

    def seat(self, user_id: str, channel_id: str | None = None) -> None:
        resp = mm_request(
            self.mattermost,
            "POST",
            f"/teams/{self.team_id}/members",
            json={"team_id": self.team_id, "user_id": user_id},
        )
        assert resp.status_code == 201, resp.text
        if channel_id is not None:
            resp = mm_request(
                self.mattermost,
                "POST",
                f"/channels/{channel_id}/members",
                json={"user_id": user_id},
            )
            assert resp.status_code == 201, resp.text

    def post(self, token: str, text: str, channel: str, *, root: str = "") -> str:
        body: dict[str, Any] = {"channel_id": channel, "message": text}
        if root:
            body["root_id"] = root
        resp = mm_request(self.mattermost, "POST", "/posts", token=token, json=body)
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    def user_id(self, username: str) -> str:
        resp = mm_request(self.mattermost, "GET", f"/users/username/{username}")
        assert resp.status_code == 200, resp.text
        return str(resp.json()["id"])

    # -- the switch, by DM ------------------------------------------------------------------
    def dm_with_broker(self, crew: Crew) -> str:
        resp = mm_request(
            self.mattermost,
            "POST",
            "/channels/direct",
            token=crew.partner_token,
            json=[crew.partner_id, crew.broker_id],
        )
        assert resp.status_code in (200, 201), resp.text
        return str(resp.json()["id"])

    def broker_posts(self, crew: Crew) -> list[dict[str, Any]]:
        """The broker's posts in its DM with the partner, oldest first."""
        channel = self.dm_with_broker(crew)
        resp = mm_request(
            self.mattermost,
            "GET",
            f"/channels/{channel}/posts",
            token=crew.partner_token,
            params={"per_page": 200},
        )
        assert resp.status_code == 200, resp.text
        posts = resp.json()["posts"].values()
        return sorted(
            (p for p in posts if p["user_id"] == crew.broker_id),
            key=lambda p: int(p["create_at"]),
        )

    def switch(self, crew: Crew, text: str) -> str:
        """The partner DMs `text` to the crew's broker; the broker's reply. Blocking: call it
        from a test's event loop through `asyncio.to_thread`."""
        channel = self.dm_with_broker(crew)
        seen = {p["id"] for p in self.broker_posts(crew)}
        self.post(crew.partner_token, text, channel)
        deadline = time.monotonic() + REPLY_TIMEOUT_S
        while True:
            for post in self.broker_posts(crew):
                new = post["id"] not in seen and (post.get("props") or {}).get(BROKER_PROP)
                # A lapse notice can land in the same DM; it is not this command's answer.
                if new and not str(post["message"]).startswith("social lapsed"):
                    return str(post["message"])
            assert time.monotonic() < deadline, f"{crew.instance} never answered {text!r}"
            time.sleep(0.3)

    def reset_switches(self) -> None:
        """Back to the defaults, crew only and the kiosk on, for the next test (R-20)."""
        for crew in self.crews.values():
            crew.broker.handle_dm(crew.partner_id, "crew")
            crew.broker.handle_dm(crew.partner_id, "kiosk on")


@pytest.fixture(scope="session")
def mattermost() -> MattermostServer:
    return mattermost_from_env(os.environ)


def _write_broker_config(
    config_dir: Path, crew: str, url: str, partner_id: str, trusted: list[str], token: str
) -> str:
    instance = f"{crew}-{SID}"
    trusted_toml = ", ".join(json.dumps(t) for t in trusted)
    broker_cfg.config_path(instance, config_dir).write_text(
        f'crew = "{crew}"\nserver_id = "{SID}"\nserver_url = "{url}"\n'
        f'human_user_ids = ["{partner_id}"]\ntrusted = [{trusted_toml}]\n',
        encoding="utf-8",
    )
    cred = broker_cfg.credential_path(instance, config_dir)
    cred.write_text(token + "\n", encoding="utf-8")
    cred.chmod(0o600)
    return instance


def _write_servers_toml(path: Path, world_crews: list[tuple[str, Path, str]], url: str) -> None:
    lines = [
        f'default_crew = "{world_crews[0][0]}"',
        f"broker_timeout_s = {BROKER_TIMEOUT_S}",
        "",
        "[[mattermost]]",
        f'id = "{SID}"',
        f'url = "{url}"',
        "kiosk = true",  # E2E-02's kiosk Q&A: off on a server unless it's set (#117)
        "",
        "[discord]",
        "enabled = false",
    ]
    for crew, sock, broker_id in world_crews:
        lines += [
            "",
            f"[crews.{crew}]",
            f'needs_list = "{crew}:needs"',
            "",
            f"[crews.{crew}.{SID}]",
            f'broker_socket = "{sock}"',
            f'broker_user_id = "{broker_id}"',
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _outsider_bot(mattermost: MattermostServer, username: str) -> tuple[str, str]:
    """A bot account the admin owns (not any broker), with a token: (user id, token)."""
    resp = mm_request(
        mattermost, "POST", "/bots", json={"username": username, "display_name": username}
    )
    assert resp.status_code == 201, resp.text
    bot_id = str(resp.json()["user_id"])
    tok = mm_request(mattermost, "POST", f"/users/{bot_id}/tokens", json={"description": "e2e"})
    assert tok.status_code == 200, tok.text
    return bot_id, str(tok.json()["token"])


@pytest.fixture(scope="session")
def e2e(mattermost: MattermostServer) -> Iterator[World]:
    run = secrets.token_hex(2)
    # Short: unix socket paths are capped at 108 bytes.
    root = Path(tempfile.mkdtemp(prefix="cc-e2e-", dir="/tmp"))
    config_dir, state_root, run_dir = root / "etc", root / "lib", root / "run"
    for d in (config_dir, state_root, run_dir):
        d.mkdir(mode=0o755)
    clock = Clock()
    counter = RateLimitCounter()
    names = {key: f"{key}{run}" for key in AGENTS}
    hung_crew = f"hung{run}"

    team = mm_request(
        mattermost,
        "POST",
        "/teams",
        json={"name": f"crew-e2e{run}", "display_name": f"e2e {run}", "type": "O"},
    )
    assert team.status_code == 201, team.text
    team_id = str(team.json()["id"])

    accounts: dict[str, dict[str, Any]] = {}
    for key, crew in names.items():
        broker_id, broker_token = broker_account(mattermost, f"crewbroker-{crew}-{SID}")
        partner_id, partner_token = login_user(mattermost, f"partner-{crew}")
        accounts[key] = {
            "broker": (broker_id, broker_token),
            "partner": (partner_id, partner_token),
        }
    outsider_id, outsider_token = login_user(mattermost, f"outsider-e2e{run}")
    spoof_id, spoof_token = _outsider_bot(mattermost, f"x.{names['oaw']}")

    running: dict[str, RunningBroker] = {}
    hung: HungBroker | None = None
    try:
        mint.clear_expiry_cache()
        for key, crew in names.items():
            others = [n for k, n in names.items() if k != key]
            partner_id = accounts[key]["partner"][0]
            instance = _write_broker_config(
                config_dir,
                crew,
                mattermost.url,
                partner_id,
                [*others, hung_crew],
                accounts[key]["broker"][1],
            )
            running[key] = RunningBroker(
                instance,
                config_dir=config_dir,
                state_root=state_root,
                run_dir=run_dir,
                clock=clock,
                counter=counter,
            )
        hung = HungBroker(broker_cfg.socket_path(f"{hung_crew}-{SID}", run_dir))
        servers_toml = config_dir / "servers.toml"
        _write_servers_toml(
            servers_toml,
            [
                *((names[k], running[k].socket, accounts[k]["broker"][0]) for k in names),
                (hung_crew, hung.path, FAKE_BROKER_ID),
            ],
            mattermost.url,
        )
        crews = {
            key: Crew(
                key=key,
                name=names[key],
                instance=running[key].instance,
                broker_id=accounts[key]["broker"][0],
                partner_id=accounts[key]["partner"][0],
                partner_token=accounts[key]["partner"][1],
                agents=AGENTS[key],
                running=running[key],
            )
            for key in names
        }
        world = World(
            mattermost=mattermost,
            root=root,
            run=run,
            clock=clock,
            counter=counter,
            team_id=team_id,
            crews=crews,
            hung_crew=hung_crew,
            hung=hung,
            outsider_id=outsider_id,
            outsider_token=outsider_token,
            spoof_id=spoof_id,
            spoof_token=spoof_token,
            servers_toml=servers_toml,
        )
        for user_id in [*(c.partner_id for c in crews.values()), outsider_id, spoof_id]:
            world.seat(user_id)
        yield world
    finally:
        for rb in running.values():
            rb.close()
        if hung is not None:
            hung.close()
        # Not configured brokers on this host: give the patched role back (IT-01, R-06).
        for acct in accounts.values():
            resp = mm_request(
                mattermost,
                "PUT",
                f"/users/{acct['broker'][0]}/roles",
                json={"roles": "system_user"},
            )
            assert resp.status_code == 200, resp.text
        shutil.rmtree(root, ignore_errors=True)


class StubRunner:
    """The kiosk's model runner: CI has no Claude Code. Records each prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def answer(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"We build a chat daemon (answer {len(self.prompts)})."


class Daemon:
    """The daemon, wired from `servers.toml` as `crewchat serve` wires it, for one test."""

    def __init__(self, world: World) -> None:
        world._daemons += 1
        self.world = world
        self.root = world.root / f"d{world._daemons}"
        self.state_dir = self.root / "state"
        tokens = self.root / "tokens"
        for d in (self.root, self.state_dir, tokens):
            d.mkdir(mode=0o700)
        self.sock = self.root / "crewchat.sock"
        self.ids: dict[str, str] = {}  # Dev-Name -> bot account user id
        self.runner = StubRunner()

        mint.clear_expiry_cache()
        config = load_config(world.servers_toml)
        self.config = config
        self.brokers = broker_clients(config)
        self.store = TokenStore(tokens, self.brokers)
        self.store.load()
        (server,) = config.mattermost
        self.backend = MattermostBackend(
            server.id, server.url, self.store.token_for, refresher=self.store
        )
        self.backend._shared_http().event_hooks["response"].append(
            world.counter.async_hook("daemon")
        )
        self.store.adapters.append(self.backend)
        backends = {self.backend.name: self.backend}
        self.cache = SwitchCache.from_config(self.brokers, config, clock=world.clock)
        self.registry = Registry(config, self.brokers, self.store, self.cache)
        self.threads = MyThreads.load(self.state_dir, config.my_threads_retention_days)
        self.filters = Filters.from_config(
            self.registry, self.cache, self.threads, config, clock=world.clock
        )
        self.kiosk_queue = KioskQueue()
        self.doorbell = Doorbell(
            dict(backends),
            CursorStore(self.state_dir),
            admit=self.filters.admit_event,
            kiosk=self.kiosk_queue,
        )
        self.store.adapters.insert(0, self.doorbell)
        self.context = AgentContext()
        self.kiosk = Kiosk(
            self.kiosk_queue,
            self.context.briefs,
            backends,
            self.runner,
            state_dir=self.state_dir,
            deliver=self.doorbell.deliver,
        )
        self.cache.social_listeners.append(
            lambda crew, server_id: self.kiosk.on_social(
                server_id, self.registry.agents_on(crew, server_id)
            )
        )
        handlers = build_handlers(config, self.registry, {})
        handlers.update(
            build_chat_handlers(
                self.registry, backends, self.threads, self.context, filters=self.filters
            )
        )
        handlers.update(build_doorbell_handlers(self.registry, self.doorbell, handlers["register"]))
        self.server = DaemonServer(handlers, hangup_cancels=frozenset({"subscribe"}))

    async def __aenter__(self) -> Daemon:
        await self.server.start(self.sock, group=None)
        self.stop = asyncio.Event()
        self.kiosk.start()
        # `switch_state` and `crew_info` from every broker, now and every 30 s (§5.9).
        self.refresher = asyncio.create_task(self.cache.run(self.stop))
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.stop.set()
        self.refresher.cancel()  # it may be waiting on the hung broker
        with contextlib.suppress(asyncio.CancelledError):
            await self.refresher
        await self.server.aclose()
        await self.doorbell.aclose()
        await self.kiosk.aclose()
        await self.backend.aclose()
        self.sock.unlink(missing_ok=True)
        await asyncio.to_thread(self.world.reset_switches)

    # -- the shim's side of the socket ------------------------------------------------------
    async def op(self, op: str, args: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        """One frame over the daemon's socket, as the shim sends it; the result."""
        reply = await asyncio.to_thread(call, str(self.sock), op, args, timeout)
        assert reply.get("ok") is True, (op, reply)
        result: dict[str, Any] = reply["result"]
        return result

    async def register(self, dev: str, crew: Crew, channels: list[str]) -> str:
        """Register `dev` in `crew` over the socket, seat its bot in `channels`, and wait for
        its stream. Its bot account's user id."""
        # A first connect without a cursor replays the last minute (REPLAY_MARGIN_S), which
        # would hand this test an earlier test's posts: start the cursor now.
        self.doorbell.cursors.advance(dev, MM, DoorbellEvent(MM, {}, ("e2e-start",), time.time()))
        result = await self.op(
            "register",
            {"dev_name": dev, "crew": crew.name, "session_id": f"e2e-{dev}", "dev_team": "oaw"},
        )
        assert result["account"] == f"{dev}.{crew.name}"
        assert result["backends"] == [MM]
        user_id = await asyncio.to_thread(self.world.user_id, result["account"])
        for channel in channels:
            await asyncio.to_thread(self.world.seat, user_id, channel)
        self.ids[dev] = user_id
        await self.connected(dev)
        repo = self.root / "agents" / dev
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "agent-identity.json").write_text(json.dumps({"dev_name": dev}))
        return user_id

    async def connected(self, dev: str) -> None:
        for _ in range(600):
            agent = self.backend._agents.get(dev)
            if agent is not None and agent.ws is not None and agent.hello:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"{dev}'s websocket never connected")

    async def send(self, dev: str, channel: str, text: str, thread: str | None = None) -> str:
        args: dict[str, Any] = {"dev_name": dev, "backend": MM, "to": channel, "text": text}
        if thread:
            args["thread"] = thread
        return str((await self.op("send", args))["post_id"])

    # -- the doorbell one-shot --------------------------------------------------------------
    async def doorbell_run(self, dev: str, wait_s: float) -> tuple[int, list[str]]:
        """One run of the real `crewchat-doorbell`, as the agent's background task runs it,
        waiting at most `wait_s`: its exit code and its stdout lines."""
        assert DOORBELL.exists(), f"{DOORBELL} is not installed: run uv sync"
        env = {
            **os.environ,
            "CREWCHAT_DOORBELL_TIMEOUT": f"{wait_s:g}",
            # The agents' seen sets (#109): the world's, so they outlive a daemon restart as an
            # agent's do, and never the runner's home.
            "XDG_STATE_HOME": str(self.world.root / "agent-state"),
        }
        proc = await asyncio.create_subprocess_exec(
            str(DOORBELL),
            "--socket",
            str(self.sock),
            "--repo-root",
            str(self.root / "agents" / dev),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), wait_s + 15)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        assert proc.returncode is not None
        if proc.returncode not in (EXIT_EVENT, EXIT_TIMEOUT):
            raise AssertionError(f"doorbell for {dev} exited {proc.returncode}: {err.decode()}")
        return proc.returncode, out.decode().splitlines()

    async def ring(self, dev: str, wait_s: float = EVENT_TIMEOUT_S) -> dict[str, Any]:
        """The agent's next event: the one-shot prints exactly one JSON line and exits 0."""
        code, lines = await self.doorbell_run(dev, wait_s)
        assert code == EXIT_EVENT, f"no event for {dev} within {wait_s:g} s"
        assert len(lines) == 1, lines
        event: dict[str, Any] = json.loads(lines[0])
        return event

    async def silent(self, dev: str, wait_s: float = QUIET_S) -> None:
        """The agent's doorbell has nothing more: the one-shot times out, printing nothing."""
        code, lines = await self.doorbell_run(dev, wait_s)
        assert (code, lines) == (EXIT_TIMEOUT, []), (dev, lines)

    async def delivered(self, dev: str, post_id: str) -> dict[str, Any]:
        """The agent's next event is the message `post_id`."""
        event = await self.ring(dev)
        assert event.get("type") == "message", event
        assert event["message"]["post_id"] == post_id, (dev, event["message"].get("text"))
        return event
