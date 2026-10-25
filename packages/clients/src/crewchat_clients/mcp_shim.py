"""The thin MCP shim: the `chat_*` tools over the crewchat daemon's socket (Dev Spec §5.5;
Story 2.4; R-09 to R-13, R-15).

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
- **Replies (R-12).** The shim remembers the backend of every post it has seen. A `chat_send`
  with `thread` goes to that thread's backend, whatever the default is.
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
- **Aliases (R-15).** `disc_send`, `disc_read`, `disc_thread`, `disc_dm` and `disc_react` map
  to the `chat_*` tool with `backend="discord"`. Each logs one deprecation warning per session.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
import uuid
from collections import OrderedDict
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
MAX_KNOWN_POSTS = 10_000
IDENTITY_FILE = Path(".claude/agent-identity.json")
# The legacy identity file lives here as `claude-agent-<md5(repo_root)>.json` (transition).
LEGACY_IDENTITY_DIR = Path("/tmp")
SEED_FILE = Path(".claude/reseed-revive.md")
SQUAWKS_FILE = Path(".claude/sqtk-mine.json")
# CLAUDE_CODE_ENTRYPOINT values of a session with a human at the keyboard.
INTERACTIVE_ENTRYPOINTS = frozenset({"cli", "claude-vscode", "claude-desktop", "claude-jetbrains"})

log = logging.getLogger("crewchat_clients.mcp_shim")

Clock = Callable[[], datetime]
Transport = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
Squawker = Callable[[Sequence[str], Path], Awaitable[int]]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ShimError(Exception):
    """A tool call that failed; the message goes to the agent as the tool's error."""


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
        self._posts: OrderedDict[str, Backend] = OrderedDict()  # post ID -> its backend
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
        """The agent's backends, as calls name them: `register` lists a Mattermost server by
        its id, and a call names it `mattermost:<server-id>`."""
        value = self.registration.get("backends")
        names = [b for b in value if isinstance(b, str)] if isinstance(value, list) else []
        return [b if b == DISCORD or ":" in b else f"mattermost:{b}" for b in names]

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

    def _remember(self, post_id: Any, backend: Backend) -> None:
        if not isinstance(post_id, str) or not post_id:
            return
        self._posts[post_id] = backend
        self._posts.move_to_end(post_id)
        while len(self._posts) > MAX_KNOWN_POSTS:
            self._posts.popitem(last=False)

    def _remember_messages(self, result: Mapping[str, Any], backend: Backend) -> None:
        msgs = result.get("messages")
        for msg in msgs if isinstance(msgs, list) else []:
            if isinstance(msg, dict):
                self._remember(msg.get("post_id"), backend)
                self._remember(msg.get("root_id"), backend)

    def backend_of(self, post_id: str) -> Backend | None:
        """The backend of a post the shim has seen (sent, read or in a thread)."""
        return self._posts.get(post_id)

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
        """The backend for a call, and whether it is off-default; or the question to ask."""
        known = self.backend_of(post_id) if post_id else None
        if named is not None and not is_backend(named):
            raise ShimError(f"backend {named!r} is not mattermost:<server-id> or discord")
        if known is not None:
            if named is not None and named != known:
                raise ShimError(f"{post_id} is on {known}, not {named}; a reply follows its thread")
            return known, False
        if named is not None:
            try:
                default = self.default()
            except ShimError:
                default = None
            return named, named != default
        default = self.default()
        if isinstance(default, NeedsHuman):
            return await self.needs_human(default)
        return default, False

    # -- the tools ------------------------------------------------------------------------------
    async def chat_send(
        self, to: str, text: str, thread: str | None = None, backend: str | None = None
    ) -> dict[str, Any]:
        route = await self._route(backend, thread)
        if isinstance(route, dict):
            return route
        name, off = route
        args: dict[str, Any] = {"dev_name": self.dev_name, "backend": name, "to": to, "text": text}
        if thread:
            args["thread"] = thread
        if off:
            args["off_default"] = True
        result = await self._forward("send", args)
        self._remember(result.get("post_id"), name)
        self._remember(result.get("thread"), name)
        return result

    async def chat_dm(self, to: str, text: str, backend: str | None = None) -> dict[str, Any]:
        route = await self._route(backend)
        if isinstance(route, dict):
            return route
        name, off = route
        args: dict[str, Any] = {"dev_name": self.dev_name, "backend": name, "to": to, "text": text}
        if off:
            args["off_default"] = True
        result = await self._forward("dm", args)
        self._remember(result.get("post_id"), name)
        return result

    async def chat_read(
        self,
        channel: str,
        since: float | None = None,
        limit: int | None = None,
        backend: str | None = None,
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
        if off:
            args["off_default"] = True
        result = await self._forward("read", args)
        self._remember_messages(result, name)
        return result

    async def chat_thread(self, post_id: str, backend: str | None = None) -> dict[str, Any]:
        route = await self._route(backend, post_id)
        if isinstance(route, dict):
            return route
        name, off = route
        args: dict[str, Any] = {"dev_name": self.dev_name, "backend": name, "post_id": post_id}
        if off:
            args["off_default"] = True
        result = await self._forward("thread", args)
        self._remember_messages(result, name)
        return result

    async def chat_react(
        self, post_id: str, emoji: str, backend: str | None = None
    ) -> dict[str, Any]:
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
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        self.deprecated("disc_send", "chat_send")
        text = message if message is not None else content
        if not text:
            raise ShimError("disc_send needs message (or content)")
        return await self.chat_send(channel_id, text, thread=thread_id, backend=DISCORD)

    async def disc_read(
        self, channel_id: str, limit: int | None = None, message_id: str | None = None
    ) -> dict[str, Any]:
        self.deprecated("disc_read", "chat_read")
        result = await self.chat_read(
            channel_id, limit=None if message_id else limit, backend=DISCORD
        )
        if message_id:
            msgs = result.get("messages") or []
            found = [m for m in msgs if isinstance(m, dict) and m.get("post_id") == message_id]
            result = {**result, "messages": found}
        return result

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
ALIASES = ("disc_send", "disc_read", "disc_thread", "disc_dm", "disc_react")
TOOLS: dict[str, str] = {
    "chat_send": "Send `text` to channel `to`. With `thread`, it replies on that thread's "
    "backend. `backend` (mattermost:<server-id> or discord) overrides the default.",
    "chat_read": "Recent messages in `channel` (`since` epoch seconds, at most `limit`).",
    "chat_thread": "Every message in the thread `post_id` belongs to.",
    "chat_dm": "A direct message to the user `to`.",
    "chat_react": "Add the reaction `emoji` to the post `post_id`.",
    "chat_health": "Each configured backend's state.",
    "chat_set_default": "Record the human partner's answer: `key` (the repo's forge org, or its "
    "path) uses `backend` (mattermost:<server-id> or discord) by default. Local only.",
    **{a: f'Deprecated: {a} is chat_{a[5:]}(backend="discord").' for a in ALIASES},
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
