"""Story 3.3: the daemon's filters — switch cache, membership, fail-closed, the switch, the
kiosk verdicts (R-20 to R-23, R-27, R-31, R-34; D-100, D-103).

The rig: three local brokers on server `dev` (our crew `oaw`; `ally`, which `oaw` trusts; and
`rival`, which it doesn't), a fourth on server `dev2`, and `oaw` also trusting `ghost`, which has
no `servers.toml` table here. The agent is `babel` of `oaw` (Dev-Team `wave`).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import stat
import time
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from crewchat.adapters.base import InboundMessage
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.doorbell import CursorStore, Doorbell, DoorbellEvent, to_event
from crewchat.filters import (
    DELIVER,
    DELIVER_MARKED_OUTSIDER,
    KIOSK,
    Filters,
    Kind,
    KioskQueue,
    SwitchCache,
    Verdict,
)
from crewchat.registry import Registration
from crewchat.threads import MyThreads
from crewchat.tokens import BrokerClient, BrokerKey, BrokerUnavailable

SID, SID2 = "dev", "dev2"
MM, MM2 = f"mattermost:{SID}", f"mattermost:{SID2}"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

KESSEL = "bot-kessel"  # an oaw crewmate's bot account
HUMAN = "h-oaw"  # oaw's human partner
ALLY_BOT, ALLY_HUMAN = "bot-ally", "h-ally"
RIVAL_BOT = "bot-rival"  # minted by a local broker whose crew oaw doesn't trust
OUTSIDER = "h-out"  # a human no crew lists

INFO: dict[BrokerKey, dict[str, Any]] = {
    ("oaw", SID): {
        "broker_user_id": "b-oaw",
        "human_user_ids": [HUMAN],
        "trusted": ["ally", "ghost"],
        "minted_bot_ids": [KESSEL],
    },
    ("ally", SID): {
        "broker_user_id": "b-ally",
        "human_user_ids": [ALLY_HUMAN],
        "trusted": [],
        "minted_bot_ids": [ALLY_BOT],
    },
    ("rival", SID): {
        "broker_user_id": "b-rival",
        "human_user_ids": ["h-rival"],
        "trusted": [],
        "minted_bot_ids": [RIVAL_BOT],
    },
    ("oaw", SID2): {
        "broker_user_id": "b-oaw2",
        "human_user_ids": [HUMAN],
        "trusted": [],
        "minted_bot_ids": [KESSEL],
    },
}


def switch(mode: str = "crew", *, expires: datetime | None = None, **kw: Any) -> dict[str, Any]:
    return {
        "mode": mode,
        "expires_at": expires.isoformat() if expires else None,
        "revert_to": kw.get("revert_to", "crew" if mode == "social" else mode),
        "kiosk_on": kw.get("kiosk_on", True),
    }


class Broker(BrokerClient):
    """A broker in process. `hang` makes every call time out after `timeout_s`; `fail` makes
    the named ops fail at once."""

    def __init__(self, key: BrokerKey, timeout_s: float = 0.1) -> None:
        super().__init__(f"/nonexistent/{key[0]}-{key[1]}.sock", timeout_s, name=f"{key[0]}")
        self.info = copy.deepcopy(INFO[key])
        self.switch = switch()
        self.calls: list[str] = []
        self.hang = False
        self.fail: set[str] = set()

    async def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(op)
        if self.hang:
            await asyncio.sleep(self.timeout_s)
            raise BrokerUnavailable(f"{self.name}: no answer to {op}")
        if op in self.fail:
            raise BrokerUnavailable(f"{self.name}: {op} failed")
        if op == "crew_info":
            return copy.deepcopy(self.info)
        if op == "switch_state":
            return dict(self.switch)
        raise AssertionError(op)

    def count(self, op: str = "crew_info") -> int:
        return self.calls.count(op)


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def registration(dev: str = "babel", crew: str = "oaw") -> Registration:
    return Registration(
        dev_name=dev,
        crew=crew,
        session_id="s",
        account=f"{dev}.{crew}",
        backends=[MM, MM2, "discord"],
        needs_list="bj:needs",
        registered_at=NOW,
        dev_team="wave",
    )


class StubRegistry:
    def __init__(self, *regs: Registration) -> None:
        self.regs = {r.dev_name: r for r in regs}

    def get(self, dev_name: str) -> Registration | None:
        return self.regs.get(dev_name)


class Rig:
    def __init__(self, **filter_kw: Any) -> None:
        self.mono = Mono()
        self.now = NOW
        self.brokers: dict[BrokerKey, Broker] = {k: Broker(k) for k in INFO}
        self.cache = SwitchCache(self.brokers, round_wait_s=0.5, monotonic=self.mono)
        self.threads = MyThreads()
        self.reg = registration()
        self.registry = StubRegistry(self.reg, registration("kessel"))
        # Both servers have the kiosk on, with a cap no test reaches unless it sets one (#117).
        filter_kw.setdefault("kiosk_caps", {SID: 1000, SID2: 1000})
        self.filters = Filters(
            self.registry,  # type: ignore[arg-type]
            self.cache,
            self.threads,
            clock=lambda: self.now,
            monotonic=self.mono,
            **filter_kw,
        )

    def broker(self, crew: str = "oaw", sid: str = SID) -> Broker:
        return self.brokers[(crew, sid)]

    def set_mode(self, mode: str, **kw: Any) -> None:
        for (_crew, _sid), b in self.brokers.items():
            b.switch = switch(mode, **kw)

    async def prime(self) -> None:
        await self.cache.refresh_all()

    async def admit(
        self, msg: InboundMessage, path: str = "push", reg: Registration | None = None
    ) -> Verdict:
        return await self.filters.admit(msg, reg or self.reg, self.now, path)  # type: ignore[arg-type]


_posts = iter(range(1, 1_000_000))


def mm(
    author: str,
    text: str = "@babel a question",
    *,
    is_bot: bool | None = None,
    backend: str = MM,
    root: str = "",
    kiosk: bool = False,
    dm: bool = False,
    name: str = "",
    resolved: bool = True,
) -> InboundMessage:
    return InboundMessage(
        backend=backend,
        channel="chan",
        post_id=f"p{next(_posts)}",
        root_id=root,
        author_id=author,
        author_name=name or author,
        text=text,
        is_bot=author.startswith("bot") if is_bot is None else is_bot,
        kiosk=kiosk,
        dm=dm,
        author_resolved=resolved,
    )


def run[R](coro: Awaitable[R]) -> R:
    async def main() -> R:
        return await coro

    return asyncio.run(main())


@pytest.fixture
def rig() -> Rig:
    return Rig()


# -- fail closed ----------------------------------------------------------------------------
def test_unresolvable_crew_dropped(rig: Rig) -> None:
    async def scenario() -> list[Verdict]:
        await rig.prime()
        return [
            await rig.admit(mm(OUTSIDER, resolved=False)),
            await rig.admit(mm(KESSEL, resolved=False)),
            await rig.admit(mm("", resolved=False)),
            await rig.admit(mm(OUTSIDER, resolved=False), "pull"),
        ]

    assert [v.reason for v in run(scenario())] == ["unresolvable"] * 4


def test_no_cache_drops(rig: Rig) -> None:
    async def scenario() -> list[Verdict]:
        # Never fetched: no switch state and no crew_info.
        first = await rig.admit(mm(KESSEL))
        pulled = await rig.admit(mm(KESSEL), "pull")
        # crew_info cached but no switch state.
        rig.broker().fail = {"switch_state"}
        await rig.prime()
        no_switch = await rig.admit(mm(KESSEL))
        # A switch state but no crew_info.
        other = Rig()
        other.broker().fail = {"crew_info"}
        await other.prime()
        no_info = await other.admit(mm(KESSEL))
        return [first, pulled, no_switch, no_info]

    verdicts = run(scenario())
    assert all(v.kind == Kind.DROP and v.reason == "no_cache" for v in verdicts), verdicts


def test_broker_down_discord_and_squawkie_flow(rig: Rig) -> None:
    for b in rig.brokers.values():
        b.hang = True

    async def scenario() -> list[Verdict]:
        await rig.prime()  # every broker times out: nothing cached
        squawk = DoorbellEvent("squawkie", {"type": "squawk.updated"}, ("s1",))
        return [
            await rig.admit(mm("u1", "@babel ping", is_bot=False, backend="discord")),
            await rig.filters.admit_event("babel", squawk),
            await rig.admit(mm("u1", "anything", is_bot=False, backend="squawkie")),
            await rig.admit(mm(KESSEL)),
            await rig.admit(mm(HUMAN, is_bot=False)),
        ]

    discord, squawkie, squawk_msg, mm_bot, mm_human = run(scenario())
    assert discord == DELIVER and squawkie == DELIVER and squawk_msg == DELIVER
    assert mm_bot.kind == Kind.DROP and mm_human.kind == Kind.DROP


# -- membership -----------------------------------------------------------------------------
def test_reassigned_bot_not_crew(rig: Rig) -> None:
    """Minted by our broker, then reassigned: the broker no longer lists it (D-100)."""
    rig.set_mode("social", expires=NOW + timedelta(hours=1))

    async def scenario() -> tuple[Verdict, int]:
        await rig.prime()
        before = rig.broker().count()
        verdict = await rig.admit(mm("bot-moved"))
        return verdict, rig.broker().count() - before

    verdict, refetched = run(scenario())
    assert refetched == 1  # the cache-miss re-fetch ran, and it is still absent
    assert verdict == DELIVER_MARKED_OUTSIDER  # an outsider, not crew


def test_membership_by_minted_ids_not_suffix(rig: Rig) -> None:
    rig.set_mode("social", expires=NOW + timedelta(hours=1))

    async def scenario() -> list[Verdict]:
        await rig.prime()
        return [
            await rig.admit(mm("bot-spoof", name="kessel.oaw")),
            await rig.admit(mm("h-spoof", name="bj.oaw", is_bot=False)),
            await rig.admit(mm(KESSEL, name="anything-at-all")),
        ]

    assert run(scenario()) == [DELIVER_MARKED_OUTSIDER, DELIVER_MARKED_OUTSIDER, DELIVER]


def test_trusted_membership_by_minted_ids(rig: Rig) -> None:
    """A trusted crew's bots by its own `minted_bot_ids`, its humans by its listed IDs; an
    `owner_id` anywhere in a reply is never read (D-100)."""
    rig.set_mode("trusted")
    # A decoy: an owner map claiming the spoof bot is ally's broker's. It must not count.
    rig.broker("ally").info["owner_id"] = {"bot-spoof": "b-ally"}

    async def scenario() -> list[Verdict]:
        await rig.prime()
        return [
            await rig.admit(mm(ALLY_BOT)),
            await rig.admit(mm(ALLY_HUMAN, is_bot=False)),
            await rig.admit(mm("bot-spoof", name="x.ally")),
            await rig.admit(mm(ALLY_BOT, is_bot=False)),  # a bot ID listed as a human? no
        ]

    ally_bot, ally_human, spoof, wrong_list = run(scenario())
    assert ally_bot == DELIVER and ally_human == DELIVER
    assert spoof == KIOSK  # an outsider, directly addressed
    assert wrong_list == KIOSK  # humans only by `human_user_ids`


def test_trusted_crew_without_table_is_outsider(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.set_mode("trusted")

    async def scenario() -> list[Verdict]:
        await rig.prime()
        with caplog.at_level(logging.WARNING, logger="crewchat.filters"):
            return [
                await rig.admit(mm("h-ghost", is_bot=False)),
                await rig.admit(mm("h-ghost", "@babel again", is_bot=False)),
                await rig.admit(mm(ALLY_BOT)),  # the refresh went on past ghost
            ]

    ghost, again, ally = run(scenario())
    assert ghost == KIOSK and again == KIOSK  # outsiders
    assert ally == DELIVER
    assert sum("ghost" in r.getMessage() for r in caplog.records) == 1  # logged once


def test_trusted_membership_only_from_its_broker() -> None:
    async def scenario() -> None:
        rig = Rig()
        rig.set_mode("trusted")
        await rig.prime()
        # ally's broker stops answering: its last good crew_info is kept.
        rig.broker("ally").hang = True
        await rig.prime()
        assert await rig.admit(mm(ALLY_BOT)) == DELIVER

        # A trusted crew never fetched since start: no member list.
        fresh = Rig()
        fresh.set_mode("trusted")
        fresh.broker("ally").hang = True
        await fresh.prime()
        started = time.monotonic()
        assert await fresh.admit(mm(KESSEL)) == DELIVER  # other crews still resolve...
        assert time.monotonic() - started < 0.05  # ...without waiting on ally's broker
        unrecognised = await fresh.admit(mm(ALLY_BOT))
        assert unrecognised.kind == Kind.DROP  # never KIOSK
        assert await fresh.admit(mm(ALLY_HUMAN, is_bot=False)) == KIOSK  # an outsider
        fresh.mono.t += 31
        another = await fresh.admit(mm("bot-unknown"))
        assert another.kind == Kind.DROP

    run(scenario())


def test_stale_crew_info_expires_to_uncached() -> None:
    async def scenario() -> None:
        rig = Rig()
        rig.set_mode("trusted")
        await rig.prime()
        # ally's broker answers with errors from now on.
        rig.broker("ally").fail = {"crew_info"}
        rig.mono.t += 200
        await rig.prime()
        assert await rig.admit(mm(ALLY_BOT)) == DELIVER  # last good, 200 s old
        rig.mono.t += 101  # past crew_info_max_age_s (300)
        await rig.prime()
        assert rig.cache.crew_info("ally", SID) is None
        bot = await rig.admit(mm(ALLY_BOT))
        assert bot.kind == Kind.DROP  # unrecognised: never KIOSK
        assert await rig.admit(mm(ALLY_HUMAN, is_bot=False)) == KIOSK  # humans: outsiders
        assert await rig.admit(mm(KESSEL)) == DELIVER  # ours still fresh

        # Our own broker erroring past the max age: Mattermost drops.
        rig.broker("oaw").fail = {"crew_info"}
        rig.mono.t += 301
        await rig.prime()
        own = await rig.admit(mm(KESSEL))
        assert own.kind == Kind.DROP and own.reason == "no_cache"

    run(scenario())


def test_untrusted_uncached_broker_leaves_kiosk() -> None:
    """D-115: rival, which oaw doesn't trust, never fetched. Its bots are outsiders whether it
    is cached or not, so an unknown bot addressing the agent still reaches the kiosk: both when
    the miss's re-fetch finds rival down, and when the re-fetch is capped and the uncached rule
    decides."""

    async def scenario() -> list[Verdict]:
        rig = Rig()
        rig.broker("rival").hang = True
        await rig.prime()
        assert rig.cache.crew_info("rival", SID) is None
        refetched = await rig.admit(mm("bot-unknown"))
        rig.mono.t += 5  # within refetch_per_bot_s: no second re-fetch
        capped = await rig.admit(mm("bot-unknown"))
        return [refetched, capped]

    assert run(scenario()) == [KIOSK, KIOSK]


def test_trusted_uncached_broker_still_drops() -> None:
    """D-115: ally, which oaw trusts, never fetched. An unknown bot may be one of ally's, which
    `trusted` would deliver, so it is dropped rather than sent to the kiosk: by the miss's
    re-fetch finding ally down, then, with the re-fetch capped, by the uncached rule."""

    async def scenario() -> list[Verdict]:
        rig = Rig()
        rig.broker("ally").hang = True
        await rig.prime()
        assert rig.cache.crew_info("ally", SID) is None
        refetched = await rig.admit(mm("bot-unknown"))
        rig.mono.t += 5  # within refetch_per_bot_s: no second re-fetch
        capped = await rig.admit(mm("bot-unknown"))
        return [refetched, capped]

    refetched, capped = run(scenario())
    assert refetched.kind == Kind.DROP and refetched.reason == "refetch_failed"
    assert capped.kind == Kind.DROP and capped.reason == "uncached_crew"


# -- cache misses ---------------------------------------------------------------------------
def test_new_crewmate_cache_miss_never_kiosk(rig: Rig) -> None:
    async def scenario() -> tuple[Verdict, dict[BrokerKey, int]]:
        await rig.prime()
        before = {k: b.count() for k, b in rig.brokers.items()}
        rig.broker().info["minted_bot_ids"].append("bot-new")  # minted, not yet cached
        verdict = await rig.admit(mm("bot-new"))
        return verdict, {k: b.count() - before[k] for k, b in rig.brokers.items()}

    verdict, refetched = run(scenario())
    assert verdict == DELIVER
    assert set(refetched.values()) == {1}  # one re-fetch of every local broker


def test_new_trusted_agent_cache_miss_never_kiosk(rig: Rig) -> None:
    rig.set_mode("trusted")

    async def scenario() -> tuple[Verdict, int]:
        await rig.prime()
        before = rig.broker("ally").count()
        rig.broker("ally").info["minted_bot_ids"].append("bot-ally2")
        verdict = await rig.admit(mm("bot-ally2", "@babel hello from ally"))
        return verdict, rig.broker("ally").count() - before

    verdict, refetched = run(scenario())
    assert verdict == DELIVER
    assert refetched == 1  # including that crew's own broker


def test_refetch_capped_per_bot() -> None:
    async def scenario() -> None:
        rig = Rig()
        await rig.prime()
        # One unknown bot posting again and again: one round per 30 s.
        for _ in range(5):
            rig.mono.t += 3  # past the global floor each time
            assert (await rig.admit(mm("bot-x"))).kind == Kind.KIOSK
        assert rig.cache.rounds == 1
        rig.mono.t += 30
        await rig.admit(mm("bot-x"))
        assert rig.cache.rounds == 2

        # Many distinct unknown bots at once: still one round per refetch_min_interval_s.
        rig.mono.t += 5
        verdicts = await asyncio.gather(*(rig.admit(mm(f"bot-m{n}")) for n in range(20)))
        assert rig.cache.rounds == 3
        assert all(v.kind != Kind.DELIVER for v in verdicts)
        rig.mono.t += 1  # inside the 2 s floor: no new round
        await rig.admit(mm("bot-late"))
        assert rig.cache.rounds == 3
        rig.mono.t += 2
        await rig.admit(mm("bot-later"))
        assert rig.cache.rounds == 4

        # A round that doesn't finish in time: every miss waiting on it is DROP.
        slow = Rig()
        await slow.prime()
        for b in slow.brokers.values():
            b.hang, b.timeout_s = True, 5.0
        slow.cache.round_wait_s = 0.1
        waited = await asyncio.gather(*(slow.admit(mm(f"bot-s{n}")) for n in range(5)))
        assert [v.reason for v in waited] == ["refetch_failed"] * 5
        assert slow.cache.rounds == 1
        assert slow.cache._round is not None
        slow.cache._round.cancel()

    run(scenario())


# -- the switch -----------------------------------------------------------------------------
SENDERS: dict[str, InboundMessage] = {
    "crewmate_bot": mm(KESSEL),
    "crewmate_human": mm(HUMAN, is_bot=False),
    "trusted_bot": mm(ALLY_BOT),
    "trusted_human": mm(ALLY_HUMAN, is_bot=False),
    "outsider_human": mm(OUTSIDER, is_bot=False),
    "outsider_bot": mm(RIVAL_BOT),
    "kiosk_post": mm(KESSEL, kiosk=True),
    "trusted_broadcast": mm(ALLY_HUMAN, "@agents all hands", is_bot=False),
}
D, M, K = DELIVER, DELIVER_MARKED_OUTSIDER, KIOSK
EXPECTED: dict[str, dict[str, Verdict | str]] = {
    "crew": {
        "crewmate_bot": D,
        "crewmate_human": D,
        "trusted_bot": K,  # a trusted crew under crew only is an outsider
        "trusted_human": K,
        "outsider_human": K,
        "outsider_bot": K,
        "kiosk_post": "drop",
        "trusted_broadcast": "drop",  # an outsider's broadcast never reaches a kiosk
    },
    "trusted": {
        "crewmate_bot": D,
        "crewmate_human": D,
        "trusted_bot": D,
        "trusted_human": D,
        "outsider_human": K,
        "outsider_bot": K,
        "kiosk_post": "drop",
        "trusted_broadcast": D,
    },
    "social": {
        "crewmate_bot": D,
        "crewmate_human": D,
        "trusted_bot": D,
        "trusted_human": D,
        "outsider_human": M,
        "outsider_bot": M,
        "kiosk_post": M,
        "trusted_broadcast": D,
    },
}


@pytest.mark.parametrize("mode", ["crew", "trusted", "social"])
def test_modes_matrix(mode: str) -> None:
    rig = Rig()
    rig.set_mode(mode, expires=NOW + timedelta(hours=2) if mode == "social" else None)

    async def scenario() -> dict[str, Verdict]:
        await rig.prime()
        return {name: await rig.admit(msg) for name, msg in SENDERS.items()}

    got = run(scenario())
    for name, want in EXPECTED[mode].items():
        if want == "drop":
            assert got[name].kind == Kind.DROP, (mode, name, got[name])
        else:
            assert got[name] == want, (mode, name, got[name])


def test_expiry_checked_per_delivery(rig: Rig) -> None:
    rig.set_mode("social", expires=NOW + timedelta(hours=1), revert_to="trusted")

    async def scenario() -> list[Verdict]:
        await rig.prime()
        for b in rig.brokers.values():
            b.hang = True  # the broker is stopped: only the cached state is left
        before = [await rig.admit(mm(OUTSIDER, is_bot=False))]
        rig.now = NOW + timedelta(hours=1, seconds=1)  # past the cached expiry
        return [
            *before,
            await rig.admit(mm(OUTSIDER, is_bot=False)),
            await rig.admit(mm(ALLY_HUMAN, is_bot=False)),
        ]

    assert run(scenario()) == [DELIVER_MARKED_OUTSIDER, KIOSK, DELIVER]  # reverted to trusted


def test_unaddressed_outsider_never_kiosk(rig: Rig) -> None:
    async def scenario() -> list[Verdict]:
        await rig.prime()
        return [
            await rig.admit(mm(OUTSIDER, "morning, everyone", is_bot=False)),
            await rig.admit(mm(RIVAL_BOT, "status update")),
        ]

    assert [v.reason for v in run(scenario())] == ["unaddressed", "unaddressed"]


def test_outsider_broadcast_never_kiosk(rig: Rig) -> None:
    async def scenario() -> list[Verdict]:
        out: list[Verdict] = []
        for mode in ("crew", "trusted"):
            rig.set_mode(mode)
            await rig.prime()
            for text in ("@agents anyone?", "@oaw anyone?", "@wave anyone?"):
                out.append(await rig.admit(mm(OUTSIDER, text, is_bot=False)))
        return out

    verdicts = run(scenario())
    assert all(v.kind == Kind.DROP for v in verdicts), verdicts


def test_kiosk_off_drops_outsiders(rig: Rig) -> None:
    """IT-23's unit side (R-34)."""
    rig.set_mode("crew", kiosk_on=False)

    async def scenario() -> Verdict:
        await rig.prime()
        return await rig.admit(mm(OUTSIDER, is_bot=False))

    assert run(scenario()).reason == "kiosk_off"


def test_kiosk_server_off_drops() -> None:
    """#117: a server without `kiosk = true` gives no kiosk, whatever the switch says."""

    async def scenario() -> None:
        rig = Rig(kiosk_caps={SID2: 10})  # the kiosk on dev2 only
        rig.set_mode("crew", kiosk_on=True)
        await rig.prime()
        off = await rig.admit(mm(OUTSIDER, is_bot=False))
        assert off.reason == "kiosk_server_off"
        assert await rig.admit(mm(OUTSIDER, is_bot=False, backend=MM2)) == KIOSK
        # The default: no server has the kiosk.
        bare = Filters(rig.registry, rig.cache, rig.threads)  # type: ignore[arg-type]
        assert (await bare.admit(mm("h-out3", is_bot=False), rig.reg, NOW, "push")).reason == (
            "kiosk_server_off"
        )

    run(scenario())


def test_kiosk_daily_cap(tmp_path: Path) -> None:
    """#117: past the cap a server's kiosk drops for the rest of the UTC day; another server
    is unaffected; a new UTC date starts over."""

    async def scenario() -> None:
        rig = Rig(kiosk_caps={SID: 2, SID2: 5}, state_dir=tmp_path)
        await rig.prime()
        got = [await rig.admit(mm(f"h-o{i}", is_bot=False)) for i in range(3)]
        assert got[:2] == [KIOSK, KIOSK] and got[2].reason == "kiosk_daily_cap"
        assert await rig.admit(mm("h-o9", is_bot=False, backend=MM2)) == KIOSK
        rig.now = datetime(2026, 10, 25, 0, 0, 1, tzinfo=UTC)  # past midnight UTC
        assert await rig.admit(mm("h-o10", is_bot=False)) == KIOSK

    run(scenario())
    saved = json.loads((tmp_path / "kiosk-daily.json").read_text())
    assert saved == {
        SID: {"date": "2026-10-25", "count": 1},
        SID2: {"date": "2026-10-24", "count": 1},
    }
    assert stat.S_IMODE((tmp_path / "kiosk-daily.json").stat().st_mode) == 0o600


def test_kiosk_daily_cap_zero_means_none() -> None:
    async def scenario() -> Verdict:
        rig = Rig(kiosk_caps={SID: 0})
        await rig.prime()
        return await rig.admit(mm(OUTSIDER, is_bot=False))

    assert run(scenario()).reason == "kiosk_daily_cap"


def test_kiosk_daily_cap_survives_restart(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The count is reloaded from `kiosk-daily.json`: a restart never resets the day."""
    (tmp_path / "kiosk-daily.json").write_text(
        json.dumps({SID: {"date": "2026-10-24", "count": 3}, SID2: {"date": "2026-10-23",
                                                                      "count": 3}})
    )
    caplog.set_level(logging.WARNING, logger="crewchat.filters")

    async def scenario() -> list[Verdict]:
        rig = Rig(kiosk_caps={SID: 3, SID2: 3}, state_dir=tmp_path)
        await rig.prime()
        return [
            await rig.admit(mm("h-a", is_bot=False)),
            await rig.admit(mm("h-b", is_bot=False)),
            await rig.admit(mm("h-c", is_bot=False, backend=MM2)),  # yesterday's count
        ]

    first, second, other = run(scenario())
    assert first.reason == "kiosk_daily_cap" and second.reason == "kiosk_daily_cap"
    assert other == KIOSK
    capped = [r for r in caplog.records if "daily cap" in r.getMessage()]
    assert len(capped) == 1  # once per server per day

    # A file it can't read is logged and read as empty: the kiosk still answers.
    (tmp_path / "kiosk-daily.json").write_text("not json")

    async def again() -> Verdict:
        rig = Rig(kiosk_caps={SID: 3}, state_dir=tmp_path)
        await rig.prime()
        return await rig.admit(mm("h-d", is_bot=False))

    assert run(again()) == KIOSK


def test_kiosk_rate_limit_per_outsider() -> None:
    async def scenario() -> None:
        rig = Rig()
        await rig.prime()
        verdicts = [await rig.admit(mm(OUTSIDER, is_bot=False)) for _ in range(11)]
        assert verdicts[:10] == [KIOSK] * 10
        assert verdicts[10].reason == "kiosk_rate"
        # Another outsider, another expert, another server: unaffected.
        assert await rig.admit(mm("h-out2", is_bot=False)) == KIOSK
        kessel = rig.registry.get("kessel")
        assert kessel is not None
        assert await rig.admit(mm(OUTSIDER, "@kessel hi", is_bot=False), reg=kessel) == KIOSK
        assert await rig.admit(mm(OUTSIDER, is_bot=False, backend=MM2)) == KIOSK
        # An hour later the outsider may ask again.
        rig.mono.t += 3600
        await rig.prime()  # the 30 s refresh kept running meanwhile
        assert await rig.admit(mm(OUTSIDER, is_bot=False)) == KIOSK

        # Configurable.
        tight = Rig(kiosk_rate_per_hour=2)
        await tight.prime()
        got = [await tight.admit(mm(OUTSIDER, is_bot=False)) for _ in range(3)]
        assert got[:2] == [KIOSK, KIOSK] and got[2].reason == "kiosk_rate"

    run(scenario())


# -- kiosk posts ----------------------------------------------------------------------------
def test_kiosk_post_classed_outsider(rig: Rig) -> None:
    rig.set_mode("social", expires=NOW + timedelta(hours=1))

    async def scenario() -> list[Verdict]:
        await rig.prime()
        return [
            await rig.admit(mm(KESSEL, kiosk=True)),
            await rig.admit(mm(HUMAN, is_bot=False, kiosk=True)),
            await rig.admit(mm(KESSEL, kiosk=True), "pull"),
        ]

    assert run(scenario()) == [DELIVER_MARKED_OUTSIDER] * 3


def test_kiosk_post_never_kiosk_verdict() -> None:
    async def scenario() -> dict[str, list[Verdict]]:
        out: dict[str, list[Verdict]] = {}
        for mode in ("crew", "trusted", "social"):
            rig = Rig()
            rig.set_mode(mode, expires=NOW + timedelta(hours=1) if mode == "social" else None)
            await rig.prime()
            out[mode] = [
                await rig.admit(mm(KESSEL, kiosk=True)),
                await rig.admit(mm(OUTSIDER, is_bot=False, kiosk=True)),
                await rig.admit(mm(RIVAL_BOT, kiosk=True)),
                await rig.admit(mm(KESSEL, kiosk=True), "pull"),
            ]
        return out

    got = run(scenario())
    for mode in ("crew", "trusted"):
        assert all(v.kind == Kind.DROP for v in got[mode]), (mode, got[mode])
    assert got["social"] == [DELIVER_MARKED_OUTSIDER] * 4


def test_two_kiosks_no_second_answer(rig: Rig) -> None:
    """kessel's kiosk answers an outsider in a thread babel's kiosk also answered: babel's
    doorbell never routes that answer to babel's kiosk."""
    rig.threads.add("babel", "root-1", MM)  # babel's kiosk answered in this thread
    kiosk = KioskQueue()
    bell = Doorbell({}, CursorStore(None), admit=rig.filters.admit_event, kiosk=kiosk)

    async def scenario() -> tuple[int, int, int]:
        await rig.prime()
        question = mm(OUTSIDER, "@babel @kessel what is this?", is_bot=False, root="root-1")
        await bell.offer("babel", MM, to_event(MM, question))
        first = kiosk.queue("babel").qsize()
        answer = mm(KESSEL, "@babel it is a widget", root="root-1", kiosk=True)
        await bell.offer("babel", MM, to_event(MM, answer))
        return first, kiosk.queue("babel").qsize(), bell.queue("babel").qsize()

    first, after, delivered = run(scenario())
    assert first == 1  # the outsider's question went to babel's kiosk
    assert after == 1  # kessel's kiosk answer did not
    assert delivered == 0  # nor to babel itself


# -- push and pull --------------------------------------------------------------------------
class ReadBackend:
    def __init__(self, msgs: list[InboundMessage]) -> None:
        self.msgs = msgs

    async def read(self, *args: Any, **kw: Any) -> list[InboundMessage]:
        return list(self.msgs)

    async def thread(self, *args: Any, **kw: Any) -> list[InboundMessage]:
        return list(self.msgs)


@pytest.mark.parametrize("op", ["read", "thread"])
def test_read_path_filtered(op: str) -> None:
    unaddressed = mm(KESSEL, "pushed the fix to main")
    outsider = mm(OUTSIDER, "@babel can you help?", is_bot=False)
    kiosk_post = mm(KESSEL, "a kiosk answer", kiosk=True)
    msgs = [unaddressed, outsider, kiosk_post]

    async def scenario(mode: str) -> tuple[list[dict[str, Any]], Verdict, int]:
        rig = Rig()
        rig.set_mode(mode, expires=NOW + timedelta(hours=1) if mode == "social" else None)
        await rig.prime()
        kiosk = KioskQueue()
        handlers = build_chat_handlers(
            rig.registry,  # type: ignore[arg-type]
            {MM: ReadBackend(msgs)},  # type: ignore[dict-item]
            rig.threads,
            AgentContext(),
            filters=rig.filters,
        )
        args = {"dev_name": "babel", "backend": MM}
        args.update({"channel": "chan"} if op == "read" else {"post_id": "p1"})
        result = await handlers[op](args)
        # The same crewmate post on push: addressing applies there, so it is dropped.
        pushed = await rig.admit(unaddressed, "push")
        return result["messages"], pushed, kiosk.queue("babel").qsize()

    for mode in ("crew", "trusted"):
        got, pushed, queued = run(scenario(mode))
        assert [m["post_id"] for m in got] == [unaddressed.post_id], mode
        assert "outsider" not in got[0]
        assert pushed.reason == "unaddressed"
        assert queued == 0  # omitted, never enqueued to the kiosk
    got, _pushed, _queued = run(scenario("social"))
    assert [m["post_id"] for m in got] == [m.post_id for m in msgs]
    assert [m.get("outsider", False) for m in got] == [False, True, True]


def test_pull_never_kiosk(rig: Rig) -> None:
    async def scenario() -> list[Verdict]:
        await rig.prime()
        return [
            await rig.admit(mm(OUTSIDER, "@babel direct", is_bot=False), "pull"),
            await rig.admit(mm(RIVAL_BOT, "@babel direct"), "pull"),
            await rig.admit(mm(ALLY_BOT, "@babel direct"), "pull"),
        ]

    verdicts = run(scenario())
    assert all(v.kind == Kind.DROP for v in verdicts)


def test_marked_outsider_event_on_push(rig: Rig) -> None:
    rig.set_mode("social", expires=NOW + timedelta(hours=1))
    bell = Doorbell({}, CursorStore(None), admit=rig.filters.admit_event, kiosk=KioskQueue())

    async def scenario() -> dict[str, Any]:
        await rig.prime()
        await bell.offer("babel", MM, to_event(MM, mm(OUTSIDER, is_bot=False)))
        return (await bell.next_event("babel")).payload

    payload = run(scenario())
    assert payload["outsider"] is True and payload["message"]["outsider"] is True


def test_discord_crew_equivalent_squawkie_exempt(rig: Rig) -> None:
    rig.threads.add("babel", "700000000000000001", "discord")

    async def scenario() -> list[Verdict]:
        # No broker cache at all: Discord and squawkie never need it.
        return [
            await rig.admit(mm("u", "@babel hi", is_bot=False, backend="discord")),
            await rig.admit(mm("u", "@agents in my thread", root="700000000000000001",
                               is_bot=True, backend="discord")),
            await rig.admit(mm("u", "no mention", root="700000000000000001", is_bot=True,
                               backend="discord")),
            await rig.admit(mm("u", "no mention", is_bot=True, backend="discord"), "pull"),
            await rig.admit(mm("u", "x", backend="squawkie")),
        ]

    human, bot_mentioned, bot_unmentioned, pulled, squawkie = run(scenario())
    assert human == DELIVER and bot_mentioned == DELIVER
    assert bot_unmentioned.reason == "loop_guard"  # the loop guard still applies on push
    assert pulled == DELIVER and squawkie == DELIVER


def test_team_token_needs_own_crew_author(rig: Rig) -> None:
    """D-103: membership supplies `author_crew`, so `@wave` addresses only for our crew."""
    rig.set_mode("trusted")

    async def scenario() -> list[Verdict]:
        await rig.prime()
        rig.threads.add("babel", "root-9", MM)
        return [
            await rig.admit(mm(HUMAN, "@wave standup", is_bot=False)),
            await rig.admit(mm(ALLY_HUMAN, "@wave standup", is_bot=False)),
        ]

    own, ally = run(scenario())
    assert own == DELIVER
    assert ally.reason == "unaddressed"
