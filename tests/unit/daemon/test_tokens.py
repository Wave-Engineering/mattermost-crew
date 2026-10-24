"""Story 1.4: broker clients, the token store, 401 refresh and rotation (§4.8; R-05, R-08, R-30)."""

from __future__ import annotations

import ast
import asyncio
import json
import stat
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import crewchat
import pytest
from crewchat.tokens import (
    BrokerClient,
    BrokerUnavailable,
    Rotator,
    TokenStore,
    Unauthorized,
    broker_clients,
)

PACKAGE = Path(crewchat.__file__).parent


def register(daemon: Any, dev: str = "babel", crew: str | None = "oaw") -> Any:
    return asyncio.run(daemon.registry.register(dev, crew, "s1"))


def steps(events: list[Any]) -> list[tuple[str, str]]:
    return [(e[0], e[2] if e[0] == "broker" else e[1]) for e in events]


class NoSleep:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def test_token_store_mode_0700(daemon: Any) -> None:
    register(daemon)
    root = daemon.store.root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    path = root / "babel.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in root.iterdir()] == ["babel.json"]  # no temporary file left
    data = json.loads(path.read_text())
    assert data["servers"]["dev"]["token"] == daemon.store.token_for("babel", "dev")

    # A restarted daemon reads it back, so rotation carries on.
    again = TokenStore(root, daemon.brokers)
    again.load()
    assert again.token_for("babel", "dev") == daemon.store.token_for("babel", "dev")
    assert again.agent("babel").last_register == daemon.clock.now


def test_token_store_tightens_an_existing_directory(tmp_path: Path) -> None:
    root = tmp_path / "tokens"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    TokenStore(root, {})
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_token_refresh_on_401(daemon: Any) -> None:
    register(daemon)
    old = daemon.store.agent("babel").servers["dev"]
    daemon.events.clear()
    broker = daemon.broker()
    at_revoke: list[str | None] = []
    broker.on_revoke = lambda _tid: at_revoke.append(daemon.store.token_for("babel", "dev"))
    used: list[str] = []

    async def request(token: str) -> str:
        used.append(token)
        if token == old.token:
            raise Unauthorized()
        return "posted"

    result = asyncio.run(daemon.store.call_with_refresh("babel", "dev", request))
    new = daemon.store.agent("babel").servers["dev"]
    assert result == "posted"
    assert used == [old.token, new.token]  # retried once, with the new token
    assert steps(daemon.events) == [
        ("broker", "remint"),
        ("hook", "babel"),  # the reconnect hook fires for that agent
        ("broker", "revoke"),
    ]
    assert broker.ops("mint") == []
    assert broker.ops("revoke") == [{"token_id": old.token_id}]
    assert at_revoke == [new.token]
    # remint never refreshes last_register (R-30).
    assert daemon.store.agent("babel").last_register == daemon.clock.now


def test_401_twice_is_not_retried_again(daemon: Any) -> None:
    register(daemon)
    calls: list[str] = []

    async def request(token: str) -> str:
        calls.append(token)
        raise Unauthorized()

    with pytest.raises(Unauthorized):
        asyncio.run(daemon.store.call_with_refresh("babel", "dev", request))
    assert len(calls) == 2
    assert len(daemon.broker().ops("remint")) == 1


def test_concurrent_401s_remint_once(daemon: Any) -> None:
    register(daemon)
    old = daemon.store.token_for("babel", "dev")

    async def request(token: str) -> str:
        await asyncio.sleep(0)
        if token == old:
            raise Unauthorized()
        return token

    async def both() -> list[str]:
        return list(
            await asyncio.gather(
                daemon.store.call_with_refresh("babel", "dev", request),
                daemon.store.call_with_refresh("babel", "dev", request),
            )
        )

    results = asyncio.run(both())
    assert results[0] == results[1] == daemon.store.token_for("babel", "dev")
    assert len(daemon.broker().ops("remint")) == 1


def test_crew_info_refreshed_after_mint(daemon: Any) -> None:
    assert daemon.cache.crew_info("oaw", "dev") is None
    register(daemon)
    ops = [e[2] for e in daemon.events if e[0] == "broker"]
    assert ops.index("crew_info") > ops.index("mint")
    info = daemon.cache.crew_info("oaw", "dev")
    assert info is not None and info["minted_bot_ids"] == ["bot:babel.oaw"]

    register(daemon, dev="kessel")
    assert daemon.cache.crew_info("oaw", "dev")["minted_bot_ids"] == [
        "bot:babel.oaw",
        "bot:kessel.oaw",
    ]


def test_switch_cache_keeps_last_good_crew_info(daemon: Any) -> None:
    register(daemon)
    good = daemon.cache.crew_info("oaw", "dev")
    daemon.broker().fail["crew_info"] = BrokerUnavailable("timeout")
    assert asyncio.run(daemon.cache.refresh("oaw", "dev")) == good
    assert daemon.cache.crew_info("oaw", "dev") == good
    assert asyncio.run(daemon.cache.refresh("otter", "dev")) is None


def _mint_calls() -> list[tuple[str, str]]:
    """Every `<expr>.mint(...)` call in the daemon package, as (file, enclosing function)."""
    found: list[tuple[str, str]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "mint"
                ):
                    found.append((path.name, func.name))
    return found


def test_only_register_calls_mint(daemon: Any, clock: Any) -> None:
    # Statically: the one call site of BrokerClient.mint is Registry.register.
    assert sorted(set(_mint_calls())) == [("registry.py", "register")]
    # And no string-dispatched "mint" frame anywhere else.
    for path in PACKAGE.rglob("*.py"):
        if path.name != "tokens.py":
            assert '"mint"' not in path.read_text(encoding="utf-8"), path.name

    # Dynamically: rotation and a 401 refresh use remint only.
    register(daemon)
    clock.now += timedelta(days=1)
    asyncio.run(Rotator(daemon.store, sleep=NoSleep()).run_daily(clock.now))
    token = daemon.store.token_for("babel", "dev")

    async def request(t: str) -> None:
        if t == token:
            raise Unauthorized()

    asyncio.run(daemon.store.call_with_refresh("babel", "dev", request))
    assert len(daemon.broker().ops("mint")) == 1
    assert len(daemon.broker().ops("remint")) == 2


def test_unregistered_30_days_closes_websocket(daemon: Any, clock: Any) -> None:
    register(daemon)
    held = daemon.store.agent("babel").servers["dev"]
    daemon.events.clear()
    report = asyncio.run(
        Rotator(daemon.store, sleep=NoSleep()).run_daily(clock.now + timedelta(days=31))
    )
    assert report.dropped == ["babel"] and report.rotated == []
    assert ("close", "babel") in daemon.events
    assert daemon.broker().ops("remint") == []
    assert daemon.store.agent("babel") is None
    assert not (daemon.store.root / "babel.json").exists()
    # Its token is not left live.
    assert held.token_id not in daemon.broker().live
    # And it stays out of rotation.
    daemon.events.clear()
    asyncio.run(Rotator(daemon.store, sleep=NoSleep()).run_daily(clock.now + timedelta(days=32)))
    assert daemon.events == []


def test_rotation_order(daemon: Any, clock: Any) -> None:
    register(daemon)
    register(daemon, dev="kessel")
    before = {d: daemon.store.agent(d).servers["dev"] for d in ("babel", "kessel")}
    daemon.events.clear()
    broker = daemon.broker()
    at_revoke: list[tuple[str, str | None]] = []
    owner = {before["babel"].token_id: "babel", before["kessel"].token_id: "kessel"}
    broker.on_revoke = lambda tid: at_revoke.append(
        (tid, daemon.store.token_for(owner[tid], "dev"))
    )
    sleep = NoSleep()
    clock.now += timedelta(hours=24)
    report = asyncio.run(Rotator(daemon.store, sleep=sleep).run_daily(clock.now))

    assert report.rotated == [("babel", "dev"), ("kessel", "dev")]
    assert steps(daemon.events) == [
        ("broker", "remint"),
        ("hook", "babel"),
        ("broker", "revoke"),
        ("broker", "remint"),
        ("hook", "kessel"),
        ("broker", "revoke"),
    ]
    # Revoke is never called before the swap: at each revoke the store holds the new token.
    for tid, current in at_revoke:
        assert current is not None and current != before[owner[tid]].token
    assert [a["token_id"] for a in broker.ops("revoke")] == [
        before["babel"].token_id,
        before["kessel"].token_id,
    ]
    # Paced at 5/s or less (R-05, CT-06).
    assert sleep.waits == [pytest.approx(0.2)]


def test_failed_revoke_is_retried(daemon: Any, clock: Any) -> None:
    register(daemon)
    old = daemon.store.agent("babel").servers["dev"]
    broker = daemon.broker()
    broker.fail["revoke"] = BrokerUnavailable("timeout")
    asyncio.run(daemon.store.rotate("babel", "dev"))
    assert daemon.store.agent("babel").pending_revoke == {"dev": [old.token_id]}
    assert old.token_id in broker.live
    del broker.fail["revoke"]
    asyncio.run(daemon.store.rotate("babel", "dev"))
    assert daemon.store.agent("babel").pending_revoke["dev"] == []
    assert old.token_id not in broker.live
    assert list(broker.live) == [daemon.store.agent("babel").servers["dev"].token_id]


def test_rotation_skips_unregistered_30_days(daemon: Any, clock: Any) -> None:
    start = clock.now
    register(daemon, dev="stale")
    clock.now = start + timedelta(days=2)
    register(daemon, dev="fresh")
    daemon.events.clear()
    now = start + timedelta(days=31)  # stale: 31 days; fresh: 29 days
    report = asyncio.run(Rotator(daemon.store, sleep=NoSleep()).run_daily(now))
    assert report.rotated == [("fresh", "dev")]
    assert report.dropped == ["stale"]
    assert [a["dev_name"] for a in daemon.broker().ops("remint")] == ["fresh"]


def test_broker_client_per_crew_and_server(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any]
) -> None:
    raw = servers_toml
    raw["mattermost"].append({"id": "prod", "url": "http://mm2.invalid"})
    raw["crews"]["oaw"]["prod"] = {
        "broker_socket": "/run/crewbroker/oaw-prod.sock",
        "broker_user_id": "b-oaw-prod",
    }
    raw["crews"]["otter"] = {
        "needs_list": "otter:needs",
        "dev": {"broker_socket": "/run/crewbroker/otter-dev.sock", "broker_user_id": "b-ot"},
        "prod": {"broker_socket": "/run/crewbroker/otter-prod.sock", "broker_user_id": "b-ot2"},
    }
    daemon = make_daemon(raw)
    clients = broker_clients(daemon.config)
    assert {k: c.socket_path for k, c in clients.items()} == {
        ("oaw", "dev"): "/run/crewbroker/oaw-dev.sock",
        ("oaw", "prod"): "/run/crewbroker/oaw-prod.sock",
        ("otter", "dev"): "/run/crewbroker/otter-dev.sock",
        ("otter", "prod"): "/run/crewbroker/otter-prod.sock",
    }
    assert all(c.timeout_s == 2.0 for c in clients.values())
    assert sorted(daemon.config.broker_user_ids()) == ["b-oaw", "b-oaw-prod", "b-ot", "b-ot2"]

    register(daemon, dev="babel", crew="oaw")
    register(daemon, dev="lutra", crew="otter")
    for crew, dev in (("oaw", "babel"), ("otter", "lutra")):
        for sid in ("dev", "prod"):
            assert daemon.broker(crew, sid).ops("mint") == [{"dev_name": dev, "crew": crew}]
    # token_for keys by server: each server's broker minted its own token.
    tokens = {
        (dev, sid): daemon.store.token_for(dev, sid)
        for dev in ("babel", "lutra")
        for sid in ("dev", "prod")
    }
    assert len(set(tokens.values())) == 4 and None not in tokens.values()
    assert daemon.store.token_for("babel", "nowhere") is None


def test_broker_call_timeout(short_dir: Path) -> None:
    hang_path = short_dir / "hang.sock"
    fast_path = short_dir / "fast.sock"

    async def hang(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        await asyncio.sleep(30)  # never answers

    async def fast(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = json.loads(await reader.readline())
        reply = {"proto": 1, "id": line["id"], "ok": True, "result": {"minted_bot_ids": []}}
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
        writer.close()

    async def main() -> None:
        hang_srv = await asyncio.start_unix_server(hang, path=str(hang_path))
        fast_srv = await asyncio.start_unix_server(fast, path=str(fast_path))
        try:
            slow = BrokerClient(hang_path, 0.3)
            ok = BrokerClient(fast_path, 0.3)

            async def timed(client: BrokerClient) -> tuple[Any, float]:
                start = time.monotonic()
                try:
                    return await client.crew_info(), time.monotonic() - start
                except BrokerUnavailable as exc:
                    return exc, time.monotonic() - start

            (slow_result, slow_s), (ok_result, ok_s) = await asyncio.gather(timed(slow), timed(ok))
            assert isinstance(slow_result, BrokerUnavailable)
            assert 0.25 <= slow_s < 1.0
            assert ok_result == {"minted_bot_ids": []}
            assert ok_s < 0.25  # the other broker's call is unaffected
            with pytest.raises(BrokerUnavailable):
                await BrokerClient(short_dir / "missing.sock", 0.3).crew_info()
        finally:
            for srv in (hang_srv, fast_srv):
                srv.close()

    asyncio.run(main())
