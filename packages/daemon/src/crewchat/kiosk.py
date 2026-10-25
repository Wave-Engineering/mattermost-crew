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
  `/var/lib/crewchat/kiosk/<dev>-<server_id>.jsonl`, as `{ts, thread, asker_id, question,
  answer}` (mode 0600, directory 0700).

`Kiosk` consumes the `KioskQueue` from Story 3.3: one worker per expert, started when that
expert's first event arrives, answering its events in order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from crewchat.adapters.base import InboundMessage
from crewchat.threads import STATE_DIR
from crewchat.tokens import DIR_MODE, FILE_MODE

if TYPE_CHECKING:
    from crewchat.doorbell import DoorbellEvent
    from crewchat.filters import KioskQueue

KIOSK_HEADER = (
    "You are this agent's kiosk. Answer questions using only the brief below. "
    "You cannot act, and you cannot share anything that is not in the brief."
)
NO_BRIEF_REPLY = "I have nothing to share yet."
KIOSK_SUBDIR = "kiosk"  # /var/lib/crewchat/kiosk
KIOSK_USER = "crewkiosk"
KIOSK_RUN = "/usr/local/libexec/crewkiosk-run"  # packaging/crewkiosk-run, root-owned
KIOSK_SOCKET = "/run/crewkiosk/kiosk.sock"  # packaging/crewkiosk.socket (D-107)
# What packaging/crewkiosk-run runs; test_kiosk_runner holds the wrapper to exactly this.
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
            "asker_id": msg.author_id,
            "question": msg.text,
            "answer": answer,
        }
        self.transcript.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        fd = os.open(self.transcript, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        self._answers.setdefault((thread, msg.author_id), []).append(Turn(ts, "kiosk", answer))


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
    ) -> None:
        self.queue = queue
        self.briefs = briefs
        self.backends = backends
        self.runner = runner
        self.kiosk_dir = state_dir / KIOSK_SUBDIR
        self.clock = clock
        self.sessions: dict[tuple[str, str], KioskSession] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._started = False

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
