"""Stories 1.4 and 2.1: broker clients, the token store, 401 refresh and rotation (§4.8; R-05,
R-07, R-08, R-30)."""

from __future__ import annotations

import ast
import asyncio
import itertools
import json
import stat
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import crewchat
import pytest
from crewchat.__main__ import rotation_loop
from crewchat.tokens import (
    RETRY_FIRST_S,
    RETRY_MAX_S,
    BrokerClient,
    BrokerError,
    BrokerUnavailable,
    Rotator,
    TokenStore,
    Unauthorized,
    broker_clients,
    retry_delays,
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


def test_deactivated_account_retires_its_token(daemon: Any, clock: Any) -> None:
    """#130 review: a remint the broker refuses as `deactivated` (the prune, or verify's canary
    `deactivate`) drops the token and revokes it, so neither the next 401 nor the daily
    rotation tries the dead login against the server again."""
    register(daemon)
    old = daemon.store.agent("babel").servers["dev"]
    broker = daemon.broker()
    broker.fail["remint"] = BrokerError("remint", "deactivated: babel.oaw was pruned")
    with pytest.raises(BrokerError):
        asyncio.run(daemon.store.refresh("babel", "dev", old.token))
    assert daemon.store.token_for("babel", "dev") is None
    assert broker.ops("revoke") == [{"token_id": old.token_id}]
    assert daemon.store.agent("babel").pending_revoke["dev"] == []
    # The next 401 on the dead token has nothing to remint.
    asyncio.run(daemon.store.refresh("babel", "dev", old.token))
    clock.now += timedelta(days=1)
    report = asyncio.run(Rotator(daemon.store, sleep=NoSleep()).run_daily(clock.now))
    assert report.rotated == [] and report.failed == []
    assert len(broker.ops("remint")) == 1
    # A register mints again, as for any pruned account.
    del broker.fail["remint"]
    register(daemon)
    assert daemon.store.token_for("babel", "dev") is not None


def test_other_remint_errors_keep_the_token(daemon: Any) -> None:
    register(daemon)
    old = daemon.store.agent("babel").servers["dev"]
    daemon.broker().fail["remint"] = BrokerError("remint", "upstream: GET /bots failed")
    with pytest.raises(BrokerError):
        asyncio.run(daemon.store.refresh("babel", "dev", old.token))
    assert daemon.store.agent("babel").servers["dev"] == old
    assert daemon.broker().ops("revoke") == []


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
    # Each remint names the token held, which the broker keeps live (#41).
    assert [a["current_token_id"] for a in broker.ops("remint")] == [
        before["babel"].token_id,
        before["kessel"].token_id,
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
            (args,) = daemon.broker(crew, sid).ops("mint")
            assert (args["dev_name"], args["crew"]) == (dev, crew)
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


def test_rotation_does_not_drop_a_concurrent_register(daemon: Any, clock: Any) -> None:
    """#40 review minor 1 (R-05, R-30): a `register` that lands between the rotation's idle
    check and its drop keeps the agent, its fresh token and its file."""
    start = clock.now
    register(daemon)
    broker = daemon.broker()
    now = start + timedelta(days=31)
    store = daemon.store

    async def scenario() -> Any:
        lock = store.lock("babel")
        await lock.acquire()  # the register is inside its critical section
        rotation = asyncio.create_task(Rotator(store, sleep=NoSleep()).run_daily(now))
        for _ in range(10):
            await asyncio.sleep(0)  # the rotation has seen a stale last_register, and waits
        assert not rotation.done()
        # The register's body, under the lock it already holds (Registry.register).
        held = store.held_token_id("babel", "oaw", "dev")
        minted = await broker.mint("babel", "oaw", held)
        await store.install_minted("babel", "oaw", "dev", minted, now)
        lock.release()
        return await rotation, minted

    report, minted = asyncio.run(scenario())
    assert report.dropped == []
    assert ("close", "babel") not in daemon.events
    agent = store.agent("babel")
    assert agent is not None and agent.last_register == now
    assert store.token_for("babel", "dev") == minted["token"]
    assert minted["token_id"] in broker.live  # its fresh token was not revoked
    assert (store.root / "babel.json").exists()


def test_idle_agent_dropped_then_closed(daemon: Any, clock: Any) -> None:
    """`close_agent` follows a drop that `drop_if_idle` confirmed, and only then."""
    register(daemon)
    daemon.events.clear()
    now = clock.now + timedelta(days=31)
    report = asyncio.run(Rotator(daemon.store, sleep=NoSleep()).run_daily(now))
    assert report.dropped == ["babel"]
    assert steps(daemon.events) == [("broker", "revoke"), ("close", "babel")]
    assert asyncio.run(daemon.store.drop_if_idle("babel", now)) is False  # already gone


def test_retry_delays_double_to_an_hour() -> None:
    assert RETRY_FIRST_S == 300 and RETRY_MAX_S == 3600
    assert list(itertools.islice(retry_delays(), 6)) == [300, 600, 1200, 2400, 3600, 3600]


class FakeTime:
    """`rotation_loop`'s clock and wait: each wait advances monotonic time, without sleeping."""

    def __init__(self, stop_after: int, on_wait: Callable[[int], None] = lambda _n: None):
        self.t = 0.0
        self.waits: list[float] = []
        self.stop_after = stop_after
        self.on_wait = on_wait

    def monotonic(self) -> float:
        return self.t

    async def wait(self, stop: asyncio.Event, seconds: float) -> bool:
        self.waits.append(seconds)
        self.t += seconds
        self.on_wait(len(self.waits))
        if len(self.waits) >= self.stop_after:
            stop.set()
        return stop.is_set()


def test_failed_rotation_retried_on_backoff(daemon: Any, clock: Any) -> None:
    """#40 review minor 3 (R-05, R-07): a rotation that failed on a broker timeout is retried
    at 5 min, then 10 min, and succeeds once the broker answers, long before the next
    daily run and the 26 h native expiry."""
    register(daemon)
    old = daemon.store.agent("babel").servers["dev"]
    broker = daemon.broker()
    broker.fail["remint"] = BrokerUnavailable("babel: no answer to remint in 2.0s")
    clock.now += timedelta(days=1)

    def broker_back(n: int) -> None:
        if n == 2:
            del broker.fail["remint"]

    fake = FakeTime(stop_after=3, on_wait=broker_back)

    async def scenario() -> None:
        stop = asyncio.Event()
        rotator = Rotator(daemon.store, sleep=NoSleep())
        await rotation_loop(
            rotator,
            stop,
            interval_s=24 * 3600,
            wait=fake.wait,
            monotonic=fake.monotonic,
            now=clock,
        )

    asyncio.run(scenario())
    assert fake.waits == [300, 600, 24 * 3600 - 900]
    assert len(broker.ops("remint")) == 3  # the daily run, a failed retry, a good one
    rotated = daemon.store.agent("babel").servers["dev"]
    assert rotated.token_id != old.token_id
    assert old.token_id not in broker.live  # the old token was revoked after the swap


def test_failing_rotation_retries_stop_at_the_next_daily_run(daemon: Any, clock: Any) -> None:
    register(daemon)
    daemon.broker().fail["remint"] = BrokerUnavailable("down")
    clock.now += timedelta(days=1)
    fake = FakeTime(stop_after=1000)

    async def scenario() -> None:
        stop = asyncio.Event()
        rotator = Rotator(daemon.store, sleep=NoSleep())
        original = rotator.run_daily
        runs = 0

        async def run_daily(now: Any) -> Any:
            nonlocal runs
            runs += 1
            if runs == 2:
                stop.set()
            return await original(now)

        rotator.run_daily = run_daily  # type: ignore[method-assign]
        await rotation_loop(
            rotator, stop, interval_s=24 * 3600, wait=fake.wait, monotonic=fake.monotonic
        )

    asyncio.run(scenario())
    first_day = []
    total = 0.0
    for w in fake.waits:
        first_day.append(w)
        total += w
        if total >= 24 * 3600:
            break
    assert total == 24 * 3600  # the retries and the final wait fill exactly one day
    assert first_day[:5] == [300, 600, 1200, 2400, 3600]
    assert all(w <= 3600 for w in first_day[:-1])
