"""The squawkie adapter: `sqtk-doorbell.sh`'s logic, moved into the daemon (Dev Spec §5.2, §5.8;
Story 3.2; R-26, R-27, R-28).

`SquawkieBackend(url, token)` is one squawkie server. It is not a chat backend: it only feeds
the doorbell, and squawkie events are exempt from the filters (§5.9).

- **The feed.** `stream(dev_name)` holds `GET <url>/api/stream` (SSE) for the agent, with
  `Authorization: Bearer <token>` when a token is configured. A `data:` line is an event; a
  `:` line (squawkie's `: connected` and its ~25 s `: ping`) is not.
- **The predicate** (`rings`) is `sqtk-doorbell.sh`'s, verbatim in effect: a `squawk.updated`
  event for one of the agent's squawk IDs, where the squawk is no longer `open` or has an
  answer after its last `→`. The IDs are the ones the shim pushed with `squawk_ids`
  (`ids_for(dev_name)`, read at every event, so a new push applies at once); the daemon never
  reads the agent's squawk records itself (§5.3). As in the script, a squawk ID only matches
  when the event carries it as a number. A shared fixture
  (`tests/fixtures/sqtk_doorbell_predicate.json`) holds this predicate to the script's jq.
- **Catch-up on connect** mirrors `sqtk-drain.sh --pending`. Once the stream is open (squawkie
  sends its headers with `: connected`), the adapter reads the lists (`GET /api/lists`, only
  those with activity since the agent's position when it has one) and their squawks
  (`GET /api/lists/{id}/squawks`). The agent's squawks that ring and haven't rung yet go out as
  one `{"type":"sqtk.catch-up","changes":[{id, list, seq, state, answer, text}]}` event.
  Anything later arrives on the stream, so no answer falls between. A catch-up that fails is
  an outage: the stream is dropped and retried, so an answer is never silently skipped.
- **Exactly once** (R-28). A squawk rings once per `<id>:<state>|<answer>`, its *key*, as
  `--pending`'s `sqtk-rung.json` does: a re-broadcast of the same state and answer, or a
  catch-up after a live ring, is not rung again. The keys rung are each event's `keys`; the
  doorbell's cursor carries them across a restart (`cursor.seen`).
- **Status** (R-27): `on_status(True, "")` once the stream is open and caught up;
  `on_status(False, reason)` on a 401 (`unauthorized`), any other refusal, a failed catch-up,
  or a stream that ends. Reconnects back off 1 s, doubling, capped at 60 s.

`sqtk-doorbell.sh` also leaves out IDs that `sqtk-drain.sh` has already reported in a terminal
state (its seen file, on the agent's side). The daemon can't read that file (§5.3); its own
rung keys do that job, so a settled squawk rings at most once from the daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from crewchat.adapters.base import NO_STATUS, StatusSink, StreamCursor

SQUAWKIE = "squawkie"
ARROW = "→"
BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 60.0
READ_TIMEOUT_S = 90.0  # over squawkie's ~25 s heartbeat: a silent dead stream is noticed
CONNECT_TIMEOUT_S = 10.0
CATCH_UP_MARGIN_S = 60.0  # a list's activity this close before the position still counts
RECENT_KEYS = 1000

log = logging.getLogger("crewchat.adapters.squawkie")

IdsFor = Callable[[str], Collection[str]]


class SquawkieError(Exception):
    """squawkie refused a request or answered something unreadable."""


@dataclass(frozen=True)
class SquawkEvent:
    """One doorbell event from squawkie: the event's JSON (`payload`), the rung keys it
    carries (`keys`, the first is its ID) and its time (`ts`, seconds since the epoch)."""

    payload: dict[str, Any]
    keys: tuple[str, ...]
    ts: float = 0.0

    @property
    def id(self) -> str:
        return self.keys[0]


def squawk_answer(text: Any) -> str:
    """The text after the squawk's last `→`, trimmed; `""` when it has no arrow."""
    if not isinstance(text, str):
        return ""
    parts = text.split(ARROW)
    # jq's `gsub("^\\s+|\\s+$"; "")`: its `\s` is Unicode whitespace, like `str.strip`.
    return parts[-1].strip() if len(parts) > 1 else ""


def _squawk_id(squawk: Mapping[str, Any]) -> str | None:
    """The squawk's ID as the agent's ids hold it, or None when it is not a number (as jq's
    `index` would not match a string against the squawk records' numbers)."""
    value = squawk.get("id")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return str(int(value)) if float(value).is_integer() else str(value)


def _settled(squawk: Mapping[str, Any]) -> bool:
    """No longer open, or answered: `.state != "open" or (.text | answer) != ""`."""
    if squawk.get("state") != "open":
        return True
    text = squawk.get("text")
    if not isinstance(text, str):
        raise ValueError("text is not a string")  # jq errors: no ring
    return squawk_answer(text) != ""


def rings(event: Any, ids: Collection[str]) -> bool:
    """`sqtk-doorbell.sh`'s predicate: a `squawk.updated` event on one of `ids` whose squawk
    is no longer open or has an answer after its last arrow."""
    if not isinstance(event, dict) or event.get("type") != "squawk.updated":
        return False
    squawk = event.get("squawk")
    if not isinstance(squawk, dict):
        return False
    sid = _squawk_id(squawk)
    if sid is None or sid not in ids:
        return False
    try:
        return _settled(squawk)
    except ValueError:
        return False


def squawk_key(squawk: Mapping[str, Any]) -> str:
    """`<id>:<state>|<answer>`: a squawk rings once per key (`sqtk-rung.json`'s value)."""
    return f"{_squawk_id(squawk)}:{squawk.get('state')}|{squawk_answer(squawk.get('text'))}"


def iso_ts(value: Any) -> float:
    """An ISO 8601 time as seconds since the epoch; 0.0 when it isn't one."""
    if not isinstance(value, str):
        return 0.0
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


def backoff_delays(first: float = BACKOFF_FIRST_S, cap: float = BACKOFF_MAX_S) -> Iterator[float]:
    delay = first
    while True:
        yield delay
        delay = min(delay * 2, cap)


@dataclass
class _Agent:
    closed: bool = False
    since: float = 0.0  # the newest event's time: catch-up reads lists active since then
    rung: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def remember(self, keys: Collection[str]) -> None:
        for key in keys:
            self.rung[key] = None
            self.rung.move_to_end(key)
        while len(self.rung) > RECENT_KEYS:
            self.rung.popitem(last=False)


class SquawkieBackend:
    """squawkie's SSE feed, filtered to each agent's own squawks (R-26)."""

    name = SQUAWKIE

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        ids_for: IdsFor | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_timeout_s: float = READ_TIMEOUT_S,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token or None
        self._ids_for: IdsFor = ids_for if ids_for is not None else (lambda _dev: ())
        self._http = http_client
        self._owns_http = http_client is None
        self._sleep = sleep
        self.read_timeout_s = read_timeout_s
        self._agents: dict[str, _Agent] = {}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=self.read_timeout_s)
            )
        return self._http

    def _headers(self, **extra: str) -> dict[str, str]:
        headers = dict(extra)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def ids(self, dev_name: str) -> set[str]:
        return {str(i) for i in self._ids_for(dev_name)}

    async def _get_json(self, path: str) -> Any:
        resp = await self._client().get(f"{self.url}{path}", headers=self._headers())
        if resp.status_code == 401:
            raise SquawkieError("unauthorized")
        if resp.status_code != 200:
            raise SquawkieError(f"GET {path}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SquawkieError(f"GET {path}: not JSON") from exc

    # -- catch-up (`sqtk-drain.sh --pending`) ------------------------------------------------
    async def catch_up(self, dev_name: str) -> SquawkEvent | None:
        """The agent's squawks that ring and haven't rung, as one `sqtk.catch-up` event."""
        agent = self._agents.setdefault(dev_name, _Agent())
        ids = self.ids(dev_name)
        if not ids:
            return None
        lists = await self._get_json("/api/lists")
        if not isinstance(lists, list):
            raise SquawkieError("GET /api/lists: not a list")
        changes: list[dict[str, Any]] = []
        keys: list[str] = []
        newest = 0.0
        for entry in lists:
            if not isinstance(entry, dict):
                continue
            list_id = entry.get("id")
            if isinstance(list_id, bool) or not isinstance(list_id, int):
                continue
            if agent.since and iso_ts(entry.get("last_activity")) < (
                agent.since - CATCH_UP_MARGIN_S
            ):
                continue  # nothing on it has changed since the agent's last event
            squawks = await self._get_json(f"/api/lists/{list_id}/squawks")
            if not isinstance(squawks, list):
                continue
            for squawk in squawks:
                if not isinstance(squawk, dict):
                    continue
                sid = _squawk_id(squawk)
                if sid is None or sid not in ids:
                    continue
                try:
                    if not _settled(squawk):
                        continue
                except ValueError:
                    continue
                key = squawk_key(squawk)
                if key in agent.rung or key in keys:
                    continue
                keys.append(key)
                newest = max(newest, iso_ts(squawk.get("updated_at")))
                changes.append(
                    {
                        "id": squawk.get("id"),
                        "list": entry.get("name"),
                        "seq": squawk.get("seq"),
                        "state": squawk.get("state"),
                        "answer": squawk_answer(squawk.get("text")),
                        "text": squawk.get("text"),
                    }
                )
        if not changes:
            return None
        return SquawkEvent({"type": "sqtk.catch-up", "changes": changes}, tuple(keys), newest)

    # -- the stream -------------------------------------------------------------------------
    def _event(self, dev_name: str, agent: _Agent, line: str) -> SquawkEvent | None:
        if not line.startswith("data:"):
            return None  # a comment (`: connected`, `: ping`) or another SSE field
        try:
            event = json.loads(line[len("data:") :])
        except ValueError:
            return None
        if not rings(event, self.ids(dev_name)):
            return None
        key = squawk_key(event["squawk"])
        if key in agent.rung:
            return None  # this state and answer already rang (R-28)
        return SquawkEvent(event, (key,), iso_ts(event["squawk"].get("updated_at")))

    def _yielded(self, agent: _Agent, event: SquawkEvent) -> None:
        agent.remember(event.keys)
        agent.since = max(agent.since, event.ts)

    async def stream(
        self,
        dev_name: str,
        *,
        cursor: StreamCursor | None = None,
        on_status: StatusSink = NO_STATUS,
    ) -> AsyncIterator[SquawkEvent]:
        """The agent's squawk answers: a catch-up on each connect, then the live feed."""
        agent = self._agents.setdefault(dev_name, _Agent())
        agent.closed = False
        if cursor is not None:
            agent.remember(sorted(cursor.seen | {cursor.id} - {""}))
            agent.since = max(agent.since, cursor.ts)
        delays = backoff_delays()
        while not agent.closed:
            try:
                async with self._client().stream(
                    "GET",
                    f"{self.url}/api/stream",
                    headers=self._headers(Accept="text/event-stream"),
                ) as resp:
                    if resp.status_code != 200:
                        reason = (
                            "unauthorized"
                            if resp.status_code == 401
                            else f"stream refused: HTTP {resp.status_code}"
                        )
                        log.warning("squawkie stream %s: %s", dev_name, reason)
                        on_status(False, reason)
                    else:
                        try:
                            caught = await self.catch_up(dev_name)
                        except (SquawkieError, httpx.HTTPError) as exc:
                            log.warning("squawkie catch-up for %s: %s", dev_name, exc)
                            on_status(False, f"catch-up failed: {exc}")
                        else:
                            on_status(True, "")
                            delays = backoff_delays()
                            if caught is not None:
                                self._yielded(agent, caught)
                                yield caught
                            async for line in resp.aiter_lines():
                                if agent.closed:
                                    break
                                event = self._event(dev_name, agent, line)
                                if event is not None:
                                    self._yielded(agent, event)
                                    yield event
                            if not agent.closed:
                                log.warning("squawkie stream %s ended", dev_name)
                                on_status(False, "stream ended")
            except (httpx.HTTPError, httpx.StreamError) as exc:
                if not agent.closed:
                    log.warning("squawkie stream %s: %s", dev_name, exc)
                    on_status(False, f"stream failed: {exc}")
            if agent.closed:
                break
            await self._sleep(next(delays))

    async def close_agent(self, dev_name: str) -> None:
        """End `dev_name`'s stream at its next line (the doorbell also cancels its task)."""
        agent = self._agents.get(dev_name)
        if agent is not None:
            agent.closed = True

    async def aclose(self) -> None:
        for dev_name in list(self._agents):
            await self.close_agent(dev_name)
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
