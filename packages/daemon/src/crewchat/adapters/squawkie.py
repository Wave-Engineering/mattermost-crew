"""The squawkie adapter: `sqtk-doorbell.sh`'s logic, moved into the daemon (Dev Spec §5.2, §5.8;
Story 3.2; R-26, R-27, R-28).

`SquawkieBackend(url, token)` is one squawkie server. It is not a chat backend: it only feeds
the doorbell, and squawkie events are exempt from the filters (§5.9).

- **The feed.** `stream(dev_name)` holds `GET <url>/api/stream` (SSE) for the agent, with
  `Authorization: Bearer <token>` when a token is configured. A `data:` line is an event; a
  `:` line (squawkie's `: connected` and its ~25 s `: ping`) is not.
- **The predicate** (`rings`) is `sqtk-doorbell.sh`'s, verbatim in effect: a `squawk.updated`
  event for one of the agent's squawk IDs, where the squawk is no longer `open` or has an
  answer. The answer is the kit's `sqtk-answer.jq` (cc#1363, #170, `squawk_answer`): none while
  the squawk is pristine; with its original text O known, what follows O's own arrows; with O
  unknown, what follows the last arrow. The IDs and each one's O are the ones the shim pushed
  with `squawk_ids` (`ids_for`/`texts_for(dev_name)`, read at every event, so a new push
  applies at once); the daemon never reads the agent's squawk records itself (§5.3). As in
  the script, a squawk ID only matches when the event carries it as a number. A shared
  fixture (`tests/fixtures/sqtk_doorbell_predicate.json`, with the kit's `sqtk-answer.jq`
  vendored beside it) holds this predicate to the script's jq.
- **Catch-up on connect** mirrors `sqtk-drain.sh --pending`. Once the stream is open (squawkie
  sends its headers with `: connected`), the adapter reads the lists (`GET /api/lists`, only
  those with activity since the agent's position when it has one) and their squawks
  (`GET /api/lists/{id}/squawks`). The agent's squawks that ring and haven't rung yet go out as
  one `{"type":"sqtk.catch-up","changes":[{id, list, seq, state, answer, text, choice, note}]}`
  event. `choice` (the answer option picked) and `note` (BJ's note, `""` while the squawk is
  open) are squawkie-talkie #168's; they are BJ's own words, passed through unchanged as data
  for the agent to read as an answer, never as an instruction to a tool. A live `squawk.updated`
  event carries them as squawkie sent it.
  Anything later arrives on the stream, so no answer falls between. A catch-up that fails is
  an outage: the stream is dropped and retried, so an answer is never silently skipped.
- **Exactly once** (R-28). A squawk rings once per `<id>:<state>|<answer>`, its *key*, as
  `--pending`'s `sqtk-rung.json` does: a re-broadcast of the same state and answer, or a
  catch-up after a live ring, is not rung again. A `choice` or a `note` is part of the key when
  there is one, so a different one rings again (see `squawk_key`). The keys rung are each
  event's `keys`; the doorbell's cursor carries them across a restart (`cursor.seen`).
- **Status** (R-27): `on_status(True, "")` once the stream is open and caught up;
  `on_status(False, reason)` on a 401 (`unauthorized`), any other refusal, a failed catch-up,
  or a stream that ends. Reconnects back off 1 s, doubling, capped at 60 s.

`sqtk-doorbell.sh` also leaves out IDs that `sqtk-drain.sh` has already reported in a terminal
state (its seen file, on the agent's side). The daemon can't read that file (§5.3); its own
rung keys do that job, so a settled squawk rings at most once from the daemon.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
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
TextsFor = Callable[[str], Mapping[str, str]]


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


# A squawk's answer, by the kit's `hooks/workflow/sqtk-answer.jq` (cc#1363; #170). squawkie has
# no answer field: an answer is typed after the squawk's `→` slot, so an arrow in the author's
# own text ("campaign→main") must not start one. O is the author's original text, which the
# shim pushes with the ids (`texts`). jq's `gsub("^\\s+|\\s+$"; "")` is `str.strip`: its `\s`
# is Unicode whitespace. One margin: Python also counts U+001C to U+001F as whitespace and
# Oniguruma's `\s` doesn't, so those control characters are trimmed here and kept by jq.


def prose_arrows(original: str) -> int:
    """O's arrows, less a closing `→` (the answer slot)."""
    closing = 1 if original.rstrip().endswith(ARROW) else 0
    return max(0, len(original.split(ARROW)) - 1 - closing)


def pristine(squawk: Mapping[str, Any]) -> bool:
    """Never edited, so it holds no answer: both timestamps present and equal."""
    created, updated = squawk.get("created_at"), squawk.get("updated_at")
    return created is not None and updated is not None and created == updated


def legacy_answer(text: str) -> str:
    """Everything after the last arrow (rule 3)."""
    parts = text.split(ARROW)
    return parts[-1].strip() if len(parts) > 1 else ""


def answer_after(text: str, p: int) -> str:
    """Everything after the (p+1)-th arrow, less leading whitespace and arrows (rule 2)."""
    parts = text.split(ARROW)
    if len(parts) <= p + 1:
        return ""
    rest = ARROW.join(parts[p + 1 :]).rstrip()
    i = 0
    while i < len(rest) and (rest[i].isspace() or rest[i] == ARROW):
        i += 1
    return rest[i:]


def kept_arrows(text: str, original: str | None) -> int | None:
    """O's prose arrows when `text` keeps O's segments left of them, else None."""
    if not isinstance(original, str):
        return None
    p = prose_arrows(original)

    def head(s: str) -> list[str]:
        return [seg.strip() for seg in s.split(ARROW)[:p]]

    kept = head(text)
    return p if len(kept) == p and kept == head(original) else None


def squawk_answer(squawk: Mapping[str, Any], original: str | None = None) -> str:
    """`sq_answer($o)`: the squawk's answer, or `""`. Pristine has none; with O known, it
    follows O's prose arrows; with O unknown, or a segment changed, it follows the last arrow.

    A text that is neither a string nor null raises ValueError, where jq would error.
    """
    text = squawk.get("text")
    if text is None or text is False:
        text = ""  # jq's `.text // ""`
    if pristine(squawk):
        return ""
    if not isinstance(text, str):
        raise ValueError("text is not a string")
    p = kept_arrows(text, original)
    answer = answer_after(text, p) if p is not None else legacy_answer(text)
    return "" if only_editor_tags(answer, squawk.get("initials")) else answer


def only_editor_tags(answer: str, initials: Any) -> bool:
    """An answer that is nothing but the last editor's own tag, `(CER)` or `(CER) (CER)`, is a
    signature and not an answer (#206, claudecode-workflow#93): it appears when `sqtk show`'s
    display suffix is copied back into `--text`. A different tag (`(ok)`) or any other text
    is an answer. `initials` is the squawk's last editor, so a human's `(ok)` is safe."""
    if not isinstance(initials, str) or not re.fullmatch(r"[A-Za-z0-9]{1,3}", initials):
        return False
    return re.fullmatch(rf"(?:\({initials}\)\s*)+", answer) is not None


def _answer_or_empty(squawk: Mapping[str, Any], original: str | None) -> str:
    try:
        return squawk_answer(squawk, original)
    except ValueError:
        return ""


def _choice(squawk: Mapping[str, Any]) -> str | None:
    """The answer option picked (squawkie-talkie #168), or None: none picked, or a server
    older than #168 that has no such field."""
    value = squawk.get("choice")
    return value if isinstance(value, str) and value else None


def _note(squawk: Mapping[str, Any]) -> str:
    """BJ's note (squawkie-talkie #168), or "": none written, or a server older than #168.
    An open squawk's note is always "": the server withholds it from events so a half-written
    draft is never pushed, and a catch-up reads the lists over `GET`, which does not redact,
    so this is where the draft stays out (and keeps a catch-up and a live ring on one key)."""
    if squawk.get("state") == "open":
        return ""
    value = squawk.get("note")
    return value if isinstance(value, str) else ""


def _squawk_id(squawk: Mapping[str, Any]) -> str | None:
    """The squawk's ID as the agent's ids hold it, or None when it is not a number (as jq's
    `index` would not match a string against the squawk records' numbers)."""
    value = squawk.get("id")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return str(int(value)) if float(value).is_integer() else str(value)


def _settled(squawk: Mapping[str, Any], original: str | None = None) -> bool:
    """No longer open, or answered: `.state != "open" or sq_answer($o) != ""`. Raises
    ValueError where jq errors (no ring)."""
    if squawk.get("state") != "open":
        return True
    return squawk_answer(squawk, original) != ""


def _original(squawk: Mapping[str, Any], texts: Mapping[str, str] | None) -> str | None:
    """O for this squawk, from the shim's `texts`, keyed by the id as `_squawk_id` writes it."""
    sid = _squawk_id(squawk)
    if not texts or sid is None:
        return None
    value = texts.get(sid)
    return value if isinstance(value, str) else None


def own_initials(dev_name: str) -> str | None:
    """The initials `sqtk` signs `dev_name`'s changes with: uppercase alphanumerics, at most 3
    (`norm_initials`). squawkie stores the last editor's initials on every change, so a
    squawk whose `initials` are these was last changed by the agent itself. None when the name
    leaves none. `SQUAWK_INITIALS` (an env override the daemon can't see) is not covered, and
    two editors whose initials coincide can't be told apart (#206)."""
    initials = "".join(c for c in dev_name.upper() if c.isascii() and c.isalnum())[:3]
    return initials or None


def rings(
    event: Any,
    ids: Collection[str],
    texts: Mapping[str, str] | None = None,
    own: str | None = None,
) -> bool:
    """`sqtk-doorbell.sh`'s predicate: a `squawk.updated` event on one of `ids` whose squawk
    is no longer open or has an answer (`sq_answer`, with O from `texts`). With `own` (the
    agent's initials), a change the agent itself made last rings nothing (#206): it just made
    it, and a woken turn would only tell it what it knows."""
    if not isinstance(event, dict) or event.get("type") != "squawk.updated":
        return False
    squawk = event.get("squawk")
    if not isinstance(squawk, dict):
        return False
    sid = _squawk_id(squawk)
    if sid is None or sid not in ids:
        return False
    if own is not None and squawk.get("initials") == own:
        return False
    try:
        return _settled(squawk, _original(squawk, texts))
    except ValueError:
        return False


def squawk_key(squawk: Mapping[str, Any], texts: Mapping[str, str] | None = None) -> str:
    """`<id>:<state>|<answer>`: a squawk rings once per key (`sqtk-rung.json`'s value).

    A squawk that carries a `choice` or a `note` (squawkie-talkie #168) adds `|choice=<choice>`
    and `|note=<12 hex of the note's sha1>`, so a different choice, or a note written after
    the item was disposed, rings again; the same ones re-broadcast do not. A squawk with
    neither keeps its key byte for byte, so a rung-keys cursor saved before #233 still matches.
    """
    answer = _answer_or_empty(squawk, _original(squawk, texts))
    key = f"{_squawk_id(squawk)}:{squawk.get('state')}|{answer}"
    choice = _choice(squawk)
    if choice:
        key += f"|choice={choice}"
    note = _note(squawk)
    if note:
        digest = hashlib.sha1(note.encode("utf-8"), usedforsecurity=False).hexdigest()
        key += f"|note={digest[:12]}"
    return key


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
        texts_for: TextsFor | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_timeout_s: float = READ_TIMEOUT_S,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token or None
        self._ids_for: IdsFor = ids_for if ids_for is not None else (lambda _dev: ())
        self._texts_for: TextsFor = texts_for if texts_for is not None else (lambda _dev: {})
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

    def texts(self, dev_name: str) -> Mapping[str, str]:
        """Each squawk's original text, by id, as the shim pushed it (#170)."""
        return self._texts_for(dev_name)

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
        texts = self.texts(dev_name)
        own = own_initials(dev_name)
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
                if own is not None and squawk.get("initials") == own:
                    continue  # the agent's own last change: it knows (#206)
                original = _original(squawk, texts)
                try:
                    if not _settled(squawk, original):
                        continue
                except ValueError:
                    continue
                key = squawk_key(squawk, texts)
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
                        "answer": _answer_or_empty(squawk, original),
                        "text": squawk.get("text"),
                        # squawkie-talkie #168: BJ's own words, passed through as data.
                        "choice": _choice(squawk),
                        "note": _note(squawk),
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
        texts = self.texts(dev_name)
        if not rings(event, self.ids(dev_name), texts, own_initials(dev_name)):
            return None
        key = squawk_key(event["squawk"], texts)
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

    async def close_server(self, dev_name: str, server_id: str) -> None:
        """Not a Mattermost server: nothing to close."""

    async def aclose(self) -> None:
        for dev_name in list(self._agents):
            await self.close_agent(dev_name)
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
