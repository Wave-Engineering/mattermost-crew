"""The doorbell core: one queue per agent, fed by every backend (Dev Spec §4.7, §5.8; Story 3.2;
R-26, R-27, R-28).

`Doorbell(sources, cursors)` runs **one task per (agent, backend)**. Each task consumes that
backend's `stream(dev_name, cursor=..., on_status=...)`, turns what it yields into a
`DoorbellEvent`, passes it through the filter hook (`admit`: `crewchat serve` wires
`filters.Filters.admit_event`, Story 3.3) and acts on the verdict: `DELIVER` puts it on the
agent's one `asyncio.Queue`, `DELIVER_MARKED_OUTSIDER` puts it there with `"outsider": true`,
`KIOSK` puts it on the kiosk's holding queue (Story 3.5) and `DROP` puts it nowhere.
`register` attaches the agent's backends (`attach`): its Mattermost servers and Discord from
the registration, and squawkie when it is configured (§4.2 step 3).

- **`subscribe{dev_name}`** awaits that agent's queue and returns **one** event (R-26): the
  thin one-shot exits on it. An agent that hasn't registered gets `not_registered`.
- **Events.** A chat message is `{"type":"message","backend":b,"message":{...}}`, the message
  as `read` returns one. squawkie's are its own JSON with `backend: "squawkie"`:
  `squawk.updated`, or `sqtk.catch-up` with its `changes`, as `sqtk-doorbell.sh` prints them.
- **Cursors** (R-28). `CursorStore` keeps, per agent per backend,
  `/var/lib/crewchat/cursors/<dev>/<backend>.json`: `{"id", "ts", "seen": [...]}`, the last
  event handed to the agent, its time, and the most recent IDs handed over (file `0600`,
  directories `0700`, written atomically). The cursor moves when `subscribe` hands an event
  over, so an event still queued when the daemon stops is replayed after it starts. Each
  stream starts from its cursor and replays what came after it; the doorbell also drops any
  event whose IDs it has already queued or handed over, so a replay overlapping the live feed
  delivers each event exactly once.
- **Backend health** (R-27). A stream reports its state through `on_status`; a stream that
  raises or ends counts as down too, and is restarted after a backoff (1 s, doubling, capped at
  60 s). The first report of an outage enqueues
  `{"type":"backend.down","backend":b,"reason":...}`, once; the outage's later reports add
  nothing. The next report that the stream is up enqueues `{"type":"backend.up","backend":b}`.
  Every other task keeps running throughout.
- The queue holds at most `QUEUE_MAX` events. Past that an event is dropped, loudly in the log;
  a dropped message is not handed over, so its cursor does not move past it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import logging
import os
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from crewchat import filters
from crewchat.adapters.base import InboundMessage, StatusSink, StreamCursor
from crewchat.adapters.squawkie import SquawkEvent
from crewchat.registry import Registry
from crewchat.server import Handler, OpError
from crewchat.threads import SAFE_NAME_RE, STATE_DIR
from crewchat.tokens import DIR_MODE, write_private_json

CURSORS_SUBDIR = "cursors"
RECENT_IDS = 1000  # the IDs a cursor remembers handing over, and the doorbell having queued
QUEUE_MAX = 1000
RESTART_FIRST_S = 1.0
RESTART_MAX_S = 60.0

log = logging.getLogger("crewchat.doorbell")


@dataclass(frozen=True)
class DoorbellEvent:
    """One event on an agent's queue: what `subscribe` returns (`payload`), the backend it came
    from, and the IDs it carries (`keys`; none for `backend.down` and `backend.up`). A chat
    message's event also carries the `InboundMessage` itself, for the filters (Story 3.3)."""

    backend: str
    payload: dict[str, Any]
    keys: tuple[str, ...] = ()
    ts: float = 0.0
    message: InboundMessage | None = dataclasses.field(default=None, compare=False, repr=False)


StreamItem = InboundMessage | SquawkEvent


class Source(Protocol):
    """A backend the doorbell reads: every chat backend, and squawkie."""

    def stream(
        self, dev_name: str, *, cursor: StreamCursor | None = ..., on_status: StatusSink = ...
    ) -> AsyncIterator[Any]: ...


def to_event(backend: str, item: StreamItem) -> DoorbellEvent:
    """What a stream yielded, as a queued event."""
    if isinstance(item, InboundMessage):
        payload = {"type": "message", "backend": backend, "message": dataclasses.asdict(item)}
        return DoorbellEvent(backend, payload, (item.post_id,), item.ts, item)
    return DoorbellEvent(backend, {**item.payload, "backend": backend}, item.keys, item.ts)


def backend_down(backend: str, reason: str) -> DoorbellEvent:
    return DoorbellEvent(backend, {"type": "backend.down", "backend": backend, "reason": reason})


def backend_up(backend: str) -> DoorbellEvent:
    return DoorbellEvent(backend, {"type": "backend.up", "backend": backend})


def restart_delays(first: float = RESTART_FIRST_S, cap: float = RESTART_MAX_S) -> Iterator[float]:
    delay = first
    while True:
        yield delay
        delay = min(delay * 2, cap)


class _Recent:
    """An ordered, bounded set of IDs, oldest first."""

    def __init__(self, ids: Iterable[str] = (), limit: int = RECENT_IDS) -> None:
        self.limit = limit
        self._ids: OrderedDict[str, None] = OrderedDict()
        self.add(ids)

    def add(self, ids: Iterable[str]) -> None:
        for i in ids:
            if not i:
                continue
            self._ids[i] = None
            self._ids.move_to_end(i)
        while len(self._ids) > self.limit:
            self._ids.popitem(last=False)

    def __contains__(self, item: object) -> bool:
        return item in self._ids

    def ids(self) -> list[str]:
        return list(self._ids)


@dataclass
class _Position:
    id: str = ""
    ts: float = 0.0
    seen: _Recent = dataclasses.field(default_factory=_Recent)


class CursorStore:
    """Each agent's cursor per backend, under `<state_dir>/cursors/<dev>/<backend>.json`.

    Built with no `state_dir`, the cursors live in memory only (the unit tests' store)."""

    def __init__(self, state_dir: Path | None = STATE_DIR, *, limit: int = RECENT_IDS) -> None:
        self.state_dir = state_dir
        self.limit = limit
        self._held: dict[tuple[str, str], _Position] = {}

    @property
    def root(self) -> Path | None:
        return None if self.state_dir is None else self.state_dir / CURSORS_SUBDIR

    def path(self, dev_name: str, backend: str) -> Path | None:
        if self.root is None:
            return None
        if not SAFE_NAME_RE.fullmatch(dev_name) or not _safe_backend(backend):
            return None
        return self.root / dev_name / f"{backend}.json"

    def _position(self, dev_name: str, backend: str) -> _Position:
        key = (dev_name, backend)
        held = self._held.get(key)
        if held is None:
            held = self._read(dev_name, backend)
            self._held[key] = held
        return held

    def _read(self, dev_name: str, backend: str) -> _Position:
        path = self.path(dev_name, backend)
        if path is None or not path.exists():
            return _Position(seen=_Recent(limit=self.limit))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            last, ts, seen = raw["id"], raw["ts"], raw["seen"]
            if not isinstance(last, str) or not isinstance(seen, list):
                raise ValueError("id is a string and seen a list")
            if isinstance(ts, bool) or not isinstance(ts, int | float):
                raise ValueError("ts is a number")
            if not all(isinstance(s, str) for s in seen):
                raise ValueError("seen holds strings")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.error("ignoring unreadable cursor %s: %s", path, exc)
            return _Position(seen=_Recent(limit=self.limit))
        return _Position(last, float(ts), _Recent(seen, self.limit))

    def load(self, dev_name: str, backend: str) -> StreamCursor | None:
        """The agent's cursor on `backend`, or None when nothing was ever handed over."""
        held = self._position(dev_name, backend)
        if not held.id and not held.seen.ids():
            return None
        return StreamCursor(held.id, held.ts, frozenset(held.seen.ids()))

    def advance(self, dev_name: str, backend: str, event: DoorbellEvent) -> None:
        """`event` was handed to the agent: move its cursor there and save it."""
        if not event.keys:
            return
        held = self._position(dev_name, backend)
        held.id = event.keys[0]
        held.ts = max(held.ts, event.ts)
        held.seen.add(event.keys)
        self._save(dev_name, backend, held)

    def _save(self, dev_name: str, backend: str, held: _Position) -> None:
        path = self.path(dev_name, backend)
        if path is None:
            return
        try:
            path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            os.chmod(path.parent, DIR_MODE)
            write_private_json(path, {"id": held.id, "ts": held.ts, "seen": held.seen.ids()})
        except OSError as exc:
            log.error("cannot save the cursor for %s on %s at %s: %s", dev_name, backend, path, exc)


def _safe_backend(backend: str) -> bool:
    """`mattermost:<server_id>`, `discord` or `squawkie`: a name with no `/` or leading dot."""
    return bool(backend) and "/" not in backend and not backend.startswith(".")


class Doorbell:
    def __init__(
        self,
        sources: Mapping[str, Source],
        cursors: CursorStore,
        *,
        admit: filters.Admit = filters.pass_through,
        kiosk: filters.KioskQueue | None = None,
        queue_max: int = QUEUE_MAX,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sources = dict(sources)
        self.cursors = cursors
        self.admit = admit
        # Where push `KIOSK` verdicts wait for Story 3.5's kiosk; None drops them, loudly.
        self.kiosk = kiosk
        self.queue_max = queue_max
        self._sleep = sleep
        self._queues: dict[str, asyncio.Queue[DoorbellEvent]] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._down: dict[tuple[str, str], bool] = {}
        # IDs queued or handed over in this process, per (agent, backend): never twice (R-28).
        self._handled: dict[tuple[str, str], _Recent] = {}

    # -- the queue --------------------------------------------------------------------------
    def queue(self, dev_name: str) -> asyncio.Queue[DoorbellEvent]:
        q = self._queues.get(dev_name)
        if q is None:
            q = asyncio.Queue(self.queue_max)
            self._queues[dev_name] = q
        return q

    def _enqueue(self, dev_name: str, event: DoorbellEvent) -> bool:
        try:
            self.queue(dev_name).put_nowait(event)
        except asyncio.QueueFull:
            log.error(
                "doorbell queue for %s is full (%d): dropped a %s event from %s",
                dev_name,
                self.queue_max,
                event.payload.get("type"),
                event.backend,
            )
            return False
        return True

    def deliver(self, dev_name: str, event: DoorbellEvent) -> bool:
        """Put a daemon-made event (the kiosk review, Story 3.6) straight on the agent's
        queue, past the filters. False when the queue is full and it was dropped."""
        return self._enqueue(dev_name, event)

    async def next_event(self, dev_name: str) -> DoorbellEvent:
        """The agent's next event; its cursor moves past it (R-28)."""
        event = await self.queue(dev_name).get()
        self.cursors.advance(dev_name, event.backend, event)
        return event

    # -- health (R-27) ----------------------------------------------------------------------
    def status(self, dev_name: str, backend: str, up: bool, reason: str = "") -> None:
        """A stream's state: one `backend.down` per outage, one `backend.up` at its end."""
        key = (dev_name, backend)
        down = self._down.get(key, False)
        if not up and not down:
            self._down[key] = True
            log.warning("backend %s down for %s: %s", backend, dev_name, reason)
            self._enqueue(dev_name, backend_down(backend, reason or "unavailable"))
        elif up and down:
            self._down[key] = False
            log.info("backend %s up again for %s", backend, dev_name)
            self._enqueue(dev_name, backend_up(backend))

    def is_down(self, dev_name: str, backend: str) -> bool:
        return self._down.get((dev_name, backend), False)

    # -- the tasks --------------------------------------------------------------------------
    def attach(self, dev_name: str, backends: Iterable[str]) -> list[str]:
        """Start a task for each of `backends` the doorbell has a source for and isn't
        already running for `dev_name`. The backends newly attached."""
        self.queue(dev_name)
        started: list[str] = []
        for backend in backends:
            key = (dev_name, backend)
            if backend not in self.sources:
                continue
            task = self._tasks.get(key)
            if task is not None and not task.done():
                continue
            self._tasks[key] = asyncio.create_task(
                self._run(dev_name, backend), name=f"doorbell {dev_name} {backend}"
            )
            started.append(backend)
        return started

    def attached(self, dev_name: str) -> list[str]:
        return sorted(b for (d, b), t in self._tasks.items() if d == dev_name and not t.done())

    def _handled_for(self, dev_name: str, backend: str) -> _Recent:
        key = (dev_name, backend)
        handled = self._handled.get(key)
        if handled is None:
            cursor = self.cursors.load(dev_name, backend)
            handled = _Recent(cursor.seen if cursor is not None else ())
            self._handled[key] = handled
        return handled

    def _cursor(self, dev_name: str, backend: str) -> StreamCursor | None:
        """Where a (re)started stream resumes: the saved cursor, and every ID already queued."""
        saved = self.cursors.load(dev_name, backend)
        handled = self._handled_for(dev_name, backend).ids()
        if saved is None and not handled:
            return None
        base = saved if saved is not None else StreamCursor()
        return StreamCursor(base.id, base.ts, frozenset(handled) | base.seen)

    async def verdict(self, dev_name: str, event: DoorbellEvent) -> filters.Verdict:
        result = self.admit(dev_name, event)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, filters.Verdict):
            return result
        return filters.DELIVER if result else filters.DROP("refused")

    async def offer(self, dev_name: str, backend: str, event: DoorbellEvent) -> bool:
        """Put `event` through the filters (R-21) unless it is a repeat: on the agent's queue
        (marked when it is outsider content), on the kiosk's holding queue, or nowhere.
        True when it went on the agent's queue."""
        handled = self._handled_for(dev_name, backend)
        if event.keys and all(k in handled for k in event.keys):
            return False  # a replay of an event already queued or handed over (R-28)
        handled.add(event.keys)
        verdict = await self.verdict(dev_name, event)
        if verdict.kind == filters.Kind.KIOSK:
            if self.kiosk is None:
                log.error("no kiosk queue: dropped a kiosk event for %s from %s", dev_name, backend)
            else:
                self.kiosk.put(dev_name, event)
            return False
        if not verdict.delivers:
            log.debug("dropped an event for %s from %s: %s", dev_name, backend, verdict.reason)
            return False
        if verdict.kind == filters.Kind.DELIVER_MARKED_OUTSIDER:
            event = filters.mark_outsider(event)
        self._enqueue(dev_name, event)
        return True

    async def _run(self, dev_name: str, backend: str) -> None:
        source = self.sources[backend]
        sink: StatusSink = partial(self.status, dev_name, backend)
        delays = restart_delays()
        while True:
            try:
                stream = source.stream(
                    dev_name, cursor=self._cursor(dev_name, backend), on_status=sink
                )
                async for item in stream:
                    delays = restart_delays()
                    await self.offer(dev_name, backend, to_event(backend, item))
                reason = "stream ended"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("doorbell stream %s for %s failed", backend, dev_name)
                reason = f"stream failed: {exc}"
            self.status(dev_name, backend, False, reason)
            await self._sleep(next(delays))

    async def detach(self, dev_name: str) -> None:
        """Stop every task of `dev_name`'s."""
        for key in [k for k in self._tasks if k[0] == dev_name]:
            task = self._tasks.pop(key)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def aclose(self) -> None:
        for dev_name in {d for d, _ in self._tasks}:
            await self.detach(dev_name)

    # -- token hooks (crewchat.tokens.Adapter) ----------------------------------------------
    async def on_token_rotated(self, dev_name: str) -> None:
        """Nothing to do: each adapter reconnects its own stream, which replays the gap."""

    async def close_agent(self, dev_name: str) -> None:
        """`dev_name` left rotation: stop its tasks, before the adapters close its streams, so
        a stream closed on purpose is not restarted or reported down."""
        await self.detach(dev_name)


def build_doorbell_handlers(
    registry: Registry,
    doorbell: Doorbell,
    register: Handler,
    *,
    extra_backends: Iterable[str] = (),
) -> dict[str, Handler]:
    """`subscribe`, and `register` wrapped so a registration attaches the agent's backends
    (its registration's, plus `extra_backends`: squawkie)."""
    extra = list(extra_backends)

    async def registered(args: dict[str, Any]) -> dict[str, Any]:
        result = await register(args)
        dev_name = args.get("dev_name")
        reg = registry.get(dev_name) if isinstance(dev_name, str) else None
        if reg is not None:
            doorbell.attach(reg.dev_name, [*reg.backends, *extra])
        return result

    async def subscribe(args: dict[str, Any]) -> dict[str, Any]:
        dev_name = args.get("dev_name")
        if not isinstance(dev_name, str) or not dev_name:
            raise OpError("bad_request", "dev_name is a required string")
        if registry.get(dev_name) is None:
            raise OpError("not_registered", dev_name)
        event = await doorbell.next_event(dev_name)
        return event.payload

    return {"register": registered, "subscribe": subscribe}
