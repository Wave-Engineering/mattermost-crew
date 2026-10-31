"""#186: a partial `register` retries its failed servers in the background, and only those."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from crewchat.doorbell import build_doorbell_handlers
from crewchat.registry import Registration
from crewchat.retry import ATTEMPTS, DELAYS_S, RegisterRetry
from crewchat.server import build_handlers
from crewchat.tokens import BrokerError, BrokerUnavailable


def _two_servers(raw: dict[str, Any]) -> dict[str, Any]:
    raw["mattermost"].append({"id": "prod", "url": "http://mm2.invalid"})
    raw["crews"]["oaw"]["prod"] = {
        "broker_socket": "/run/crewbroker/oaw-prod.sock",
        "broker_user_id": "b-oaw-prod",
    }
    return raw


@pytest.fixture
def daemon(make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any]) -> Any:
    """Two servers; `prod`'s broker times out until a test clears it."""
    d = make_daemon(_two_servers(servers_toml))
    d.broker("oaw", "prod").fail["mint"] = BrokerUnavailable("timeout")
    return d


class Sleeps:
    """An injected sleep: records each delay and runs `then[i]` at the i-th, in no wall time."""

    def __init__(self, then: dict[int, Callable[[], Any]] | None = None) -> None:
        self.delays: list[float] = []
        self.then = then or {}

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        hook = self.then.get(len(self.delays) - 1)
        if hook is not None:
            got = hook()
            if asyncio.iscoroutine(got):
                await got


async def settle(retry: RegisterRetry, dev_name: str = "babel") -> None:
    async with asyncio.timeout(2):
        while retry.pending(dev_name):
            await asyncio.sleep(0)


def mints(daemon: Any, server_id: str) -> int:
    return len(daemon.broker("oaw", server_id).ops("mint"))


def test_retry_mints_only_on_the_server_that_failed(daemon: Any) -> None:
    prod = daemon.broker("oaw", "prod")
    sleeps = Sleeps({1: lambda: prod.fail.clear()})  # prod heals before the second retry
    retry = RegisterRetry(daemon.registry, sleep=sleeps, uniform=lambda a, b: 1.0)
    recovered: list[list[str]] = []

    async def scenario() -> Registration:
        reg = await daemon.registry.register("babel", "oaw", "s1")
        assert reg.backends == ["mattermost:dev", "discord"] and reg.unavailable == ["prod"]
        retry.schedule(reg, lambda r: recovered.append(list(r.backends)))
        await settle(retry)
        return reg

    reg = asyncio.run(scenario())
    assert reg.backends == ["mattermost:dev", "mattermost:prod", "discord"]
    assert reg.unavailable == [] and reg.retryable == []
    assert recovered == [["mattermost:dev", "mattermost:prod", "discord"]]
    assert sleeps.delays == [DELAYS_S[0], DELAYS_S[1]]
    # dev minted once, at register: a retry never rotates a good token.
    assert mints(daemon, "dev") == 1
    assert mints(daemon, "prod") == 3  # register, the failed first retry, the second
    assert daemon.store.token_for("babel", "prod") is not None
    assert daemon.registry.agents_on("oaw", "prod") == ["babel"]


def test_a_squatted_server_is_never_retried(daemon: Any) -> None:
    daemon.broker("oaw", "prod").fail["mint"] = BrokerError("mint", "squatted: held by x")
    retry = RegisterRetry(daemon.registry, sleep=Sleeps())

    async def scenario() -> Registration:
        reg = await daemon.registry.register("babel", "oaw", "s1")
        retry.schedule(reg, lambda r: None)
        assert not retry.pending("babel")
        return reg

    reg = asyncio.run(scenario())
    assert reg.unavailable == ["prod"] and reg.squatted == ["prod"] and reg.retryable == []
    assert mints(daemon, "prod") == 1


def test_a_newer_register_supersedes_the_retry(daemon: Any) -> None:
    async def scenario() -> tuple[Registration, Registration]:
        first = await daemon.registry.register("babel", "oaw", "s1")
        newer: list[Registration] = []

        async def reregister() -> None:
            newer.append(await daemon.registry.register("babel", "oaw", "s2"))
            daemon.broker("oaw", "prod").fail.clear()  # too late for the stale retry

        retry = RegisterRetry(daemon.registry, sleep=Sleeps({0: reregister}))
        retry.schedule(first, lambda r: None)
        await settle(retry)
        return first, newer[0]

    first, newer = asyncio.run(scenario())
    assert daemon.registry.get("babel") is newer
    # The stale retry saw it was replaced and minted nothing, and touched neither registration.
    assert mints(daemon, "prod") == 2
    assert first.unavailable == ["prod"] and newer.unavailable == ["prod"]


def test_a_retry_queued_behind_a_newer_register_mints_nothing(daemon: Any) -> None:
    """The register holds the store lock when the retry's mint starts waiting on it, so only
    the check inside the lock sees the registration was replaced (`retry_unavailable`)."""
    prod = daemon.broker("oaw", "prod")
    real_call = prod.call

    async def scenario() -> tuple[Registration, Registration]:
        first = await daemon.registry.register("babel", "oaw", "s1")
        prod.fail.clear()
        entered, release = asyncio.Event(), asyncio.Event()
        block = {"next_mint": True}

        async def call(op: str, args: dict[str, Any]) -> dict[str, Any]:
            if op == "mint" and block["next_mint"]:
                block["next_mint"] = False
                entered.set()
                await release.wait()
            return await real_call(op, args)

        prod.call = call  # type: ignore[method-assign]
        tasks: list[asyncio.Task[Registration]] = []

        async def newer_holds_the_lock() -> None:
            tasks.append(asyncio.create_task(daemon.registry.register("babel", "oaw", "s2")))
            await asyncio.wait_for(entered.wait(), 2)

        retry = RegisterRetry(daemon.registry, sleep=Sleeps({0: newer_holds_the_lock}))
        retry.schedule(first, lambda r: None)
        await asyncio.sleep(0)  # the retry's task starts and makes the register task
        # The register holds the lock in its prod mint. The retry waited on `entered` first,
        # so it wakes first, passes `_run`'s check and starts its mint, before `release`.
        await asyncio.wait_for(entered.wait(), 2)
        assert daemon.registry.get("babel") is first and retry.pending("babel")
        release.set()
        newer = await asyncio.wait_for(tasks[0], 2)
        await settle(retry)
        return first, newer

    first, newer = asyncio.run(scenario())
    assert daemon.registry.get("babel") is newer
    assert newer.backends == ["mattermost:dev", "mattermost:prod", "discord"]
    # The failed register's mint and the newer register's: the stale retry added none, so
    # it rotated nothing out, and it left the replaced registration as it was.
    assert mints(daemon, "prod") == 2
    assert prod.ops("revoke") == []
    assert first.unavailable == ["prod"]


def test_cancel_while_sleeping_ends_the_retry(daemon: Any) -> None:
    async def scenario() -> None:
        reg = await daemon.registry.register("babel", "oaw", "s1")
        gate = asyncio.Event()

        async def sleep(delay: float) -> None:
            await gate.wait()

        retry = RegisterRetry(daemon.registry, sleep=sleep)
        retry.schedule(reg, lambda r: None)
        await asyncio.sleep(0)
        assert retry.pending("babel")
        task = retry._tasks["babel"]
        retry.cancel("babel")
        await asyncio.sleep(0)
        assert task.cancelled() and not retry.pending("babel")

    asyncio.run(scenario())
    assert mints(daemon, "prod") == 1


def test_a_register_during_a_retrys_mint_waits_for_it(daemon: Any) -> None:
    """The mint in flight finishes under `shield`; the newer register queues on the store lock
    behind it, then rotates the token it installed, so exactly one stays live."""
    prod = daemon.broker("oaw", "prod")
    real_call = prod.call
    retry = RegisterRetry(daemon.registry, sleep=Sleeps())
    handlers = build_doorbell_handlers(
        daemon.registry,
        Attaches(),  # type: ignore[arg-type]
        build_handlers(daemon.config, daemon.registry)["register"],
        retry=retry,
    )

    async def scenario() -> dict[str, Any]:
        entered, release = asyncio.Event(), asyncio.Event()
        block = {"next_mint": False}

        async def call(op: str, args: dict[str, Any]) -> dict[str, Any]:
            if op == "mint" and block["next_mint"]:
                block["next_mint"] = False
                entered.set()
                await release.wait()
            return await real_call(op, args)

        prod.call = call  # type: ignore[method-assign]
        await handlers["register"]({"dev_name": "babel", "session_id": "s1"})
        prod.fail.clear()
        block["next_mint"] = True  # the retry's mint blocks, holding the store lock
        first = retry._tasks["babel"]
        await asyncio.wait_for(entered.wait(), 2)
        newer = asyncio.create_task(
            handlers["register"]({"dev_name": "babel", "session_id": "s2"})
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert first.cancelled()  # the outer task, not the shielded mint
        release.set()
        result = await asyncio.wait_for(newer, 2)
        await settle(retry)
        return result

    result = asyncio.run(scenario())
    assert result["backends"] == ["mattermost:dev", "mattermost:prod", "discord"]
    reg = daemon.registry.get("babel")
    assert reg.session_id == "s2" and reg.unavailable == []
    # register (failed), the retry's mint, then the newer register's: no fourth.
    assert mints(daemon, "prod") == 3
    assert len(prod.ops("revoke")) == 1  # the retry's token, rotated out
    assert list(prod.live.values()) == ["babel.oaw"]


def test_delays_are_capped_and_jittered() -> None:
    low = RegisterRetry(None, uniform=lambda a, b: a)  # type: ignore[arg-type]
    high = RegisterRetry(None, uniform=lambda a, b: b)  # type: ignore[arg-type]
    assert low.delay(0) == DELAYS_S[0] * 0.5 and high.delay(0) == DELAYS_S[0] * 1.5
    assert low.delay(99) == DELAYS_S[-1] * 0.5 and high.delay(99) == DELAYS_S[-1] * 1.5
    seen: list[tuple[float, float]] = []

    def uniform(a: float, b: float) -> float:
        seen.append((a, b))
        return a

    RegisterRetry(None, uniform=uniform).delay(3)  # type: ignore[arg-type]
    assert seen == [(0.5, 1.5)]


def test_the_attempts_are_bounded(daemon: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="crewchat.retry")
    sleeps = Sleeps()
    retry = RegisterRetry(daemon.registry, sleep=sleeps, uniform=lambda a, b: 1.0)

    async def scenario() -> None:
        reg = await daemon.registry.register("babel", "oaw", "s1")
        retry.schedule(reg, lambda r: None)
        await settle(retry)

    asyncio.run(scenario())
    assert len(sleeps.delays) == ATTEMPTS
    assert sleeps.delays[-1] == DELAYS_S[-1]
    assert mints(daemon, "prod") == 1 + ATTEMPTS
    assert any("still unavailable on prod" in r.getMessage() for r in caplog.records)


class Attaches:
    """The doorbell's `attach`, recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def attach(self, dev_name: str, backends: Any) -> list[str]:
        self.calls.append((dev_name, list(backends)))
        return []


def test_the_register_op_retries_and_attaches_what_comes_back(daemon: Any) -> None:
    prod = daemon.broker("oaw", "prod")
    retry = RegisterRetry(daemon.registry, sleep=Sleeps({0: lambda: prod.fail.clear()}))
    doorbell = Attaches()
    reviewed: list[list[str]] = []
    handlers = build_doorbell_handlers(
        daemon.registry,
        doorbell,  # type: ignore[arg-type]
        build_handlers(daemon.config, daemon.registry)["register"],
        extra_backends=["squawkie"],
        on_registered=lambda reg: reviewed.append(list(reg.backends)),
        retry=retry,
    )

    async def scenario() -> dict[str, Any]:
        result = await handlers["register"]({"dev_name": "babel", "session_id": "s1"})
        await settle(retry)
        return result

    result = asyncio.run(scenario())
    assert result["backends"] == ["mattermost:dev", "discord"]  # the register's own answer
    assert doorbell.calls == [
        ("babel", ["mattermost:dev", "discord", "squawkie"]),
        ("babel", ["mattermost:dev", "mattermost:prod", "discord", "squawkie"]),
    ]
    assert reviewed == [
        ["mattermost:dev", "discord"],
        ["mattermost:dev", "mattermost:prod", "discord"],
    ]


def test_the_register_op_cancels_a_pending_retry(daemon: Any) -> None:
    gate = asyncio.Event()

    async def sleep(delay: float) -> None:
        await gate.wait()

    retry = RegisterRetry(daemon.registry, sleep=sleep)
    handlers = build_doorbell_handlers(
        daemon.registry,
        Attaches(),  # type: ignore[arg-type]
        build_handlers(daemon.config, daemon.registry)["register"],
        retry=retry,
    )

    async def scenario() -> None:
        await handlers["register"]({"dev_name": "babel", "session_id": "s1"})
        first = retry._tasks["babel"]
        await handlers["register"]({"dev_name": "babel", "session_id": "s2"})
        await asyncio.sleep(0)
        assert first.cancelled() and retry.pending("babel")
        await retry.aclose()
        assert not retry.pending("babel")

    asyncio.run(scenario())
