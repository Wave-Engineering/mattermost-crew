"""The daemon's inbound filters: the switch cache, membership, the loop guard and the switch
(Dev Spec §5.9; Story 3.3; R-20 to R-23, R-25, R-31, R-34; D-100).

**`SwitchCache`** holds, per local broker (crew, server_id), the last good `crew_info` and
`switch_state`. It fetches both every 30 s (`run`), right after that broker's `mint`
(`refresh`, from `Registry.register`), and on a cache miss (`miss`). Every call goes through
the `BrokerClient`s of Story 1.4, each with its `broker_timeout_s`, and the brokers are asked
concurrently, so one that doesn't answer costs at most one timeout and never stalls another.

- A broker that doesn't answer keeps its last good entries. A last good `crew_info` older than
  `crew_info_max_age_s` (default 300) counts as **not cached**, so a broker answering with
  errors can't keep a reassigned bot in the crew indefinitely.
- A trusted crew's membership comes **only** from its own broker's `crew_info`, fetched
  directly from `[crews.<name>.<server_id>] broker_socket`. A trusted name with no such table
  on this host is skipped (logged once), and its senders are outsiders.
- **Cache miss.** A bot account (`is_bot`) in no cached `minted_bot_ids` makes `admit`
  re-fetch every local broker's `crew_info` once and re-check before any verdict. A bot
  account is re-fetched for at most once per 30 s, and rounds across all bots are at least
  `refetch_min_interval_s` (default 2 s) apart: a miss while a round is in flight waits for
  it, and a round that fails or times out gives `DROP` (R-22). The daemon never reads
  `owner_id`: a bot token can't see it (D-100).
- **Social engaged** (Story 3.6, R-33). Each `switch_state` fetch compares the broker's
  `effective_mode` with the one its previous fetch gave. A move to `social` from `crew` or
  `trusted` (or a first fetch that finds social) calls each `social_listeners(crew,
  server_id)` once; no other change calls them. `crewchat serve` wires the kiosk review.

**`admit(msg, reg, now, path)`** gives one `Verdict`: `DELIVER`, `DELIVER_MARKED_OUTSIDER`,
`KIOSK` or `DROP(reason)`. `path` is `push` (the doorbell) or `pull` (`read`, `thread`).

- **Discord** is crew-equivalent: `DELIVER`, with the loop guard on push (R-15). **squawkie**
  events are exempt. Neither needs the broker, so both flow while it is down (R-27).
- **Mattermost membership.** A bot account is in crew X if and only if its user ID is in X's
  `minted_bot_ids` (X's broker lists only the bots it minted and still owns, D-100); a human
  is in X if and only if it is in X's `human_user_ids`. Username suffixes are never read. With
  no switch state or `crew_info` cached for the agent's crew, or an author the server can't
  resolve, the verdict is `DROP`. While a trusted crew's local broker on the server has no
  cached `crew_info`, an unrecognised bot account is `DROP`, never `KIOSK`. An untrusted
  crew's uncached broker changes nothing: its bots are outsiders either way (D-115).
- **Push order:** membership, addressing (`addressed_to`, with membership's crew as
  `author_crew`, D-103), the loop guard, then the switch. **Pull order:** membership, then the
  switch, only.
- **The switch.** A crewmate is `DELIVER`. A trusted crew is `DELIVER` under `trusted` and
  `social`, and an outsider under `crew`. A post tagged `props.crew_kiosk=1` is an outsider's
  whoever wrote it. An outsider under `social` (checked against `now`, every delivery) is
  `DELIVER_MARKED_OUTSIDER`. Under `crew` or `trusted`, on push, an outsider is `KIOSK` only
  on a server with `kiosk = true` in `servers.toml` (#117), with the switch's kiosk on, a
  direct address (`is_direct_address`), a post that is not a kiosk post, within
  `kiosk_rate_per_hour` per outsider, per expert, per server, and within the server's
  `kiosk_daily_cap` for the UTC day (`KioskDaily`); otherwise `DROP`. On pull it is omitted,
  so pull never gives `KIOSK` (R-21, R-31, R-34).

**`LoopGuard.admit(msg, reg)`** (R-25). A bot account's message needs an opted-in conversation
(the agent started it or posted in it, per `MyThreads`; or it was mentioned into it; or it is
a DM) and an inline mention (`@<dev>`, `@<dev>.<crew>`, `@agents`, a crew or team token), and
is dropped past `loop_guard_window` (default 20) bot messages in the conversation within the
last hour. A human's message is admitted when it is addressed (R-16 to R-19).

`KioskQueue` is the holding queue push `KIOSK` verdicts go to; Story 3.5 consumes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from crewchat.adapters.base import InboundMessage
from crewchat.addressing import (
    addressed_to,
    group_tokens,
    is_broadcast,
    is_direct_address,
    is_discord,
    own_names,
    tokens,
)
from crewchat.config import (
    DEFAULT_CREW_INFO_MAX_AGE_S,
    DEFAULT_KIOSK_RATE_PER_HOUR,
    DEFAULT_LOOP_GUARD_WINDOW,
    DEFAULT_REFETCH_MIN_INTERVAL_S,
    ServersConfig,
)
from crewchat.registry import Registration, Registry
from crewchat.threads import MyThreads
from crewchat.tokens import (
    BrokerClient,
    BrokerError,
    BrokerKey,
    BrokerUnavailable,
    write_private_json,
)

if TYPE_CHECKING:
    from crewchat.doorbell import DoorbellEvent

REFRESH_INTERVAL_S = 30.0
REFETCH_PER_BOT_S = 30.0
LOOP_GUARD_WINDOW_S = 3600.0  # the loop guard's sliding window
KIOSK_RATE_WINDOW_S = 3600.0
TRACKED_MAX = 10_000  # conversations and senders each table remembers
KIOSK_QUEUE_MAX = 1000
KIOSK_DAILY_FILE = "kiosk-daily.json"  # under the state dir (#117)
MATTERMOST_PREFIX = "mattermost:"
SQUAWKIE = "squawkie"

Mode = Literal["crew", "trusted", "social"]
SAFE_MODES: tuple[Mode, ...] = ("crew", "trusted")
Path_ = Literal["push", "pull"]
Monotonic = Callable[[], float]

log = logging.getLogger("crewchat.filters")


# -- verdicts -------------------------------------------------------------------------------
class Kind(StrEnum):
    DELIVER = "deliver"
    DELIVER_MARKED_OUTSIDER = "deliver_marked_outsider"
    KIOSK = "kiosk"
    DROP = "drop"


@dataclass(frozen=True)
class Verdict:
    kind: Kind
    reason: str = ""

    @property
    def delivers(self) -> bool:
        return self.kind in (Kind.DELIVER, Kind.DELIVER_MARKED_OUTSIDER)


DELIVER = Verdict(Kind.DELIVER)
DELIVER_MARKED_OUTSIDER = Verdict(Kind.DELIVER_MARKED_OUTSIDER)
KIOSK = Verdict(Kind.KIOSK)


def DROP(reason: str) -> Verdict:
    return Verdict(Kind.DROP, reason)


def pass_through(dev_name: str, event: DoorbellEvent) -> bool:
    """The doorbell's default hook: every event is admitted (`crewchat serve` wires `Filters`)."""
    return True


# -- the switch state -----------------------------------------------------------------------
@dataclass(frozen=True)
class SwitchState:
    """A broker's `switch_state` reply, read failing closed (as the broker's `from_json`)."""

    mode: str = "crew"
    expires_at: datetime | None = None
    revert_to: str = "crew"
    kiosk_on: bool = True

    @classmethod
    def from_reply(cls, data: Mapping[str, Any]) -> SwitchState:
        mode, revert_to, kiosk_on = data.get("mode"), data.get("revert_to"), data.get("kiosk_on")
        return cls(
            mode=mode if mode in ("crew", "trusted", "social") else "crew",
            expires_at=_parse_ts(data.get("expires_at")),
            revert_to=revert_to if isinstance(revert_to, str) else "crew",
            kiosk_on=kiosk_on if isinstance(kiosk_on, bool) else True,
        )


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


def effective_mode(state: SwitchState, now: datetime) -> Mode:
    """The mode in force at `now`: social past (or without) its expiry reverts (R-23)."""
    if state.mode == "social":
        if state.expires_at is not None and now < state.expires_at:
            return "social"
        return "trusted" if state.revert_to == "trusted" else "crew"
    return "trusted" if state.mode == "trusted" else "crew"


def utc_now() -> datetime:
    return datetime.now(UTC)


# -- the switch cache -----------------------------------------------------------------------
MissResult = Literal["ok", "capped", "failed"]


class SwitchCache:
    """Each local broker's last good `crew_info` and `switch_state`, by (crew, server_id)."""

    def __init__(
        self,
        brokers: Mapping[BrokerKey, BrokerClient],
        *,
        crew_info_max_age_s: float = DEFAULT_CREW_INFO_MAX_AGE_S,
        refetch_min_interval_s: float = DEFAULT_REFETCH_MIN_INTERVAL_S,
        refetch_per_bot_s: float = REFETCH_PER_BOT_S,
        round_wait_s: float | None = None,
        monotonic: Monotonic = time.monotonic,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._brokers = brokers
        # The wall clock `effective_mode` is read against, for the social transition (R-33).
        self.clock = clock
        # Called with (crew, server_id) when that broker's effective mode becomes `social`.
        self.social_listeners: list[Callable[[str, str], object]] = []
        self._modes: dict[BrokerKey, Mode] = {}
        self.crew_info_max_age_s = crew_info_max_age_s
        self.refetch_min_interval_s = refetch_min_interval_s
        self.refetch_per_bot_s = refetch_per_bot_s
        # How long a miss waits for a round: past every broker's own timeout.
        timeouts = [c.timeout_s for c in brokers.values()]
        self.round_wait_s = round_wait_s if round_wait_s is not None else max(timeouts or [0]) + 1
        self._mono = monotonic
        self._crew_info: dict[BrokerKey, tuple[dict[str, Any], float]] = {}
        self._switch: dict[BrokerKey, SwitchState] = {}
        self._bot_refetch: OrderedDict[str, float] = OrderedDict()
        self._round: asyncio.Task[set[BrokerKey]] | None = None
        self._round_at: float | None = None
        self._round_failed: set[BrokerKey] = set()
        self.rounds = 0  # re-fetch rounds started, for the tests (R-22)

    @classmethod
    def from_config(
        cls, brokers: Mapping[BrokerKey, BrokerClient], config: ServersConfig, **kwargs: Any
    ) -> SwitchCache:
        return cls(
            brokers,
            crew_info_max_age_s=config.crew_info_max_age_s,
            refetch_min_interval_s=config.refetch_min_interval_s,
            **kwargs,
        )

    # -- reading ----------------------------------------------------------------------------
    def has_broker(self, crew: str, server_id: str) -> bool:
        return (crew, server_id) in self._brokers

    def keys(self, server_id: str | None = None) -> list[BrokerKey]:
        return [k for k in self._brokers if server_id is None or k[1] == server_id]

    def crew_info(self, crew: str, server_id: str) -> dict[str, Any] | None:
        """The last good `crew_info`, or None when there is none or it is too old."""
        held = self._crew_info.get((crew, server_id))
        if held is None:
            return None
        info, at = held
        if self._mono() - at > self.crew_info_max_age_s:
            return None
        return info

    def switch_state(self, crew: str, server_id: str) -> SwitchState | None:
        return self._switch.get((crew, server_id))

    def mode(self, crew: str, server_id: str) -> Mode | None:
        """The cached switch's mode in force now, or None with no switch state cached."""
        state = self.switch_state(crew, server_id)
        return None if state is None else effective_mode(state, self.clock())

    # -- fetching ---------------------------------------------------------------------------
    async def _fetch_crew_info(self, key: BrokerKey) -> bool:
        client = self._brokers.get(key)
        if client is None:
            return False
        try:
            info = await client.crew_info()
        except (BrokerUnavailable, BrokerError) as exc:
            log.warning("crew_info %s-%s: %s; keeping the last good", key[0], key[1], exc)
            return False
        self._crew_info[key] = (info, self._mono())
        return True

    async def _fetch_switch(self, key: BrokerKey) -> bool:
        client = self._brokers.get(key)
        if client is None:
            return False
        try:
            state = await client.call("switch_state", {})
        except (BrokerUnavailable, BrokerError) as exc:
            log.warning("switch_state %s-%s: %s; keeping the last good", key[0], key[1], exc)
            return False
        self._switch[key] = SwitchState.from_reply(state)
        self._observe(key)
        return True

    def _observe(self, key: BrokerKey) -> None:
        """Compare this broker's effective mode with the one its last fetch gave: a move to
        `social` (from crew or trusted, or the first fetch finding social) tells each
        listener, once per engagement (R-33). Any other change tells nobody."""
        state = self._switch.get(key)
        if state is None:
            return
        mode = effective_mode(state, self.clock())
        before = self._modes.get(key)
        self._modes[key] = mode
        if mode != "social" or before == "social":
            return
        log.info("social engaged for %s on %s", key[0], key[1])
        for listener in list(self.social_listeners):
            try:
                listener(key[0], key[1])
            except Exception:
                log.exception("social listener for %s-%s failed", key[0], key[1])

    async def refresh(self, crew: str, server_id: str) -> dict[str, Any] | None:
        """Re-fetch one broker's `crew_info` and `switch_state` (after its `mint`). On failure
        the last good entries are kept. The `crew_info` now cached, or None."""
        key = (crew, server_id)
        if key not in self._brokers:
            return None
        await asyncio.gather(self._fetch_crew_info(key), self._fetch_switch(key))
        return self.crew_info(crew, server_id)

    async def refresh_all(self) -> None:
        """Every local broker, concurrently: the 30 s refresh, and the on-demand one."""
        await asyncio.gather(*(self.refresh(c, s) for c, s in self._brokers))

    async def run(self, stop: asyncio.Event, interval_s: float = REFRESH_INTERVAL_S) -> None:
        """Refresh every `interval_s` until `stop` is set, starting now."""
        while not stop.is_set():
            try:
                await self.refresh_all()
            except Exception:
                log.exception("switch cache refresh failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval_s)

    # -- cache misses (R-22) ----------------------------------------------------------------
    async def _crew_info_round(self) -> set[BrokerKey]:
        keys = list(self._brokers)
        results = await asyncio.gather(*(self._fetch_crew_info(k) for k in keys))
        failed = {k for k, ok in zip(keys, results, strict=True) if not ok}
        self._round_failed = failed
        return failed

    async def refetch_round(self) -> set[BrokerKey]:
        """One re-fetch of every local broker's `crew_info`, at most one round per
        `refetch_min_interval_s`. The brokers that failed. Raises `TimeoutError` when the
        round in flight doesn't finish within `round_wait_s`."""
        now = self._mono()
        if self._round is None or self._round.done():
            if self._round_at is not None and now - self._round_at < self.refetch_min_interval_s:
                return set(self._round_failed)  # a round just ran: its answers are current
            self._round_at = now
            self.rounds += 1
            self._round = asyncio.create_task(self._crew_info_round(), name="crew_info round")
        return await asyncio.wait_for(asyncio.shield(self._round), self.round_wait_s)

    async def miss(
        self, bot_id: str, server_id: str, crews: Collection[str] | None = None
    ) -> MissResult:
        """A bot account in no cached `minted_bot_ids`: re-fetch, at most once per bot per
        `refetch_per_bot_s`. `failed` when the round timed out or a broker on `server_id`
        didn't answer, counting only `crews`' brokers when it is given (D-115: an untrusted
        crew's broker being down changes nothing); `capped` when this bot was re-fetched for
        too recently."""
        now = self._mono()
        while self._bot_refetch:
            oldest, at = next(iter(self._bot_refetch.items()))
            if now - at < self.refetch_per_bot_s and len(self._bot_refetch) <= TRACKED_MAX:
                break
            self._bot_refetch.pop(oldest)
        if bot_id in self._bot_refetch:
            return "capped"
        self._bot_refetch[bot_id] = now
        try:
            failed = await self.refetch_round()
        except TimeoutError:
            log.warning("crew_info re-fetch for bot %s timed out", bot_id)
            return "failed"
        mine = [k for k in failed if k[1] == server_id and (crews is None or k[0] in crews)]
        return "failed" if mine else "ok"


# -- the loop guard -------------------------------------------------------------------------
class _Bounded(OrderedDict[Any, Any]):
    """An LRU table: at most `TRACKED_MAX` keys."""

    def touch(self, key: Any, default: Callable[[], Any]) -> Any:
        if key not in self:
            self[key] = default()
        self.move_to_end(key)
        while len(self) > TRACKED_MAX:
            self.popitem(last=False)
        return self[key]


def conversation(msg: InboundMessage) -> str:
    """A message's conversation: its thread root, or itself when it is a root post."""
    return msg.root_id or msg.post_id


class LoopGuard:
    def __init__(
        self,
        threads: MyThreads,
        *,
        window: int = DEFAULT_LOOP_GUARD_WINDOW,
        window_s: float = LOOP_GUARD_WINDOW_S,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self.threads = threads
        self.window = window
        self.window_s = window_s
        self._mono = monotonic
        self._mentioned = _Bounded()  # (dev, backend, conversation) the agent was mentioned into
        self._bot_counts = _Bounded()  # (dev, backend, conversation) -> deque of times

    def _count(self, key: tuple[str, str, str]) -> int:
        times: deque[float] = self._bot_counts.touch(key, deque)
        now = self._mono()
        times.append(now)
        while times and now - times[0] > self.window_s:
            times.popleft()
        return len(times)

    def admit(self, msg: InboundMessage, reg: Registration, author_crew: str | None = None) -> bool:
        roots = self.threads.roots(reg.dev_name)
        conv = conversation(msg)
        key = (reg.dev_name, msg.backend, conv)
        toks = tokens(msg.text)
        direct = bool(toks & own_names(reg))
        if direct:
            self._mentioned.touch(key, lambda: True)  # mentioned into it (R-25)
        if not msg.is_bot:
            return addressed_to(msg, reg, roots, author_crew)
        if self._count(key) > self.window:
            log.info("loop guard: %s past %d bot messages in %s", reg.dev_name, self.window, conv)
            return False
        opted_in = msg.dm or conv in roots or key in self._mentioned
        mentioned = (
            direct
            or is_broadcast(toks, msg.backend)
            or bool(toks & group_tokens(reg, msg.backend, author_crew))
        )
        return opted_in and mentioned


# -- the kiosk holding queue ----------------------------------------------------------------
class KioskQueue:
    """Push `KIOSK` verdicts, per expert (the agent addressed), for Story 3.5's kiosk."""

    def __init__(self, maxsize: int = KIOSK_QUEUE_MAX) -> None:
        self.maxsize = maxsize
        self._queues: dict[str, asyncio.Queue[DoorbellEvent]] = {}
        # Called with the expert's Dev-Name after each event is queued (the kiosk's wake-up).
        self.listeners: list[Callable[[str], None]] = []

    def pending(self) -> list[str]:
        """The experts with events waiting."""
        return [dev for dev, q in self._queues.items() if not q.empty()]

    def queue(self, dev_name: str) -> asyncio.Queue[DoorbellEvent]:
        q = self._queues.get(dev_name)
        if q is None:
            q = asyncio.Queue(self.maxsize)
            self._queues[dev_name] = q
        return q

    def put(self, dev_name: str, event: DoorbellEvent) -> bool:
        try:
            self.queue(dev_name).put_nowait(event)
        except asyncio.QueueFull:
            log.error("kiosk queue for %s is full (%d): dropped an event", dev_name, self.maxsize)
            return False
        for listener in list(self.listeners):
            listener(dev_name)
        return True

    async def get(self, dev_name: str) -> DoorbellEvent:
        return await self.queue(dev_name).get()


# -- the filters ----------------------------------------------------------------------------
Class = Literal["crew", "trusted", "outsider"]


class KioskDaily:
    """Which servers have the kiosk at all, and each one's answers per UTC day (#117).

    `caps` holds only the servers with `kiosk = true`, each with its `kiosk_daily_cap`. The
    count is `KIOSK` verdicts, persisted in `<state_dir>/kiosk-daily.json` as `{server_id:
    {date, count}}`, so a daemon restart never resets a server's day. A bad file is logged and
    read as empty; a failed write is logged, and the count in memory still holds.
    """

    def __init__(
        self,
        caps: Mapping[str, int],
        state_dir: Path | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.caps = dict(caps)
        self.path = None if state_dir is None else state_dir / KIOSK_DAILY_FILE
        self.clock = clock
        self._counts: dict[str, tuple[str, int]] = self._load()
        self._warned: set[tuple[str, str]] = set()  # (server_id, date) the cap was logged

    def server_on(self, server_id: str) -> bool:
        return server_id in self.caps

    def take(self, server_id: str) -> bool:
        """Count one kiosk answer on `server_id` today, or False at the day's cap."""
        today = self.clock().astimezone(UTC).date().isoformat()
        date, count = self._counts.get(server_id, (today, 0))
        if date != today:
            count = 0
        if count >= self.caps.get(server_id, 0):
            if (server_id, today) not in self._warned:
                self._warned.add((server_id, today))
                log.warning(
                    "kiosk: %s reached its daily cap of %d for %s",
                    server_id,
                    self.caps.get(server_id, 0),
                    today,
                )
            return False
        self._counts[server_id] = (today, count + 1)
        self._save()
        return True

    def _load(self) -> dict[str, tuple[str, int]]:
        if self.path is None:
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.error("ignoring unreadable kiosk count %s: %s", self.path, exc)
            return {}
        counts: dict[str, tuple[str, int]] = {}
        for sid, entry in raw.items() if isinstance(raw, dict) else ():
            date = entry.get("date") if isinstance(entry, dict) else None
            count = entry.get("count") if isinstance(entry, dict) else None
            if isinstance(date, str) and isinstance(count, int) and not isinstance(count, bool):
                counts[str(sid)] = (date, count)
        return counts

    def _save(self) -> None:
        if self.path is None:
            return
        data = {sid: {"date": d, "count": c} for sid, (d, c) in self._counts.items()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_private_json(self.path, data)
        except OSError as exc:
            log.error("cannot save the kiosk count at %s: %s", self.path, exc)


def server_of(backend: str) -> str | None:
    return backend[len(MATTERMOST_PREFIX) :] if backend.startswith(MATTERMOST_PREFIX) else None


def _ids(info: Mapping[str, Any], key: str) -> set[str]:
    value = info.get(key)
    return {str(v) for v in value} if isinstance(value, list) else set()


def _trusted(own: Mapping[str, Any], crew: str) -> list[str]:
    """The crews our `crew_info` trusts, in its order, never our own crew."""
    value = own.get("trusted")
    names = [str(v) for v in value] if isinstance(value, list) else []
    return [n for n in names if n != crew]


class Filters:
    def __init__(
        self,
        registry: Registry,
        cache: SwitchCache,
        threads: MyThreads,
        *,
        kiosk_rate_per_hour: int = DEFAULT_KIOSK_RATE_PER_HOUR,
        loop_guard_window: int = DEFAULT_LOOP_GUARD_WINDOW,
        kiosk_caps: Mapping[str, int] | None = None,
        state_dir: Path | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self.registry = registry
        self.cache = cache
        # The one persisted `MyThreads` from `serve()` (D-104): never a second instance.
        self.threads = threads
        self.loop_guard = LoopGuard(threads, window=loop_guard_window, monotonic=monotonic)
        self.kiosk_rate_per_hour = kiosk_rate_per_hour
        self.clock = clock
        self._mono = monotonic
        self._kiosk_hits = _Bounded()  # (outsider, expert, backend) -> deque of times
        # The servers with `kiosk = true`, each with its daily cap (#117). Absent: no kiosk.
        self.kiosk_daily = KioskDaily(kiosk_caps or {}, state_dir, clock=clock)
        self._skipped: set[tuple[str, str]] = set()  # trusted names logged as skipped

    @classmethod
    def from_config(
        cls,
        registry: Registry,
        cache: SwitchCache,
        threads: MyThreads,
        config: ServersConfig,
        **kwargs: Any,
    ) -> Filters:
        return cls(
            registry,
            cache,
            threads,
            kiosk_rate_per_hour=config.kiosk_rate_per_hour,
            loop_guard_window=config.loop_guard_window,
            kiosk_caps={s.id: s.kiosk_daily_cap for s in config.mattermost if s.kiosk},
            **kwargs,
        )

    # -- membership -------------------------------------------------------------------------
    def _crews(self, reg: Registration, server_id: str) -> list[tuple[str, Class, dict[str, Any]]]:
        """Our crew and each trusted crew with a cached `crew_info`, ours first."""
        own = self.cache.crew_info(reg.crew, server_id)
        if own is None:
            return []
        crews: list[tuple[str, Class, dict[str, Any]]] = [(reg.crew, "crew", own)]
        for name in _trusted(own, reg.crew):
            if not self.cache.has_broker(name, server_id):
                if (name, server_id) not in self._skipped:
                    self._skipped.add((name, server_id))
                    log.warning(
                        "trusted crew %s has no [crews.%s.%s] table here: its senders are "
                        "outsiders",
                        name,
                        name,
                        server_id,
                    )
                continue
            info = self.cache.crew_info(name, server_id)
            if info is not None:
                crews.append((name, "trusted", info))
        return crews

    def _lookup(
        self, msg: InboundMessage, reg: Registration, server_id: str
    ) -> tuple[Class, str] | None:
        key = "minted_bot_ids" if msg.is_bot else "human_user_ids"
        for name, cls, info in self._crews(reg, server_id):
            if msg.author_id in _ids(info, key):
                return cls, name
        return None

    async def membership(
        self, msg: InboundMessage, reg: Registration, server_id: str
    ) -> tuple[Class, str | None] | Verdict:
        """The author's class and crew, or a `DROP`."""
        found = self._lookup(msg, reg, server_id)
        if found is not None:
            return found
        if not msg.is_bot:
            return "outsider", None
        # A bot in no cached `minted_bot_ids`: re-fetch, then re-check (R-22). Only our own and
        # our trusted crews' brokers failing can matter (D-115); with our own crew uncached,
        # which crews we trust is unknown, so every broker counts.
        cached = self.cache.crew_info(reg.crew, server_id)
        crews = None if cached is None else {reg.crew, *_trusted(cached, reg.crew)}
        if await self.cache.miss(msg.author_id, server_id, crews) == "failed":
            return DROP("refetch_failed")
        own = self.cache.crew_info(reg.crew, server_id)
        if own is None:
            return DROP("no_cache")
        found = self._lookup(msg, reg, server_id)
        if found is not None:
            return found
        # Only a trusted crew's bot can be misclassified by a cache miss (D-115): an untrusted
        # crew's bots are outsiders whether its broker is cached or not.
        trusted = set(_trusted(own, reg.crew))
        uncached = [
            k
            for k in self.cache.keys(server_id)
            if k[0] in trusted and self.cache.crew_info(*k) is None
        ]
        if uncached:
            return DROP("uncached_crew")  # it may be theirs: never the kiosk
        return "outsider", None

    # -- the verdict ------------------------------------------------------------------------
    async def admit(
        self, msg: InboundMessage, reg: Registration, now: datetime, path: Path_
    ) -> Verdict:
        if msg.backend == SQUAWKIE:
            return DELIVER
        if is_discord(msg.backend):
            if path == "push" and not self.loop_guard.admit(msg, reg):
                return DROP("loop_guard")
            return DELIVER
        server_id = server_of(msg.backend)
        if server_id is None:
            return DROP("unknown_backend")
        state = self.cache.switch_state(reg.crew, server_id)
        if state is None or self.cache.crew_info(reg.crew, server_id) is None:
            return DROP("no_cache")
        if not msg.author_id or not msg.author_resolved:
            return DROP("unresolvable")
        member = await self.membership(msg, reg, server_id)
        if isinstance(member, Verdict):
            return member
        cls, crew = member
        if msg.kiosk:
            cls, crew = "outsider", None  # kiosk text is an outsider's, whoever posted it
        if path == "push":
            roots = self.threads.roots(reg.dev_name)
            if not addressed_to(msg, reg, roots, crew):
                return DROP("unaddressed")
            if not self.loop_guard.admit(msg, reg, crew):
                return DROP("loop_guard")
        mode = effective_mode(state, now)  # checked on every delivery (R-23)
        if cls == "crew" or (cls == "trusted" and mode != "crew"):
            return DELIVER
        if mode == "social":
            return DELIVER_MARKED_OUTSIDER
        if path == "pull":
            return DROP("outsider")  # omitted, never enqueued (R-21)
        if msg.kiosk:
            return DROP("kiosk_post")
        if not self.kiosk_daily.server_on(server_id):
            return DROP("kiosk_server_off")  # the operator's ceiling, over the switch (#117)
        if not state.kiosk_on:
            return DROP("kiosk_off")
        if not is_direct_address(msg, reg, self.threads.roots(reg.dev_name)):
            return DROP("not_direct")
        if not self._kiosk_rate_ok(msg.author_id, reg.dev_name, msg.backend):
            return DROP("kiosk_rate")
        if not self.kiosk_daily.take(server_id):
            return DROP("kiosk_daily_cap")
        return KIOSK

    def _kiosk_rate_ok(self, outsider: str, expert: str, backend: str) -> bool:
        times: deque[float] = self._kiosk_hits.touch((outsider, expert, backend), deque)
        now = self._mono()
        while times and now - times[0] >= KIOSK_RATE_WINDOW_S:
            times.popleft()
        if len(times) >= self.kiosk_rate_per_hour:
            return False
        times.append(now)
        return True

    # -- the hooks --------------------------------------------------------------------------
    async def admit_event(self, dev_name: str, event: DoorbellEvent) -> Verdict:
        """The doorbell's hook (push). Events with no message (squawkie, backend health)
        are delivered."""
        if event.message is None:
            return DELIVER
        reg = self.registry.get(dev_name)
        if reg is None:
            return DROP("not_registered")
        return await self.admit(event.message, reg, self.clock(), "push")

    async def pull(
        self, reg: Registration, msgs: Iterable[InboundMessage]
    ) -> list[tuple[InboundMessage, bool]]:
        """`read`'s and `thread`'s filter: each message kept, with whether it is marked
        outsider content. Everything else is omitted (R-21)."""
        now = self.clock()
        kept: list[tuple[InboundMessage, bool]] = []
        for msg in msgs:
            verdict = await self.admit(msg, reg, now, "pull")
            if verdict.delivers:
                kept.append((msg, verdict.kind == Kind.DELIVER_MARKED_OUTSIDER))
        return kept


def mark_outsider(event: DoorbellEvent) -> DoorbellEvent:
    """`DELIVER_MARKED_OUTSIDER`: `{"outsider": true}` on the event and on its message."""
    payload = dict(event.payload)
    payload["outsider"] = True
    message = payload.get("message")
    if isinstance(message, dict):
        payload["message"] = {**message, "outsider": True}
    return dataclasses.replace(event, payload=payload)


AdmitResult = bool | Verdict
Admit = Callable[[str, "DoorbellEvent"], AdmitResult | Awaitable[AdmitResult]]
