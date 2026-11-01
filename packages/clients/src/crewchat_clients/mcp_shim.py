"""The thin MCP shim: the `chat_*` tools over the crewchat daemon's socket (Dev Spec §5.5;
Stories 2.4, 2.4c; R-09 to R-13, R-15).

The shim is a stdio MCP server that runs in the agent's session, as the agent's user. It holds
**no credentials** (R-08): every chat tool forwards one frame to `/run/crewchat/crewchat.sock`,
and the daemon acts on the agent's behalf with what it holds itself.

- `chat_send`, `chat_read`, `chat_thread`, `chat_dm`, `chat_react`, `chat_edit` and
  `chat_health` each forward one frame. `chat_set_default(key, backend)` is the one local-only
  tool: it writes `~/.claude/chat-defaults.json` and sends nothing to the daemon.
- **Default backend (R-11).** The shim resolves it (`crewchat_clients.defaults`) and always
  sends the resolved `backend` in the frame; the daemon does no resolution. With no default,
  an interactive session gets a structured question for its human, answered through
  `chat_set_default`. A headless session's send fails loudly, and the shim runs
  `sqtk add <needs-list> "<question>"` with its own text, never message content. There is no
  Discord fallback.
- **Off-default (R-13).** A call that names a backend other than the default is sent with
  `off_default: true`, and the daemon logs `off-default: <dev> <backend>`.
- **Replies (R-12).** The shim keeps no map of posts to backends. A `chat_send` with
  `thread` (and `chat_thread`, `chat_react`, `chat_edit`) that names no backend asks the
  daemon (`thread_backend`), so the reply goes to that thread's backend whatever the default
  is, and across a restart of either side. A post the daemon can't place fails loudly; it
  never falls back to the default.
- **Backend names.** One form, as `register` lists them and every frame carries them:
  `mattermost:<server-id>` and `discord` (§5.4).
- **Attachments and embeds (R-15).** `chat_send` and `chat_dm` take `attach_path` and `embed`
  (`title:body`). The shim reads the file as the agent's user and streams it to the daemon in
  `upload` chunks that each fit a frame, then names the upload in the post's `attachments`. A
  file over 8 MiB, or one the daemon refuses, fails the call loudly before anything is posted.
- **Register.** At start, `register{dev_name, dev_team, dev_avatar, crew, session_id,
  project_key, repo}` from the identity file (`crew` omitted when the file has none).
  `project_key` is `repo_key()`, the same key `chat-defaults.json` uses, for the servers'
  allowlists (#117); `repo` is `repo_ref()`, the full forge path, for the kiosk's corpus pool
  (#139).
  `dev_team` and `dev_avatar` fall back to the legacy
  `/tmp/claude-agent-<md5(repo_root)>.json`, then to `""` (D-103). It registers again every
  24 hours, and before any call when the last one is older than that, so a long-lived session
  never ages out (R-30). A daemon that answers
  `not_registered` (it restarted) gets a fresh `register` and the call is retried once.
- **Ping (#129).** The timer sends `ping{dev_name}` every `PING_EVERY` (15 s). A
  `not_registered` answer re-registers and force-pushes, so an idle session comes back after a
  daemon restart with no tool call, and its doorbell, waiting through the restart, delivers.
  A daemon without the op (`unsupported`) is asked again every `PING_EVERY` too: the upgrade
  that brings the op is itself a restart, and a shim that stopped asking missed it (#137).
- **Logging (#144).** INFO on stderr by default. Claude Code doesn't keep an MCP server's
  stderr after it connects, so a field check needs a file: `CREWCHAT_SHIM_LOG` sets the level
  of the shim's own lines (`debug`, `info` or `warning`, any case; the MCP library stays at
  INFO, as its DEBUG carries message text), and `CREWCHAT_SHIM_LOG_FILE`, an absolute path,
  appends the same lines to that file, created 0600 and never rotated; a symlink at that path
  is refused, and an existing file is set to 0600. `debug` shows the
  timer's `tick` and `ping:` lines. Turn both on for a session, then remove them. A bad level
  or path logs one WARNING and the shim starts anyway, on stderr.
- **Pushes (§5.3).** The daemon never reads the agent's files, so the shim pushes what it
  needs: `kiosk_brief{dev_name, text}` with only the seed's `## Kiosk` section (R-32), at start
  and whenever the seed's mtime changes, and `squawk_ids{dev_name, ids, texts}` from the
  agent's squawk records, at start and whenever they change. `texts` is each squawk's original
  text, from its record's `.text` or the drain's `sqtk-baseline.json`, for its answer (#170).
- **Discord's guild ops (D-108).** `chat_list_channels`, `chat_create_channel` and
  `chat_create_thread` take today's `disc_*` argument shapes and always send
  `backend="discord"`. `chat_resolve` does too when given a `guild_id`; without one it goes to
  the named or default backend, where a Mattermost server resolves a channel name (#145).
  `chat_read` with `post_id` reads one message. There are no `disc_*` aliases: they were
  removed before the first deploy.
- **Health (#112).** `chat_health` sends `dev_name`, so a restarted daemon answers
  `not_registered` and the call re-registers, as any other call does.
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
    repo_key,
    repo_ref,
    resolve_default,
    write_default,
)

SOCKET_PATH = Path("/run/crewchat/crewchat.sock")
PROTO = 1
MAX_FRAME_BYTES = 1024 * 1024
CALL_TIMEOUT_S = 30.0
REGISTER_EVERY = timedelta(hours=24)
# How often the timer asks the daemon whether it still holds the agent (#129). Well inside the
# doorbell's restart grace, so a one-shot waiting through a restart sees the re-register.
PING_EVERY = timedelta(seconds=15)
POLL_INTERVAL_S = 5.0
DISCORD = "discord"
# A Discord snowflake, used with `fullmatch` (#62): the one shape a `<channel>/<message>` part
# may have, since it ends up in a REST path that carries the bot credential.
SNOWFLAKE_RE = re.compile(r"[0-9]{1,20}")
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
BASELINE_FILE = Path(".claude/sqtk-baseline.json")  # the drain hook's original texts
MAX_SQUAWK_TEXT = 4096  # the daemon's limit; a longer original is left out (unknown)
# The encoded `squawk_ids` args stay under this, well inside the daemon's 64 KiB frame: past
# it the frame is refused whole and no id lands, so texts are dropped (longest first) to fit.
SQUAWK_ARGS_BUDGET = 48 * 1024
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
    has its text prefixed with `OUTSIDER_PREFIX`. That is the top-level `text` of the kiosk
    review (Story 3.6, R-33), and `message.text` of an outsider message delivered under social
    (Story 3.3, R-23, #97)."""
    if event.get("outsider") is not True:
        return event
    out = dict(event)
    text = out.get("text")
    if isinstance(text, str) and not text.startswith(OUTSIDER_PREFIX):
        out["text"] = f"{OUTSIDER_PREFIX} {text}"
    message = out.get("message")
    if isinstance(message, dict):
        body = message.get("text")
        if isinstance(body, str) and not body.startswith(OUTSIDER_PREFIX):
            out["message"] = {**message, "text": f"{OUTSIDER_PREFIX} {body}"}
    return out


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
        if not isinstance(reply, dict):
            raise DaemonUnreachable(f"crewchat daemon sent a mismatched reply to {op}")
        # An error with no id is one the daemon gave before it could read the frame (a frame
        # too large). The connection carries one frame, so it is this frame's error.
        unread = reply.get("id") is None and not reply.get("ok")
        if reply.get("id") != req_id and not unread:
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

    mtime_ns: Any = None  # one file's mtime, or a tuple of them
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


def _json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_squawk_texts(mine: Path, baseline: Path, ids: list[str]) -> dict[str, str]:
    """Each squawk's original text O, by id, as `sqtk-doorbell.sh`'s `load_baselines` reads it
    (#170): the drain's baseline file, overlaid by each record's own `.text`, kept to `ids`.
    Only strings count (anything else is unknown), and only up to `MAX_SQUAWK_TEXT`."""
    texts: dict[str, str] = {}
    base = _json_file(baseline)
    if isinstance(base, dict):
        texts.update({str(k): v for k, v in base.items() if isinstance(v, str)})
    records = _json_file(mine)
    for item in records if isinstance(records, list) else []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            sid = item.get("id")
            if isinstance(sid, int | str) and not isinstance(sid, bool):
                texts[str(sid)] = item["text"]
    return {i: texts[i] for i in ids if i in texts and len(texts[i]) <= MAX_SQUAWK_TEXT}


def fit_squawk_texts(
    dev_name: str, ids: list[str], texts: dict[str, str], budget: int = SQUAWK_ARGS_BUDGET
) -> dict[str, str]:
    """`texts` cut until the encoded `squawk_ids` args fit `budget`, longest dropped first. A
    dropped text is unknown to the daemon, so that squawk's answer falls back to the last
    arrow: worse, but the ids, and every other squawk's doorbell, still land."""

    def size(kept: dict[str, str]) -> int:
        args = {"dev_name": dev_name, "ids": ids, "texts": kept}
        return len(json.dumps(args).encode())

    # Each entry costs its `"id": "text"` plus a `, ` separator, as `json.dumps` writes it.
    cost = {i: len(json.dumps({i: t}).encode()) for i, t in texts.items()}
    kept, total = dict(texts), size(texts)
    for i in sorted(kept, key=lambda i: (-cost[i], i)):
        if total <= budget:
            break
        del kept[i]
        total -= cost[i]
    while kept and size(kept) > budget:  # the estimate's last few bytes, never more than once
        del kept[max(kept, key=lambda i: (cost[i], i))]
    return kept


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
        self.pinged_at: datetime | None = None
        self._said_unsupported = False  # `unsupported` is logged at INFO once, then DEBUG
        self._brief = _FileState()
        self._squawks = _FileState()
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
            # For the server allowlists (#117): `host/org` or the repo's path, never the
            # remote URL, which can carry credentials.
            "project_key": repo_key(self.repo_root, self.remote_for),
            # For the kiosk's corpus pool (#139, D-112): `host/group/.../repo`, or "".
            "repo": repo_ref(self.repo_root, self.remote_for),
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

    async def ping(self) -> None:
        """Every `PING_EVERY`, ask the daemon whether it still holds the agent, and register
        again when it doesn't (#129). A restarted daemon has lost every registration, and
        without this an idle session stays unregistered until its next tool call, so its
        doorbell can't arm. A daemon older than `ping` answers `unsupported`, and is asked
        again like any other: never latch it off, because the upgrade that brings `ping` is a
        restart, the one this exists for (#137). One that is down is asked on the next tick."""
        if self.registered_at is None:
            return  # unregistered: `ensure_registered` retries the register itself
        now = self.clock()
        if self.pinged_at is not None and now - self.pinged_at < PING_EVERY:
            return
        self.pinged_at = now
        try:
            await self.transport("ping", {"dev_name": self.dev_name})
            log.debug("ping: the daemon holds %s", self.dev_name)
            return
        except DaemonUnreachable as exc:
            log.debug("ping: unreachable: %s", exc)
            return
        except DaemonError as exc:
            if exc.code == "unsupported":
                level = logging.DEBUG if self._said_unsupported else logging.INFO
                log.log(level, "ping: the daemon has no ping op yet; asking again next time")
                self._said_unsupported = True
                return
            if exc.code != "not_registered":
                raise
        log.info("ping: the daemon no longer holds %s (it restarted); registering", self.dev_name)
        await self.register()
        await self.sync_files(force=True)

    # -- pushes ---------------------------------------------------------------------------------
    async def sync_files(self, *, force: bool = False) -> None:
        """Push the brief when the seed's mtime changed, and the squawk ids when they changed.
        `force` pushes both, and a forced push that fails stays owed until it lands
        (mm:triage #47); one failing never skips the other."""
        if force:
            self._brief = _FileState()
            self._squawks = _FileState()
        errors: list[ShimError] = []
        seed = self.repo_root / SEED_FILE
        mtime = file_mtime(seed)
        if not self._brief.pushed or mtime != self._brief.mtime_ns:
            text = read_brief(seed)
            try:
                await self.transport("kiosk_brief", {"dev_name": self.dev_name, "text": text})
                self._brief = _FileState(mtime_ns=mtime, value=text, pushed=True)
            except ShimError as exc:
                errors.append(exc)

        squawks = self.repo_root / SQUAWKS_FILE
        baseline = self.repo_root / BASELINE_FILE
        mtimes = (file_mtime(squawks), file_mtime(baseline))
        if not self._squawks.pushed or mtimes != self._squawks.mtime_ns:
            ids = read_squawk_ids(squawks)
            texts = read_squawk_texts(squawks, baseline, ids)
            try:
                if not self._squawks.pushed or (ids, texts) != self._squawks.value:
                    await self.push_squawks(ids, texts)
                self._squawks = _FileState(mtime_ns=mtimes, value=(ids, texts), pushed=True)
            except ShimError as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    async def push_squawks(self, ids: list[str], texts: dict[str, str]) -> None:
        """`squawk_ids`, with the texts fitted to the frame. Should the daemon still refuse it,
        the ids go again without texts: the doorbell needs the ids, the texts only refine it."""
        fitted = fit_squawk_texts(self.dev_name, ids, texts)
        if len(fitted) < len(texts):
            left_out = len(texts) - len(fitted)
            log.warning("squawk_ids: %d of %d texts left out to fit", left_out, len(texts))
        args: dict[str, Any] = {"dev_name": self.dev_name, "ids": ids, "texts": fitted}
        try:
            await self.transport("squawk_ids", args)
        except DaemonError as exc:
            if exc.code != "bad_request" or not fitted:
                raise
            log.warning("squawk_ids: refused with texts (%s); pushing the ids alone", exc.error)
            await self.transport("squawk_ids", {**args, "texts": {}})

    async def start(self) -> None:
        """Register and push; a daemon that isn't up yet is retried before the first call."""
        try:
            await self.register()
            await self.sync_files(force=True)
        except ShimError as exc:
            log.error("start: %s (retried before the next call)", exc)

    async def tick(self) -> None:
        """The timer's work: the daily re-register, the ping, and the file pushes."""
        async with self._lock:
            log.debug("tick")  # the timer is running: a field check tells this link apart
            try:
                await self.ensure_registered()
                await self.ping()
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
            try:
                await self.tick()
            except Exception:
                # An unexpected error must not end the task: nothing awaits it, so the timer
                # would stop in silence and an idle session would never re-register again.
                log.exception("tick failed; the timer carries on")

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
        post_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._read(channel, since=since, limit=limit, backend=backend, post_id=post_id)

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

    async def chat_edit(
        self, post_id: str, text: str, embed: str | None = None, backend: str | None = None
    ) -> dict[str, Any]:
        """Rewrite the agent's own post `post_id` (#149): on the post's own backend unless
        `backend` names one, as `chat_react` routes."""
        discord_ref(post_id)  # a malformed `<channel>/<message>` fails here, before any frame
        route = await self._route(backend, post_id)
        if isinstance(route, dict):
            return route
        name, off = route
        args: dict[str, Any] = {
            "dev_name": self.dev_name,
            "backend": name,
            "post_id": post_id,
            "text": text,
        }
        if embed is not None:
            args["embed"] = embed
        if off:
            args["off_default"] = True
        return await self._forward("edit", args)

    async def chat_health(self) -> dict[str, Any]:
        # With `dev_name`, a restarted daemon answers `not_registered` and `_forward`
        # re-registers (#112).
        return await self._forward("health", {"dev_name": self.dev_name})

    async def chat_set_default(self, key: str, backend: str) -> dict[str, Any]:
        """Local only: record the human's answer in the defaults file. No frame is sent."""
        try:
            write_default(key, backend, self.defaults_file)
        except DefaultsError as exc:
            raise ShimError(str(exc)) from exc
        self._squawked.discard(key)
        return {"key": key, "backend": backend, "file": str(self.defaults_file)}

    # -- Discord's guild ops (D-108) ---------------------------------------------------------
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

    async def chat_list_channels(self, guild_id: str, type: str = "text") -> dict[str, Any]:
        return await self._discord_op("list_channels", {"guild_id": guild_id, "type": type})

    async def chat_resolve(
        self,
        name: str,
        type: str = "channel",
        guild_id: str | None = None,
        backend: str | None = None,
    ) -> dict[str, Any]:
        """With `guild_id`, Discord, as before. Without, the named or default backend: a
        Mattermost server resolves a channel name in the agent's team (#145)."""
        if guild_id is not None:
            return await self._discord_op(
                "resolve", {"guild_id": guild_id, "name": name, "type": type}
            )
        route = await self._route(backend)
        if isinstance(route, dict):
            return route
        backend_name, off = route
        frame: dict[str, Any] = {
            "dev_name": self.dev_name,
            "backend": backend_name,
            "name": name,
            "type": type,
        }
        if off:
            frame["off_default"] = True
        return await self._forward("resolve", frame)

    async def chat_create_channel(
        self, guild_id: str, name: str, type: str = "text"
    ) -> dict[str, Any]:
        return await self._discord_op(
            "create_channel", {"guild_id": guild_id, "name": name, "type": type}
        )

    async def chat_create_thread(
        self, channel_id: str, name: str, message: str | None = None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"channel": channel_id, "name": name}
        if message is not None:
            args["message"] = message
        return await self._discord_op("create_thread", args)


# -- MCP ----------------------------------------------------------------------------------------
TOOLS: dict[str, str] = {
    "chat_send": "Send `text` to channel `to` (an ID, or a name such as `precheck` or "
    "`#precheck`, on either backend). With `thread`, it replies on that thread's "
    "backend. `backend` (mattermost:<server-id> or discord) overrides the default. "
    "`attach_path` attaches a local file (at most 8 MiB); `embed` is `title:body`.",
    "chat_read": "Recent messages in `channel` (an ID or a name, on either backend; `since` "
    "epoch seconds, at most `limit`), or the one message `post_id`.",
    "chat_thread": "Every message in the thread `post_id` belongs to.",
    "chat_dm": "A direct message to the user `to`; `attach_path` and `embed` as chat_send.",
    "chat_react": "Add the reaction `emoji` to the post `post_id`.",
    "chat_edit": "Rewrite your own post `post_id` to `text` (`embed` is `title:body`), on the "
    "post's own backend. Another agent's post is `forbidden`; a gone one `not_found`.",
    "chat_health": "Each configured backend's state.",
    "chat_set_default": "Record the human partner's answer: `key` (the repo's forge org, or its "
    "path) uses `backend` (mattermost:<server-id> or discord) by default. Local only.",
    # Discord only (D-108).
    "chat_list_channels": "Discord only: the guild's channels of `type` (text, voice or category).",
    "chat_resolve": "The ID of `name`. On Discord, give `guild_id`: a channel, user or role "
    "(`type`). On Mattermost (the default, or `backend`), no `guild_id`: a channel, by URL "
    "name or display name, in the agent's team.",
    "chat_create_channel": "Discord only: create a channel `name` of `type` in the guild.",
    "chat_create_thread": "Discord only: create a thread `name` in `channel_id`, with an "
    "optional first `message`.",
}


def build_server(shim: Shim, *, timers: bool = True) -> Any:
    """The FastMCP server with the `chat_*` tools."""
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


LOG_FORMAT = "%(asctime)s %(name)s %(message)s"
LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}


def configure_logging(environ: Mapping[str, str]) -> None:
    """The root logger: stderr always, plus the file `CREWCHAT_SHIM_LOG_FILE` names (#144).
    `CREWCHAT_SHIM_LOG` (`info` by default) sets the level of the shim's own loggers only:
    the root stays at INFO, or WARNING when asked, because the MCP library logs every request
    it receives at DEBUG, message text included, and that must never reach a file. A bad
    level or path logs one WARNING and leaves stderr alone: the shim never fails to start
    over its logging."""
    root = logging.getLogger()
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(stderr)
    wanted = environ.get("CREWCHAT_SHIM_LOG", "info")
    level = LOG_LEVELS.get(wanted.lower()) or logging.INFO
    root.setLevel(max(level, logging.INFO))
    logging.getLogger("crewchat_clients").setLevel(level)
    if wanted.lower() not in LOG_LEVELS:
        log.warning("CREWCHAT_SHIM_LOG=%r is not debug, info or warning: using info", wanted)
    path = environ.get("CREWCHAT_SHIM_LOG_FILE")
    if not path:
        return
    if not os.path.isabs(path):
        log.warning("CREWCHAT_SHIM_LOG_FILE=%r is not an absolute path: stderr only", path)
        return
    # O_NOFOLLOW refuses a symlink planted at the path (ELOOP), and fchmod tightens a file
    # that already existed: the mode in `os.open` applies only to a file it creates.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            raise
    except OSError as exc:
        log.warning("CREWCHAT_SHIM_LOG_FILE=%r can't be opened (%s): stderr only", path, exc)
        return
    to_file = logging.StreamHandler(os.fdopen(fd, "a", buffering=1, encoding="utf-8"))
    to_file.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(to_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crewchat-mcp")
    parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
    parser.add_argument("--repo-root", type=Path, default=None)
    ns = parser.parse_args(argv)
    configure_logging(os.environ)
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
