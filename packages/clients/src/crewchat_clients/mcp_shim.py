"""The thin MCP shim: the `chat_*` tools over the crewchat daemon's socket (Dev Spec §5.5;
Stories 2.4, 2.4c; R-09 to R-13, R-15).

The shim is a stdio MCP server that runs in the agent's session, as the agent's user. It holds
**no credentials** (R-08): every chat tool forwards one frame to `/run/crewchat/crewchat.sock`,
and the daemon acts on the agent's behalf with what it holds itself.

- `chat_send`, `chat_read`, `chat_thread`, `chat_dm`, `chat_react` and `chat_health` each
  forward one frame. `chat_set_default(key, backend)` is the one local-only tool: it writes
  `~/.claude/chat-defaults.json` and sends nothing to the daemon.
- **Default backend (R-11).** The shim resolves it (`crewchat_clients.defaults`) and always
  sends the resolved `backend` in the frame; the daemon does no resolution. With no default,
  an interactive session gets a structured question for its human, answered through
  `chat_set_default`. A headless session's send fails loudly, and the shim runs
  `sqtk add <needs-list> "<question>"` with its own text, never message content. There is no
  Discord fallback.
- **Off-default (R-13).** A call that names a backend other than the default is sent with
  `off_default: true`, and the daemon logs `off-default: <dev> <backend>`.
- **Replies (R-12).** The shim keeps no map of posts to backends. A `chat_send` with
  `thread` (and `chat_thread`, `chat_react`) that names no backend asks the daemon
  (`thread_backend`), so the reply goes to that thread's backend whatever the default is, and
  across a restart of either side. A post the daemon can't place fails loudly; it never falls
  back to the default.
- **Backend names.** One form, as `register` lists them and every frame carries them:
  `mattermost:<server-id>` and `discord` (§5.4).
- **Attachments and embeds (R-15).** `chat_send` and `chat_dm` take `attach_path` and `embed`
  (`title:body`). The shim reads the file as the agent's user and streams it to the daemon in
  `upload` chunks that each fit a frame, then names the upload in the post's `attachments`. A
  file over 8 MiB, or one the daemon refuses, fails the call loudly before anything is posted.
- **Register.** At start, `register{dev_name, dev_team, dev_avatar, crew, session_id}` from the
  identity file (`crew` omitted when the file has none). `dev_team` and `dev_avatar` fall back
  to the legacy `/tmp/claude-agent-<md5(repo_root)>.json`, then to `""` (D-103). It
  registers again every 24 hours, and before any call when the last one is older than that,
  so a long-lived session never ages out (R-30). A daemon that answers
  `not_registered` (it restarted) gets a fresh `register` and the call is retried once.
- **Pushes (§5.3).** The daemon never reads the agent's files, so the shim pushes what it
  needs: `kiosk_brief{dev_name, text}` with only the seed's `## Kiosk` section (R-32), at start
  and whenever the seed's mtime changes, and `squawk_ids{dev_name, ids}` from the agent's
  squawk records, at start and whenever they change.
- **Aliases (R-15).** Every `disc_*` tool in today's kit takes today's args and maps to
  `backend="discord"`: `disc_send` (with `attach_path` and `embed`) and `disc_read` (with
  `message_id`) to `chat_send` and `chat_read`; `disc_list`, `disc_resolve`,
  `disc_create_channel` and `disc_create_thread` to the daemon's Discord ops. `disc_thread`,
  `disc_dm` and `disc_react` stay from Story 2.4. Each logs one deprecation warning per
  session.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from crewchat_clients.brief import read_brief
from crewchat_clients.defaults import (
    Backend,
    DefaultsError,
    NeedsHuman,
    RemoteFor,
    defaults_path,
    git_remote,
    is_backend,
    resolve_default,
    write_default,
)

SOCKET_PATH = Path("/run/crewchat/crewchat.sock")
PROTO = 1
MAX_FRAME_BYTES = 1024 * 1024
CALL_TIMEOUT_S = 30.0
REGISTER_EVERY = timedelta(hours=24)
POLL_INTERVAL_S = 5.0
DISCORD = "discord"
# A Discord snowflake, used with `fullmatch` (#62): the one shape a `<channel>/<message>` part
# may have, since it ends up in a REST path that carries the bot credential.
SNOWFLAKE_RE = re.compile(r"[0-9]{1,20}")
DISC_READ_LIMIT = 20  # today's disc_read default
# An attachment goes to the daemon in `upload` chunks (§5.4). The daemon's frames are at most
# 64 KiB; 45 KiB of raw bytes is 60 KiB of base64, leaving room for the frame around it.
UPLOAD_CHUNK_BYTES = 45 * 1024
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # the daemon's cap on one upload
MAX_UPLOAD_NAME = 255
IDENTITY_FILE = Path(".claude/agent-identity.json")
# The legacy identity file lives here as `claude-agent-<md5(repo_root)>.json` (transition).
LEGACY_IDENTITY_DIR = Path("/tmp")
SEED_FILE = Path(".claude/reseed-revive.md")
SQUAWKS_FILE = Path(".claude/sqtk-mine.json")
# CLAUDE_CODE_ENTRYPOINT values of a session with a human at the keyboard.
INTERACTIVE_ENTRYPOINTS = frozenset({"cli", "claude-vscode", "claude-desktop", "claude-jetbrains"})
OUTSIDER_PREFIX = "[from outside the crew]"

log = logging.getLogger("crewchat_clients.mcp_shim")

Clock = Callable[[], datetime]
Transport = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
Squawker = Callable[[Sequence[str], Path], Awaitable[int]]


def utc_now() -> datetime:
    return datetime.now(UTC)


def mark_outsiders(result: dict[str, Any]) -> dict[str, Any]:
    """Prefix the text of each message the daemon marked `"outsider": true` (Story 3.3, R-23)
    with `OUTSIDER_PREFIX`, so the agent never reads outsider content as its crew's."""
    messages = result.get("messages")
    if not isinstance(messages, list):
        return result
    marked: list[Any] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("outsider") is True:
            msg = {**msg, "text": f"{OUTSIDER_PREFIX} {msg.get('text', '')}"}
        marked.append(msg)
    return {**result, "messages": marked}


def render_event(event: dict[str, Any]) -> dict[str, Any]:
    """A doorbell event as the agent reads it: an event the daemon marked `"outsider": true`
    (the kiosk review, Story 3.6, R-33) has its `text` prefixed with `OUTSIDER_PREFIX`."""
    text = event.get("text")
    marked = event.get("outsider") is True and isinstance(text, str)
    if marked and not str(text).startswith(OUTSIDER_PREFIX):
        return {**event, "text": f"{OUTSIDER_PREFIX} {text}"}
    return event


class ShimError(Exception):
    """A tool call that failed; the message goes to the agent as the tool's error."""


def discord_ref(ref: str) -> tuple[str, str] | None:
    """Discord's `<channel>/<message>` form as its two snowflakes; `None` for a bare ID.

    Any other `/`, or a part that isn't a snowflake, is a `ShimError` before any frame is sent:
    the parts reach REST paths that carry the bot credential (R-08, #62). Mattermost IDs never
    contain a `/`.
    """
    if "/" not in ref:
        return None
    parts = ref.split("/")
    if len(parts) != 2 or not all(SNOWFLAKE_RE.fullmatch(p) for p in parts):
        raise ShimError(f"{ref!r} is not <channel>/<message>: two Discord IDs, digits only")
    return parts[0], parts[1]


def thread_id(ref: str) -> str:
    """The thread to reply on or read: the channel part of `<channel>/<message>` (on Discord,
    the thread channel the message is in), or the bare ID as given."""
    pair = discord_ref(ref)
    return pair[0] if pair else ref


class DaemonError(ShimError):
    """The daemon answered `ok:false`. `code` is the error's first word."""

    def __init__(self, op: str, error: str) -> None:
        self.op = op
        self.error = error
        self.code = error.split(":", 1)[0].strip()
        super().__init__(f"crewchat {op}: {error}")


class DaemonUnreachable(ShimError):
    """The socket could not be reached, or the reply was cut short."""


# -- the socket ---------------------------------------------------------------------------------
class SocketTransport:
    """One frame per connection: connect, write the request line, read the reply line."""

    def __init__(self, path: Path = SOCKET_PATH, timeout_s: float = CALL_TIMEOUT_S) -> None:
        self.path = path
        self.timeout_s = timeout_s

    async def __call__(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        req_id = str(uuid.uuid4())
        frame = {"proto": PROTO, "id": req_id, "op": op, "args": args}
        try:
            async with asyncio.timeout(self.timeout_s):
                reader, writer = await asyncio.open_unix_connection(
                    str(self.path), limit=MAX_FRAME_BYTES
                )
                try:
                    writer.write(json.dumps(frame).encode() + b"\n")
                    await writer.drain()
                    line = await reader.readline()
                finally:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
        except (OSError, TimeoutError, ValueError) as exc:
            raise DaemonUnreachable(f"crewchat daemon at {self.path}: {exc!r}") from exc
        if not line:
            raise DaemonUnreachable(f"crewchat daemon at {self.path} closed without a reply")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise DaemonUnreachable(f"crewchat daemon sent a malformed reply to {op}") from exc
        if not isinstance(reply, dict) or reply.get("id") != req_id:
            raise DaemonUnreachable(f"crewchat daemon sent a mismatched reply to {op}")
        if not reply.get("ok"):
            raise DaemonError(op, str(reply.get("error", "error")))
        result = reply.get("result")
        return result if isinstance(result, dict) else {}


# -- the session's kind -------------------------------------------------------------------------
def parent_cmdline(pid: int | None = None) -> list[str]:
    """The parent process's argv, from /proc; [] where it can't be read."""
    pid = os.getppid() if pid is None else pid
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def is_headless(environ: Mapping[str, str], cmdline: Sequence[str]) -> bool:
    """True unless the session is an interactive one: `CLAUDE_CODE_ENTRYPOINT` names an
    interactive entry point and the session was not started with `-p` / `--print`."""
    if any(arg in ("-p", "--print") or arg.startswith("--print=") for arg in cmdline):
        return True
    return environ.get("CLAUDE_CODE_ENTRYPOINT", "") not in INTERACTIVE_ENTRYPOINTS


async def run_sqtk(argv: Sequence[str], cwd: Path) -> int:
    """Run `sqtk` with `argv`; its exit code (127 when it isn't installed)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "sqtk",
            *argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        log.error("sqtk could not run: %s", exc)
        return 127
    _, err = await proc.communicate()
    if proc.returncode:
        log.error("sqtk %s exited %s: %s", argv[0] if argv else "", proc.returncode, err.decode())
    return proc.returncode or 0


# -- the shim -----------------------------------------------------------------------------------
@dataclass
class _FileState:
    """What the shim last pushed for one of the agent's files."""

    mtime_ns: int | None = None
    value: Any = None
    pushed: bool = False


def file_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def read_squawk_ids(path: Path) -> list[str]:
    """The ids in the agent's squawk records: a JSON array of `{"id": ...}` (or bare ids)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        value = item.get("id") if isinstance(item, dict) else item
        valid = isinstance(value, int | str) and not isinstance(value, bool) and value != ""
        if valid and str(value) not in ids:
            ids.append(str(value))
    return ids


class Shim:
    """The tools' behaviour, apart from the MCP plumbing (which `build_server` adds)."""

    def __init__(
        self,
        repo_root: Path,
        transport: Transport,
        *,
        clock: Clock = utc_now,
        headless: bool | None = None,
        defaults_file: Path | None = None,
        squawk: Squawker = run_sqtk,
        remote_for: RemoteFor = git_remote,
        session_id: str | None = None,
        environ: Mapping[str, str] | None = None,
        legacy_dir: Path = LEGACY_IDENTITY_DIR,
    ) -> None:
        env = os.environ if environ is None else environ
        self.repo_root = repo_root
        self.transport = transport
        self.clock = clock
        self.headless = is_headless(env, parent_cmdline()) if headless is None else headless
        self.defaults_file = defaults_file or defaults_path()
        self.squawk = squawk
        self.remote_for = remote_for
        self.session_id = session_id or env.get("CLAUDE_CODE_SESSION_ID") or str(uuid.uuid4())
        self.legacy_dir = legacy_dir
        self.identity = self._load_identity()
        self.legacy_identity = self._load_legacy_identity()
        dev_name = self.identity.get("dev_name")
        if not isinstance(dev_name, str) or not dev_name:
            raise ShimError(f"{repo_root / IDENTITY_FILE} names no dev_name; run /name first")
        self.dev_name: str = dev_name
        self.registered_at: datetime | None = None
        self.registration: dict[str, Any] = {}
        self._brief = _FileState()
        self._squawks = _FileState()
        self._warned: set[str] = set()
        self._squawked: set[str] = set()
        self._lock = asyncio.Lock()

    # -- the agent's files ----------------------------------------------------------------------
    def _load_identity(self) -> dict[str, Any]:
        path = self.repo_root / IDENTITY_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ShimError(f"cannot read {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ShimError(f"{path} is not a JSON object")
        return raw

    def legacy_identity_path(self) -> Path:
        """`/tmp/claude-agent-<md5(repo_root)>.json`, the md5 of the absolute repo-root path
        string, as `/name` keys it."""
        digest = hashlib.md5(str(self.repo_root.absolute()).encode(), usedforsecurity=False)
        return self.legacy_dir / f"claude-agent-{digest.hexdigest()}.json"

    def _load_legacy_identity(self) -> dict[str, Any]:
        """The legacy identity file, or `{}` when it is absent or unreadable."""
        try:
            raw = json.loads(self.legacy_identity_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def identity_field(self, key: str) -> str:
        """`key` from the identity file, then the legacy `/tmp` file, then `""` (D-103)."""
        for source in (self.identity, self.legacy_identity):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @property
    def needs_list(self) -> str | None:
        value = self.registration.get("needs_list")
        return value if isinstance(value, str) and value else None

    # -- register -------------------------------------------------------------------------------
    async def register(self) -> dict[str, Any]:
        args: dict[str, Any] = {
            "dev_name": self.dev_name,
            "session_id": self.session_id,
            "dev_team": self.identity_field("dev_team"),
            "dev_avatar": self.identity_field("dev_avatar"),
        }
        crew = self.identity.get("crew")
        if isinstance(crew, str) and crew:
            args["crew"] = crew
        result = await self.transport("register", args)
        self.registration = result
        self.registered_at = self.clock()
        log.info("registered %s: %s", result.get("account"), ", ".join(self.backends()))
        return result

    def backends(self) -> list[str]:
        """The agent's backends, as `register` lists them: the one form on the socket
        (`mattermost:<server-id>`, `discord`; §5.4). Nothing is translated."""
        value = self.registration.get("backends")
        return [b for b in value if isinstance(b, str)] if isinstance(value, list) else []

    def register_due(self) -> bool:
        return self.registered_at is None or self.clock() - self.registered_at >= REGISTER_EVERY

    async def ensure_registered(self) -> None:
        if self.register_due():
            await self.register()

    # -- pushes ---------------------------------------------------------------------------------
    async def sync_files(self, *, force: bool = False) -> None:
        """Push the brief when the seed's mtime changed, and the squawk ids when they changed."""
        seed = self.repo_root / SEED_FILE
        mtime = file_mtime(seed)
        if force or not self._brief.pushed or mtime != self._brief.mtime_ns:
            text = read_brief(seed)
            await self.transport("kiosk_brief", {"dev_name": self.dev_name, "text": text})
            self._brief = _FileState(mtime_ns=mtime, value=text, pushed=True)

        squawks = self.repo_root / SQUAWKS_FILE
        mtime = file_mtime(squawks)
        if force or not self._squawks.pushed or mtime != self._squawks.mtime_ns:
            ids = read_squawk_ids(squawks)
            if force or not self._squawks.pushed or ids != self._squawks.value:
                await self.transport("squawk_ids", {"dev_name": self.dev_name, "ids": ids})
            self._squawks = _FileState(mtime_ns=mtime, value=ids, pushed=True)

    async def start(self) -> None:
        """Register and push; a daemon that isn't up yet is retried before the first call."""
        try:
            await self.register()
            await self.sync_files(force=True)
        except ShimError as exc:
            log.error("start: %s (retried before the next call)", exc)

    async def tick(self) -> None:
        """The timer's work: the daily re-register, and the file pushes."""
        async with self._lock:
            try:
                await self.ensure_registered()
                await self.sync_files()
            except ShimError as exc:
                log.error("tick: %s", exc)

    async def run_timers(
        self,
        *,
        interval_s: float = POLL_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        while True:
            await sleep(interval_s)
            await self.tick()

    # -- calls ----------------------------------------------------------------------------------
    async def _forward(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """One frame to the daemon, after the register and push checks."""
        async with self._lock:
            await self.ensure_registered()
            with contextlib.suppress(ShimError):
                await self.sync_files()
            try:
                return await self.transport(op, args)
            except DaemonError as exc:
                if exc.code != "not_registered":
                    raise
            # The daemon restarted and lost the registration: register, push, retry once.
            await self.register()
            with contextlib.suppress(ShimError):
                await self.sync_files(force=True)
            return await self.transport(op, args)

    async def thread_backend(self, post_id: str) -> Backend:
        """The backend `post_id`'s thread is on, as the daemon answers it (R-12). The shim
        keeps no map of its own, so the answer survives a restart of either side. Unknown is
        a loud failure: a reply never falls back to the default backend."""
        # Discord's `<channel>/<message>` form, both parts checked, is looked up by its message.
        pair = discord_ref(post_id)
        lookup = pair[1] if pair else post_id
        try:
            result = await self._forward(
                "thread_backend", {"dev_name": self.dev_name, "post_id": lookup}
            )
        except DaemonError as exc:
            raise ShimError(
                f"cannot tell which backend {post_id} is on ({exc.error}). Nothing was sent; "
                "a reply never falls back to the default backend. Name the backend."
            ) from exc
        found = result.get("backend")
        if not is_backend(found):
            raise ShimError(f"crewchat thread_backend answered {found!r} for {post_id}")
        return str(found)

    def default(self) -> Backend | NeedsHuman:
        try:
            return resolve_default(
                self.identity, self.repo_root, path=self.defaults_file, remote_for=self.remote_for
            )
        except DefaultsError as exc:
            raise ShimError(f"default backend: {exc}") from exc

    def question(self, need: NeedsHuman) -> str:
        """The R-11 question: the shim's own text, never message content."""
        options = ", ".join(self.backends()) or "mattermost:<server-id> or discord"
        return (
            f"crewchat: agent {self.dev_name} has no default chat backend for {need.key}. "
            f"Which backend should it use? Options: {options}. Record the answer with "
            f'chat_set_default("{need.key}", "<backend>") in its session, or add '
            f'"{need.key}": "<backend>" to ~/.claude/chat-defaults.json.'
        )

    async def needs_human(self, need: NeedsHuman) -> dict[str, Any]:
        """Interactive: the question for the agent to ask. Headless: squawk it and fail loud."""
        text = self.question(need)
        if not self.headless:
            return {
                "needs_human": True,
                "key": need.key,
                "question": text,
                "options": self.backends(),
                "answer_with": "chat_set_default(key, backend)",
            }
        with contextlib.suppress(ShimError):
            await self.ensure_registered()
        needs_list = self.needs_list
        if needs_list is None:
            raise ShimError(
                f"NO DEFAULT CHAT BACKEND for {need.key}, and no needs list to ask on (the "
                "daemon has not answered register). Nothing was sent; there is no fallback."
            )
        squawked = "already squawked this session"
        if need.key not in self._squawked:
            code = await self.squawk(["add", needs_list, text], self.repo_root)
            if code != 0:
                raise ShimError(
                    f"NO DEFAULT CHAT BACKEND for {need.key}. Nothing was sent, and the squawk "
                    f"to {needs_list} FAILED (sqtk exit {code}). There is no fallback."
                )
            self._squawked.add(need.key)
            squawked = "squawked"
        raise ShimError(
            f"NO DEFAULT CHAT BACKEND for {need.key}. Nothing was sent; there is no fallback. "
            f"The question is {squawked} on {needs_list} for the human partner."
        )

    async def _route(
        self, named: str | None, post_id: str | None = None
    ) -> tuple[Backend, bool] | dict[str, Any]:
        """The backend for a call, and whether it is off-default; or the question to ask.

        A named backend is used as named. Otherwise a call on a post (a reply, a thread, a
        reaction) goes where the daemon says that post is (R-12), and any other call goes to
        the default (R-11).
        """
        if named is not None:
            if not is_backend(named):
                raise ShimError(f"backend {named!r} is not mattermost:<server-id> or discord")
            try:
                default = self.default()
            except ShimError:
                default = None
            return named, named != default
        if post_id:
            return await self.thread_backend(post_id), False
        default = self.default()
        if isinstance(default, NeedsHuman):
            return await self.needs_human(default)
        return default, False

    async def _upload(self, attach_path: str) -> str:
        """Stream the agent's file to the daemon (`upload`), as the agent's user; its id.

        The daemon never reads an agent's path (§5.3). The file is checked before any frame:
        a missing, non-regular or over-cap file fails the call loudly, and nothing is sent.
        """
        path = Path(attach_path).expanduser()
        try:
            st = path.stat()
        except OSError as exc:
            raise ShimError(f"attach_path {attach_path}: {exc.strerror or exc}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise ShimError(f"attach_path {attach_path} is not a regular file")
        if st.st_size > MAX_UPLOAD_BYTES:
            raise ShimError(
                f"attach_path {attach_path} is {st.st_size} bytes, over the "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MiB cap. Nothing was sent."
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ShimError(f"attach_path {attach_path}: {exc.strerror or exc}") from exc
        name = path.name[:MAX_UPLOAD_NAME]
        pieces = [data[i : i + UPLOAD_CHUNK_BYTES] for i in range(0, len(data), UPLOAD_CHUNK_BYTES)]
        pieces = pieces or [b""]
        result: dict[str, Any] = {}
        for seq, piece in enumerate(pieces):
            frame = {
                "dev_name": self.dev_name,
                "name": name,
                "seq": seq,
                "data_b64": base64.b64encode(piece).decode("ascii"),
                "last": seq == len(pieces) - 1,
            }
            try:
                result = await self._forward("upload", frame)
            except DaemonError as exc:
                raise ShimError(
                    f"attach_path {attach_path}: the daemon refused it ({exc.error}). "
                    "Nothing was sent."
                ) from exc
        upload_id = result.get("upload_id")
        if not isinstance(upload_id, str) or not upload_id:
            raise ShimError(f"attach_path {attach_path}: the daemon returned no upload id")
        return upload_id

    async def _post_args(
        self, name: Backend, off: bool, attach_path: str | None, embed: str | None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"dev_name": self.dev_name, "backend": name}
        if attach_path:
            args["attachments"] = [await self._upload(attach_path)]
        if embed is not None:
            args["embed"] = embed
        if off:
            args["off_default"] = True
        return args

    # -- the tools ------------------------------------------------------------------------------
    async def chat_send(
        self,
        to: str,
        text: str,
        thread: str | None = None,
        backend: str | None = None,
        attach_path: str | None = None,
        embed: str | None = None,
    ) -> dict[str, Any]:
        # The thread is checked first: `_post_args` sends `upload` frames (#62).
        tid = thread_id(thread) if thread else None
        route = await self._route(backend, thread)
        if isinstance(route, dict):
            return route
        name, off = route
        args = await self._post_args(name, off, attach_path, embed)
        args.update({"to": to, "text": text})
        if tid:
            args["thread"] = tid
        return await self._forward("send", args)

    async def chat_dm(
        self,
        to: str,
        text: str,
        backend: str | None = None,
        attach_path: str | None = None,
        embed: str | None = None,
    ) -> dict[str, Any]:
        route = await self._route(backend)
        if isinstance(route, dict):
            return route
        name, off = route
        args = await self._post_args(name, off, attach_path, embed)
        args.update({"to": to, "text": text})
        return await self._forward("dm", args)

    async def chat_read(
        self,
        channel: str,
        since: float | None = None,
        limit: int | None = None,
        backend: str | None = None,
    ) -> dict[str, Any]:
        return await self._read(channel, since=since, limit=limit, backend=backend)

    async def _read(
        self,
        channel: str,
        *,
        since: float | None = None,
        limit: int | None = None,
        backend: str | None = None,
        post_id: str | None = None,
    ) -> dict[str, Any]:
        route = await self._route(backend)
        if isinstance(route, dict):
            return route
        name, off = route
        args: dict[str, Any] = {"dev_name": self.dev_name, "backend": name, "channel": channel}
        if since is not None:
            args["since"] = since
        if limit is not None:
            args["limit"] = limit
        if post_id is not None:
            args["post_id"] = post_id
        if off:
            args["off_default"] = True
        return mark_outsiders(await self._forward("read", args))

    async def chat_thread(self, post_id: str, backend: str | None = None) -> dict[str, Any]:
        route = await self._route(backend, post_id)
        if isinstance(route, dict):
            return route
        name, off = route
        args: dict[str, Any] = {
            "dev_name": self.dev_name,
            "backend": name,
            "post_id": thread_id(post_id),
        }
        if off:
            args["off_default"] = True
        return mark_outsiders(await self._forward("thread", args))

    async def chat_react(
        self, post_id: str, emoji: str, backend: str | None = None
    ) -> dict[str, Any]:
        discord_ref(post_id)  # a malformed `<channel>/<message>` fails here, before any frame
        route = await self._route(backend, post_id)
        if isinstance(route, dict):
            return route
        name, off = route
        args: dict[str, Any] = {
            "dev_name": self.dev_name,
            "backend": name,
            "post_id": post_id,
            "emoji": emoji,
        }
        if off:
            args["off_default"] = True
        return await self._forward("react", args)

    async def chat_health(self) -> dict[str, Any]:
        return await self._forward("health", {})

    async def chat_set_default(self, key: str, backend: str) -> dict[str, Any]:
        """Local only: record the human's answer in the defaults file. No frame is sent."""
        try:
            write_default(key, backend, self.defaults_file)
        except DefaultsError as exc:
            raise ShimError(str(exc)) from exc
        self._squawked.discard(key)
        return {"key": key, "backend": backend, "file": str(self.defaults_file)}

    # -- the deprecated aliases (R-15) ----------------------------------------------------------
    def deprecated(self, alias: str, target: str) -> None:
        if alias in self._warned:
            return
        self._warned.add(alias)
        log.warning(
            'deprecated: %s is %s(backend="discord"); it will be removed after one kit minor '
            "version",
            alias,
            target,
        )

    async def disc_send(
        self,
        channel_id: str,
        message: str | None = None,
        content: str | None = None,
        attach_path: str | None = None,
        embed: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        self.deprecated("disc_send", "chat_send")
        text = message if message is not None else content
        if not text:
            raise ShimError("disc_send needs message (or content)")
        return await self.chat_send(
            channel_id,
            text,
            thread=thread_id,
            backend=DISCORD,
            attach_path=attach_path,
            embed=embed,
        )

    async def disc_read(
        self, channel_id: str, limit: int = DISC_READ_LIMIT, message_id: str | None = None
    ) -> dict[str, Any]:
        """Today's disc_read: the recent `limit` (default 20), or the one `message_id`."""
        self.deprecated("disc_read", "chat_read")
        if message_id:
            return await self._read(channel_id, backend=DISCORD, post_id=message_id)
        return await self._read(channel_id, limit=limit, backend=DISCORD)

    async def _discord_op(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """A Discord-only op (Story 2.4c), with `backend="discord"`."""
        route = await self._route(DISCORD)
        if isinstance(route, dict):  # pragma: no cover - a named backend never asks
            return route
        name, off = route
        frame: dict[str, Any] = {"dev_name": self.dev_name, "backend": name, **args}
        if off:
            frame["off_default"] = True
        return await self._forward(op, frame)

    async def disc_list(self, guild_id: str, type: str = "text") -> dict[str, Any]:
        self.deprecated("disc_list", "list_channels")
        return await self._discord_op("list_channels", {"guild_id": guild_id, "type": type})

    async def disc_resolve(self, guild_id: str, name: str, type: str = "channel") -> dict[str, Any]:
        self.deprecated("disc_resolve", "resolve")
        return await self._discord_op(
            "resolve", {"guild_id": guild_id, "name": name, "type": type}
        )

    async def disc_create_channel(
        self, guild_id: str, name: str, type: str = "text"
    ) -> dict[str, Any]:
        self.deprecated("disc_create_channel", "create_channel")
        return await self._discord_op(
            "create_channel", {"guild_id": guild_id, "name": name, "type": type}
        )

    async def disc_create_thread(
        self, channel_id: str, name: str, message: str | None = None
    ) -> dict[str, Any]:
        self.deprecated("disc_create_thread", "create_thread")
        args: dict[str, Any] = {"channel": channel_id, "name": name}
        if message is not None:
            args["message"] = message
        return await self._discord_op("create_thread", args)

    async def disc_thread(self, thread_id: str) -> dict[str, Any]:
        self.deprecated("disc_thread", "chat_thread")
        return await self.chat_thread(thread_id, backend=DISCORD)

    async def disc_dm(self, user_id: str, message: str) -> dict[str, Any]:
        self.deprecated("disc_dm", "chat_dm")
        return await self.chat_dm(user_id, message, backend=DISCORD)

    async def disc_react(
        self, message_id: str, emoji: str, channel_id: str | None = None
    ) -> dict[str, Any]:
        self.deprecated("disc_react", "chat_react")
        post = f"{channel_id}/{message_id}" if channel_id else message_id
        return await self.chat_react(post, emoji, backend=DISCORD)


# -- MCP ----------------------------------------------------------------------------------------
# Each alias, and what it is now: a `chat_*` tool or a Discord-only daemon op.
ALIAS_TARGETS: dict[str, str] = {
    "disc_send": "chat_send",
    "disc_read": "chat_read",
    "disc_list": "list_channels",
    "disc_resolve": "resolve",
    "disc_create_channel": "create_channel",
    "disc_create_thread": "create_thread",
    # Not in today's kit; kept from Story 2.4, harmless.
    "disc_thread": "chat_thread",
    "disc_dm": "chat_dm",
    "disc_react": "chat_react",
}
ALIASES = tuple(ALIAS_TARGETS)
ALIAS_HELP: dict[str, str] = {
    "disc_send": "Send `message` (or `content`) to `channel_id` (an ID, `name` or `#name`); "
    "`attach_path` attaches a local file, `embed` is `title:body`.",
    "disc_read": "Recent messages in `channel_id` (at most `limit`, default 20), or the one "
    "message `message_id`.",
    "disc_list": "The guild's channels of `type` (text, voice or category).",
    "disc_resolve": "The ID of the channel, user or role `name` (`type`).",
    "disc_create_channel": "Create a channel `name` of `type` in the guild.",
    "disc_create_thread": "Create a thread `name` in `channel_id`, with an optional first "
    "`message`.",
    "disc_thread": "Every message in the thread `thread_id`.",
    "disc_dm": "A direct message to the user `user_id`.",
    "disc_react": "Add `emoji` to `message_id` (in `channel_id`).",
}
TOOLS: dict[str, str] = {
    "chat_send": "Send `text` to channel `to`. With `thread`, it replies on that thread's "
    "backend. `backend` (mattermost:<server-id> or discord) overrides the default. "
    "`attach_path` attaches a local file (at most 8 MiB); `embed` is `title:body`.",
    "chat_read": "Recent messages in `channel` (`since` epoch seconds, at most `limit`).",
    "chat_thread": "Every message in the thread `post_id` belongs to.",
    "chat_dm": "A direct message to the user `to`; `attach_path` and `embed` as chat_send.",
    "chat_react": "Add the reaction `emoji` to the post `post_id`.",
    "chat_health": "Each configured backend's state.",
    "chat_set_default": "Record the human partner's answer: `key` (the repo's forge org, or its "
    "path) uses `backend` (mattermost:<server-id> or discord) by default. Local only.",
    **{
        a: f'Deprecated: {a} is {t}(backend="discord"). {ALIAS_HELP[a]}'
        for a, t in ALIAS_TARGETS.items()
    },
}


def build_server(shim: Shim, *, timers: bool = True) -> Any:
    """The FastMCP server with the `chat_*` tools and the `disc_*` aliases."""
    from mcp.server.fastmcp import FastMCP

    @contextlib.asynccontextmanager
    async def lifespan(_server: Any) -> AsyncIterator[None]:
        await shim.start()
        task = asyncio.create_task(shim.run_timers()) if timers else None
        try:
            yield None
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    server = FastMCP("crewchat", lifespan=lifespan)
    # FastMCP turns an exception from a tool into the tool's error result, with its message.
    for name, description in TOOLS.items():
        server.tool(name=name, description=description)(getattr(shim, name))
    return server


def find_repo_root(start: Path) -> Path:
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return start
    top = proc.stdout.strip()
    return Path(top) if proc.returncode == 0 and top else start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crewchat-mcp")
    parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
    parser.add_argument("--repo-root", type=Path, default=None)
    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s"
    )
    root = ns.repo_root or find_repo_root(Path.cwd())
    try:
        shim = Shim(root, SocketTransport(ns.socket))
    except ShimError as exc:
        print(f"crewchat-mcp: {exc}", file=sys.stderr)
        return 1
    build_server(shim).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
