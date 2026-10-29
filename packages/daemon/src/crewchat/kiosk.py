"""The kiosk: answers outsiders while social is off (Dev Spec §5.11; Story 3.5; R-31, R-32).

- **The brief.** `BriefStore` holds the latest brief per Dev-Name, as the shim pushed it in a
  `kiosk_brief` frame. The shim extracted the seed's `## Kiosk` section, as the agent's user;
  the daemon never reads the seed file (§5.3, R-32).
- **Sessions.** One `KioskSession` per (expert, server), keyed `(<dev>, <server_id>)`, started
  lazily on the first `KIOSK` verdict for that expert on that server (R-31). It serves every
  outsider on that server (D-067); outsiders on different servers never share one. With no
  brief it answers "I have nothing to share yet." without calling the runner.
- **The prompt** is `KIOSK_HEADER`, the brief, and the history: **only the asker's own posts
  in that thread, and the kiosk's own earlier answers to that asker.** A crewmate's post in the
  thread, or another outsider's, is never included (§5.11).
- **The runner** is the mechanism spike 1.2 chose (D-096): `ClaudeCliRunner`, the `claude -p`
  CLI run as the unprivileged `crewkiosk` user. The daemon keeps `NoNewPrivileges=yes`, so it
  never changes user itself (D-107): it connects to `/run/crewkiosk/kiosk.sock`
  (`packaging/crewkiosk.socket`, `Accept=yes`), and systemd starts one `crewkiosk@` instance
  per connection, as `crewkiosk`, running `packaging/crewkiosk-run`. The daemon writes the
  prompt and half-closes; the wrapper starts `claude -p --restricted --strict-mcp-config
  --tools "" --disallowedTools "mcp__*"` in an empty HOME and an empty working directory, with
  the kiosk's own model credential and no other, and answers `ok` plus the answer, or
  `error <rc>`. `crewkiosk` can read no chat or broker credential.
- **The answer** goes in the outsider's thread on the originating Mattermost server (§5.9),
  with every `@token` stripped (`strip_mentions`) and `props.crew_kiosk=1`, so `admit` treats
  it as outsider content. Each Q&A is appended to that server's transcript,
  `/var/lib/crewchat/kiosk/<dev>-<server_id>.jsonl`, as `{ts, thread, post_id, asker_id,
  question, answer}` (mode 0600, directory 0700).
- **Answered once.** A question whose `post_id` is already in the transcript is not answered
  again (#97). A `KIOSK` hand-off never moves the doorbell's cursor, so a restarted daemon's
  stream replays every outsider question since the agent's last event. Records written
  before `post_id` was kept still load; they just can't stop a replay.

`Kiosk` consumes the `KioskQueue` from Story 3.3: one worker per expert, started when that
expert's first event arrives, answering its events in order.

**The review** (Story 3.6, R-33). When the switch cache sees social engaged for (crew, server),
`on_social` runs `review_since_cursor(dev, server_id)` for each of that crew's agents
registered on that server. It reads **only that server's** transcript, from the byte offset in
`<dev>-<server_id>.cursor` (`{"offset": N}`, mode 0600, written atomically), and puts one
`{"type": "kiosk.review", "outsider": true, "server": ..., "entries": [...]}` event on the
working agent's doorbell queue. The cursor moves past an event only when the doorbell hands
it to the agent (its `on_handover`, #99), so a review still queued when the daemon stops is
read again after it starts. `on_registered` reviews an agent that registers while its crew's
switch is social, since the cache reports social engaging only once.
The offset already queued is kept in memory, so a review while one is still waiting adds
only what came after it. Only complete lines are read, so a record still being appended
waits for the next review. A transcript shorter than its cursor (replaced) is read from the
start. With no new entries nothing is enqueued. A backlog past `REVIEW_MAX_BYTES` is split
across several such events, so each fits the shim's frame; a full queue stops the review,
and the entries it didn't queue are read again next time. Other servers' transcripts and
cursors are never touched. The shim renders the event's `text` with the outsider prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from crewchat.adapters.base import InboundMessage
from crewchat.doorbell import DoorbellEvent
from crewchat.threads import STATE_DIR
from crewchat.tokens import DIR_MODE, FILE_MODE, write_private_json

if TYPE_CHECKING:
    from crewchat.filters import KioskQueue, SwitchCache

KIOSK_HEADER = (
    "You are this agent's kiosk. Answer questions using only the brief below. "
    "You cannot act, and you cannot share anything that is not in the brief."
)
NO_BRIEF_REPLY = "I have nothing to share yet."
KIOSK_SUBDIR = "kiosk"  # /var/lib/crewchat/kiosk
KIOSK_USER = "crewkiosk"
KIOSK_RUN = "/usr/local/libexec/crewkiosk-run"  # packaging/crewkiosk-run, root-owned
KIOSK_SOCKET = "/run/crewkiosk/kiosk.sock"  # packaging/crewkiosk.socket (D-107)
# What packaging/crewkiosk-run runs; test_kiosk_runner holds the wrapper to exactly this,
# followed by `--model <pin>` from /etc/crewkiosk/model.conf (#116).
CLAUDE_FLAGS = (
    "-p",
    "--restricted",
    "--strict-mcp-config",
    "--tools",
    "",
    "--disallowedTools",
    "mcp__*",
)
RUNNER_TIMEOUT_S = 120.0
MAX_ANSWER_CHARS = 16_000  # under Mattermost's 16383-character post limit
MAX_OUTPUT_BYTES = 256 * 1024
MATTERMOST_PREFIX = "mattermost:"
# `@name`, `@name.crew`, `@channel`: dots inside a token, never a trailing full stop.
MENTION_RE = re.compile(r"@[\w\-]+(?:\.[\w\-]+)*")
SAFE_PART_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
REVIEW_TYPE = "kiosk.review"
# The review's text; the shim prefixes it `[from outside the crew]` (outsider content).
REVIEW_TEXT = (
    "Kiosk review: review these exchanges and raise anything notable to your human partner."
)
REVIEW_FIELDS = ("ts", "thread", "asker_id", "question", "answer")
# One review event's entries, encoded: well inside the shim's 1 MiB reply frame.
REVIEW_MAX_BYTES = 256 * 1024
# A transcript's head (#110): this many hex digits of the digest of its first record.
HEAD_HEX = 16
Deliver = Callable[[str, DoorbellEvent], bool]
# A transcript line read for review: its entry (None when malformed), and the offset past it.
ReviewLine = tuple[dict[str, Any] | None, int]

log = logging.getLogger("crewchat.kiosk")


class BriefStore(dict[str, str]):
    """The latest `## Kiosk` brief per Dev-Name, as the shim pushed it (R-32)."""

    def brief(self, dev_name: str) -> str:
        return self.get(dev_name, "").strip()


def strip_mentions(text: str) -> str:
    """`text` with every `@token` removed, so a kiosk post can mention nobody (§5.11)."""
    out = MENTION_RE.sub("", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return "\n".join(line.strip() for line in out.splitlines()).strip()


# -- runners --------------------------------------------------------------------------------
class RunnerError(Exception):
    """The runner gave no answer."""


class Runner(Protocol):
    async def answer(self, prompt: str) -> str: ...


def parse_reply(reply: bytes) -> str:
    """The wrapper's framed reply: `ok\\n<answer>`, or `error <rc>\\n`. Anything else, an
    empty reply included (the wrapper refused before running the CLI), is a `RunnerError`;
    its reason is in the `crewkiosk@` instance's journal, never on the socket."""
    head, _, body = reply.partition(b"\n")
    if head == b"ok":
        return body[:MAX_OUTPUT_BYTES].decode(errors="replace").strip()
    if head.startswith(b"error "):
        status = head.removeprefix(b"error ").decode(errors="replace")[:16]
        raise RunnerError(f"the kiosk runner exited {status} (see journalctl -u 'crewkiosk@*')")
    raise RunnerError("the kiosk runner gave no answer (see journalctl -u 'crewkiosk@*')")


class ClaudeCliRunner:
    """`claude -p` as `crewkiosk`, through the kiosk socket (D-096, D-107).

    The daemon runs with `NoNewPrivileges=yes` and never changes user: it connects to the
    socket, and systemd starts the wrapper as `crewkiosk` for that one connection. The daemon
    sends only the prompt; the wrapper, not the daemon, holds the CLI flags, the empty HOME
    and working directory, and the model credential. A socket that is missing or refuses, or
    an answer that takes longer than `timeout_s`, is a `RunnerError`: the kiosk logs it and
    posts nothing."""

    def __init__(self, *, socket: str = KIOSK_SOCKET, timeout_s: float = RUNNER_TIMEOUT_S):
        self.socket = socket
        self.timeout_s = timeout_s

    async def answer(self, prompt: str) -> str:
        try:
            return parse_reply(await asyncio.wait_for(self._exchange(prompt), self.timeout_s))
        except TimeoutError:
            raise RunnerError(f"the kiosk runner timed out after {self.timeout_s:.0f}s") from None

    async def _exchange(self, prompt: str) -> bytes:
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket)
        except OSError as exc:
            raise RunnerError(f"the kiosk runner is unavailable at {self.socket}: {exc}") from exc
        try:
            writer.write(prompt.encode())
            await writer.drain()
            writer.write_eof()  # the prompt ends here; the CLI reads stdin to EOF
            reply = bytearray()
            while len(reply) <= MAX_OUTPUT_BYTES:  # the wrapper closes after its reply
                chunk = await reader.read(65536)
                if not chunk:
                    break
                reply += chunk
            return bytes(reply)
        except OSError as exc:
            raise RunnerError(f"the kiosk runner dropped the connection: {exc}") from exc
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


# -- posting --------------------------------------------------------------------------------
@runtime_checkable
class KioskPoster(Protocol):
    """A backend that can post a kiosk answer, `props.crew_kiosk=1` (Mattermost, §5.9), and
    read the thread it answers in."""

    async def send_kiosk(
        self, dev_name: str, channel: str, text: str, thread: str
    ) -> InboundMessage: ...

    async def thread(self, dev_name: str, post_id: str) -> list[InboundMessage]: ...


# -- the session ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Turn:
    ts: float
    who: str  # "outsider" or "kiosk"
    text: str


class KioskSession:
    """One expert's kiosk on one server (R-31): every outsider there, one transcript."""

    def __init__(
        self,
        dev_name: str,
        server_id: str,
        runner: Runner,
        *,
        briefs: BriefStore,
        kiosk_dir: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not SAFE_PART_RE.fullmatch(dev_name) or not SAFE_PART_RE.fullmatch(server_id):
            raise ValueError(f"unsafe kiosk session key ({dev_name!r}, {server_id!r})")
        self.dev_name = dev_name
        self.server_id = server_id
        self.runner = runner
        self.briefs = briefs
        self.clock = clock
        self.transcript = kiosk_dir / f"{dev_name}-{server_id}.jsonl"
        # (thread, asker_id) -> the kiosk's earlier answers to that asker in that thread.
        self._answers: dict[tuple[str, str], list[Turn]] = {}
        # The post IDs of the questions already answered here, so a replay isn't (#97).
        self.answered: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            lines = self.transcript.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.error("kiosk transcript %s unreadable: %s", self.transcript, exc)
            return
        for line in lines:
            try:
                rec = json.loads(line)
                key = (str(rec["thread"]), str(rec["asker_id"]))
                turn = Turn(float(rec["ts"]), "kiosk", str(rec["answer"]))
            except (ValueError, KeyError, TypeError):
                continue
            self._answers.setdefault(key, []).append(turn)
            if isinstance(rec.get("post_id"), str):
                self.answered.add(rec["post_id"])

    def history(self, msg: InboundMessage, thread_posts: Sequence[InboundMessage]) -> list[Turn]:
        """The asker's own posts in the thread and the kiosk's answers to that asker; nothing
        else. The question itself is the last of the asker's posts."""
        thread = msg.root_id or msg.post_id
        asker = msg.author_id
        turns: list[Turn] = []
        seen: set[str] = set()
        for post in thread_posts:
            if post.author_id != asker or post.kiosk or post.post_id in seen:
                continue
            if post.post_id != thread and post.root_id != thread:
                continue
            seen.add(post.post_id)
            turns.append(Turn(post.ts, "outsider", post.text))
        if msg.post_id not in seen:
            turns.append(Turn(msg.ts, "outsider", msg.text))
        turns.extend(self._answers.get((thread, asker), []))
        turns.sort(key=lambda t: t.ts)
        return turns

    def prompt(self, brief: str, history: Sequence[Turn]) -> str:
        convo = "\n\n".join(
            f"{'Outsider' if t.who == 'outsider' else 'Kiosk'}: {t.text}" for t in history
        )
        return (
            f"{KIOSK_HEADER}\n\n<brief>\n{brief}\n</brief>\n\n"
            f"<conversation>\n{convo}\n</conversation>\n\n"
            "Answer the outsider's last message."
        )

    async def ask(self, msg: InboundMessage, thread_posts: Sequence[InboundMessage]) -> str:
        """The answer to post, mentions stripped. No brief: the fixed reply, no runner call."""
        brief = self.briefs.brief(self.dev_name)
        if not brief:
            return NO_BRIEF_REPLY
        raw = await self.runner.answer(self.prompt(brief, self.history(msg, thread_posts)))
        answer = strip_mentions(raw)[:MAX_ANSWER_CHARS]
        if not answer:
            raise RunnerError("the kiosk runner gave an empty answer")
        return answer

    def record(self, msg: InboundMessage, answer: str, ts: float = 0.0) -> None:
        """Append the Q&A to this server's transcript, and remember the answer. `ts` is the
        answer post's own time when the server gave one, so history orders it against the
        thread's posts on one clock."""
        thread = msg.root_id or msg.post_id
        ts = ts or self.clock()
        rec = {
            "ts": ts,
            "thread": thread,
            "post_id": msg.post_id,
            "asker_id": msg.author_id,
            "question": msg.text,
            "answer": answer,
        }
        self.transcript.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        fd = os.open(self.transcript, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        self._answers.setdefault((thread, msg.author_id), []).append(Turn(ts, "kiosk", answer))
        self.answered.add(msg.post_id)


# -- the consumer ---------------------------------------------------------------------------
class Kiosk:
    """Consumes the `KIOSK` holding queue: answers each outsider in their own thread."""

    def __init__(
        self,
        queue: KioskQueue,
        briefs: BriefStore,
        backends: Mapping[str, Any],
        runner: Runner,
        *,
        state_dir: Path = STATE_DIR,
        clock: Callable[[], float] = time.time,
        deliver: Deliver | None = None,
    ) -> None:
        self.queue = queue
        self.briefs = briefs
        self.backends = backends
        self.runner = runner
        self.kiosk_dir = state_dir / KIOSK_SUBDIR
        self.clock = clock
        # Puts a review on the working agent's doorbell queue (`Doorbell.deliver`, R-33).
        self.deliver = deliver
        self.sessions: dict[tuple[str, str], KioskSession] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._started = False
        # (dev, server) -> the transcript offset already queued for review but maybe not
        # yet handed over (#99). In memory only: after a restart the cursor on disk rules.
        self._queued: dict[tuple[str, str], int] = {}
        # (dev, server) -> the head of the transcript last reviewed (#110). A handover of a
        # review from an older head leaves the cursor alone.
        self._heads: dict[tuple[str, str], str] = {}

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        """Watch the queue: a worker per expert, from that expert's first event."""
        self._started = True
        self.queue.listeners.append(self._wake)
        for dev_name in self.queue.pending():
            self._wake(dev_name)

    def _wake(self, dev_name: str) -> None:
        task = self._workers.get(dev_name)
        if task is None or task.done():
            self._workers[dev_name] = asyncio.get_running_loop().create_task(
                self._work(dev_name), name=f"kiosk-{dev_name}"
            )

    async def aclose(self) -> None:
        if self._wake in self.queue.listeners:
            self.queue.listeners.remove(self._wake)
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()

    async def _work(self, dev_name: str) -> None:
        while True:
            event = await self.queue.get(dev_name)
            try:
                await self.handle(dev_name, event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("kiosk: %s could not answer an outsider", dev_name)

    # -- one event --------------------------------------------------------------------------
    def session(self, dev_name: str, server_id: str) -> KioskSession:
        """The (expert, server) session, started on first use (R-31)."""
        key = (dev_name, server_id)
        sess = self.sessions.get(key)
        if sess is None:
            sess = KioskSession(
                dev_name,
                server_id,
                self.runner,
                briefs=self.briefs,
                kiosk_dir=self.kiosk_dir,
                clock=self.clock,
            )
            self.sessions[key] = sess
            log.info("kiosk: started %s on %s", dev_name, server_id)
        return sess

    async def handle(self, dev_name: str, event: DoorbellEvent) -> InboundMessage | None:
        """Answer one queued outsider message; the post made, or None when nothing was."""
        msg = event.message
        if msg is None or not msg.backend.startswith(MATTERMOST_PREFIX):
            log.error("kiosk: %s got a non-Mattermost event from %s", dev_name, event.backend)
            return None
        backend = self.backends.get(msg.backend)
        if not isinstance(backend, KioskPoster):
            log.error("kiosk: %s cannot post a kiosk answer", msg.backend)
            return None
        server_id = msg.backend[len(MATTERMOST_PREFIX) :]
        sess = self.session(dev_name, server_id)
        if msg.post_id in sess.answered:
            log.info("kiosk: %s already answered %s on %s", dev_name, msg.post_id, server_id)
            return None
        thread = msg.root_id or msg.post_id
        posts: list[InboundMessage] = []
        if self.briefs.brief(dev_name):
            try:
                posts = await backend.thread(dev_name, thread)
            except Exception as exc:  # the question itself still goes in
                log.warning("kiosk: thread %s unreadable for %s: %s", thread, dev_name, exc)
        answer = await sess.ask(msg, posts)
        posted: InboundMessage = await backend.send_kiosk(dev_name, msg.channel, answer, thread)
        sess.record(msg, answer, posted.ts)
        return posted

    # -- the review on social engage (R-33) -------------------------------------------------
    def review_paths(self, dev_name: str, server_id: str) -> tuple[Path, Path]:
        """That server's transcript and its review cursor."""
        if not SAFE_PART_RE.fullmatch(dev_name) or not SAFE_PART_RE.fullmatch(server_id):
            raise ValueError(f"unsafe kiosk review key ({dev_name!r}, {server_id!r})")
        base = f"{dev_name}-{server_id}"
        return self.kiosk_dir / f"{base}.jsonl", self.kiosk_dir / f"{base}.cursor"

    def read_cursor(self, dev_name: str, server_id: str) -> tuple[int, str | None]:
        """The byte offset reviewed up to, and the head of the transcript it is an offset into
        (#110): `(0, None)` when there is no cursor, or it is unreadable. A cursor written
        before #110 has no head."""
        _, path = self.review_paths(dev_name, server_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0, None
        except (OSError, ValueError) as exc:
            log.error("kiosk review cursor %s unreadable, reviewing from the start: %s", path, exc)
            return 0, None
        offset = raw.get("offset") if isinstance(raw, dict) else None
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            log.error("kiosk review cursor %s holds no offset, reviewing from the start", path)
            return 0, None
        head = raw.get("head")
        return offset, head if isinstance(head, str) and head else None

    def _write_cursor(self, path: Path, offset: int, head: str | None) -> None:
        path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        state: dict[str, Any] = {"offset": offset}
        if head is not None:
            state["head"] = head
        write_private_json(path, state)

    @staticmethod
    def _head(first_line: bytes) -> str | None:
        """A transcript's generation (#110): a digest of its first record, which names the
        answer's own post, so no two transcripts share one. None until that line is complete:
        a head taken from a line mid-append would change once it was, and reset the review."""
        if not first_line.endswith(b"\n"):
            return None
        return hashlib.sha256(first_line).hexdigest()[:HEAD_HEX]

    @staticmethod
    def _new_lines(
        transcript: Path, offset: int, cursor_head: str | None
    ) -> tuple[list[ReviewLine], int, str | None]:
        """Each complete transcript line past `offset`, parsed (None when malformed), with the
        offset just past it; the offset reading started from; and the transcript's head. A
        replaced transcript is read from 0: one shorter than the cursor, or one whose head
        isn't the cursor's (#110), though it may have grown past the old offset."""
        try:
            with transcript.open("rb") as fh:
                head = Kiosk._head(fh.readline())
                if os.fstat(fh.fileno()).st_size < offset:
                    log.warning("kiosk transcript %s is shorter than its cursor", transcript)
                    offset = 0
                elif head is not None and cursor_head is not None and head != cursor_head:
                    log.warning("kiosk transcript %s was replaced since its cursor", transcript)
                    offset = 0
                fh.seek(offset)
                data = fh.read()
        except FileNotFoundError:
            return [], offset, None
        complete = data[: data.rfind(b"\n") + 1]  # a record mid-append waits for next time
        lines: list[ReviewLine] = []
        pos = offset
        for line in complete.splitlines(keepends=True):
            pos += len(line)
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                log.warning("kiosk transcript %s: skipped a malformed line", transcript)
                lines.append((None, pos))
                continue
            lines.append(({k: rec[k] for k in REVIEW_FIELDS if k in rec}, pos))
        return lines, offset, head

    def review_since_cursor(self, dev_name: str, server_id: str) -> int:
        """Enqueue `dev_name`'s kiosk exchanges on `server_id` since that server's cursor (or
        since what is already queued) for the working agent, as `kiosk.review` outsider
        events. Each event moves the cursor past its entries when it is handed over (#99).
        The number of entries enqueued."""
        if self.deliver is None:
            log.error("kiosk review: no doorbell to deliver to; %s's cursor kept", dev_name)
            return 0
        transcript, cursor = self.review_paths(dev_name, server_id)
        key = (dev_name, server_id)
        on_disk, cursor_head = self.read_cursor(dev_name, server_id)
        # What is queued is from the generation last seen; the cursor's head may be older.
        cursor_head = self._heads.get(key) or cursor_head
        offset = max(on_disk, self._queued.get(key, on_disk))
        lines, start, head = self._new_lines(transcript, offset, cursor_head)
        if start != offset and key in self._queued:
            del self._queued[key]  # an old transcript's offset: its handover won't write (below)
        if head is not None:
            self._heads[key] = head
        batches: list[tuple[list[dict[str, Any]], int]] = []
        chunk: list[dict[str, Any]] = []
        size, end = 0, start
        for entry, pos in lines:
            if entry is not None:
                n = len(json.dumps(entry))
                if chunk and size + n > REVIEW_MAX_BYTES:
                    batches.append((chunk, end))
                    chunk, size = [], 0
                chunk.append(entry)
                size += n
            end = pos
        batches.append((chunk, end))
        backend = f"{MATTERMOST_PREFIX}{server_id}"
        delivered = 0
        for entries, upto in batches:
            if not entries:
                # Nothing but malformed lines: move past them now, unless a review is still
                # waiting, whose handover must not be overtaken.
                if upto != offset and key not in self._queued:
                    self._write_cursor(cursor, upto, head)
                continue
            payload = {
                "type": REVIEW_TYPE,
                "backend": backend,
                "outsider": True,
                "server": server_id,
                "text": REVIEW_TEXT,
                "entries": entries,
            }
            event = DoorbellEvent(
                backend,
                payload,
                ts=self.clock(),
                on_handover=partial(self._handed_over, key, cursor, upto, head),
                # The transcript's head as well as the offset: a replaced transcript starts
                # its offsets again, and a repeat ID is dropped by the one-shot (#109, #110).
                nonce=f"{REVIEW_TYPE}:{head}:{upto}",
            )
            before = self._queued.get(key)
            self._queued[key] = upto  # before `deliver`: a handover may run from inside it
            if not self.deliver(dev_name, event):
                if before is None:
                    self._queued.pop(key, None)
                else:
                    self._queued[key] = before
                log.error("kiosk review for %s on %s not delivered", dev_name, server_id)
                break
            delivered += len(entries)
        if delivered:
            log.info("kiosk review: %d exchanges for %s on %s", delivered, dev_name, server_id)
        return delivered

    def _handed_over(self, key: tuple[str, str], cursor: Path, upto: int, head: str | None) -> None:
        """A review event reached the agent: its cursor moves past the event's entries. A
        review of a transcript since replaced leaves the cursor alone: its offset means
        nothing in the new one (#110)."""
        if self._heads.get(key, head) != head:
            return
        self._write_cursor(cursor, upto, head)
        if self._queued.get(key) == upto:
            del self._queued[key]  # nothing queued beyond it

    def on_registered(self, reg: Any, cache: SwitchCache) -> int:
        """An agent registered (#99): on each of its Mattermost servers where its crew's
        switch is social now, review its kiosk there. The switch cache reports social
        engaging only once, often before any agent has registered after a daemon start, so
        this is what reaches an agent registering while social is on (R-33)."""
        total = 0
        for backend in reg.backends:
            if not backend.startswith(MATTERMOST_PREFIX):
                continue
            server_id = backend[len(MATTERMOST_PREFIX) :]
            if cache.mode(reg.crew, server_id) == "social":
                total += self.on_social(server_id, [reg.dev_name])
        return total

    def on_social(self, server_id: str, dev_names: Iterable[str]) -> int:
        """Social engaged on `server_id`: review each agent's kiosk there (R-33)."""
        total = 0
        for dev_name in dev_names:
            try:
                total += self.review_since_cursor(dev_name, server_id)
            except Exception:
                log.exception("kiosk review for %s on %s failed", dev_name, server_id)
        return total
