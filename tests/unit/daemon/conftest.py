"""Fakes for the daemon's unit tests: brokers, adapters and a clock.

`FakeBroker` stands in for one broker socket. Every broker call and adapter hook appends to
one shared `events` list, so a test can assert the order of a rotation across both.
"""

from __future__ import annotations

import copy
import itertools
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from crewchat.config import ServersConfig, parse_config
from crewchat.registry import Registry
from crewchat.tokens import BrokerClient, BrokerError, BrokerKey, SwitchCache, TokenStore

Event = tuple[Any, ...]

SERVERS_TOML: dict[str, Any] = {
    "default_crew": "oaw",
    "mattermost": [{"id": "dev", "url": "http://mm.invalid"}],
    "discord": {"enabled": True},
    "crews": {
        "oaw": {
            "needs_list": "bj:needs",
            "dev": {"broker_socket": "/run/crewbroker/oaw-dev.sock", "broker_user_id": "b-oaw"},
        },
    },
}


class FakeBroker(BrokerClient):
    """A broker that answers in process. Tokens are `tok-<n>` with id `id-<n>`."""

    _ids = itertools.count(1)

    def __init__(self, crew: str, server_id: str, events: list[Event]) -> None:
        name = f"{crew}-{server_id}"
        super().__init__(f"/run/crewbroker/{name}.sock", 2.0, name=name)
        self.crew = crew
        self.server_id = server_id
        self.events = events
        self.live: dict[str, str] = {}  # token_id -> account
        self.bots: list[str] = []
        self.fail: dict[str, BaseException] = {}
        self.on_revoke: Callable[[str], None] | None = None

    def ops(self, op: str) -> list[dict[str, Any]]:
        return [e[3] for e in self.events if e[0] == "broker" and e[1] == self.name and e[2] == op]

    def _token(self, account: str) -> dict[str, Any]:
        n = next(self._ids)
        self.live[f"id-{n}"] = account
        return {"token": f"tok-{n}", "token_id": f"id-{n}"}

    async def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.events.append(("broker", self.name, op, dict(args)))
        if op in self.fail:
            raise self.fail[op]
        if op == "mint":
            account = f"{args['dev_name']}.{args['crew']}"
            if account not in self.bots:
                self.bots.append(account)
            return {"account": account, **self._token(account)}
        if op == "remint":
            account = f"{args['dev_name']}.{args['crew']}"
            if account not in self.bots:
                raise BrokerError(op, "not_found: never registered")
            return self._token(account)
        if op == "revoke":
            if self.on_revoke is not None:
                self.on_revoke(args["token_id"])
            if self.live.pop(args["token_id"], None) is None:
                raise BrokerError(op, "not_found: no token with that id")
            return {}
        if op == "crew_info":
            return {
                "broker_user_id": f"b-{self.crew}",
                "human_user_ids": ["h1"],
                "trusted": [],
                "minted_bot_ids": [f"bot:{a}" for a in self.bots],
            }
        raise BrokerError(op, "unsupported")


class FakeAdapter:
    def __init__(self, events: list[Event]) -> None:
        self.events = events

    async def on_token_rotated(self, dev_name: str) -> None:
        self.events.append(("hook", dev_name))

    async def close_agent(self, dev_name: str) -> None:
        self.events.append(("close", dev_name))


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class Daemon:
    """A registry, a store and fake brokers, wired as `crewchat serve` wires them."""

    def __init__(self, config: ServersConfig, root: Path, clock: Clock) -> None:
        self.config = config
        self.events: list[Event] = []
        self.brokers: dict[BrokerKey, FakeBroker] = {
            (b.crew, b.server_id): FakeBroker(b.crew, b.server_id, self.events)
            for b in config.broker_entries()
        }
        self.adapter = FakeAdapter(self.events)
        self.store = TokenStore(root, self.brokers, [self.adapter])
        self.cache = SwitchCache(self.brokers)
        self.clock = clock
        self.registry = Registry(config, self.brokers, self.store, self.cache, clock=clock)

    def broker(self, crew: str = "oaw", server_id: str = "dev") -> FakeBroker:
        return self.brokers[(crew, server_id)]


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def servers_toml() -> dict[str, Any]:
    """A fresh copy of the parsed `servers.toml`, safe to change."""
    return copy.deepcopy(SERVERS_TOML)


@pytest.fixture
def servers_config() -> ServersConfig:
    return parse_config(SERVERS_TOML)


@pytest.fixture
def daemon(servers_config: ServersConfig, tmp_path: Path, clock: Clock) -> Daemon:
    return Daemon(servers_config, tmp_path / "tokens", clock)


@pytest.fixture
def make_daemon(tmp_path: Path, clock: Clock) -> Callable[[dict[str, Any]], Daemon]:
    def make(raw: dict[str, Any]) -> Daemon:
        return Daemon(parse_config(raw), tmp_path / "tokens", clock)

    return make


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # A unix socket path is limited to 108 bytes; pytest's tmp_path can be longer.
    with tempfile.TemporaryDirectory(prefix="cc-", dir="/tmp") as d:
        yield Path(d)
