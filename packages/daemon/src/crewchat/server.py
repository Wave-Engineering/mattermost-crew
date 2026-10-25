"""The daemon's socket server: `/run/crewchat/crewchat.sock`, mode `0660`, group
`crewchat-clients` (Dev Spec §5.3, §5.4).

Each connection carries JSON-line frames (`crewchat.protocol`); each frame is dispatched to
the handler for its `op`. Unknown ops get `unsupported`. No op ever returns a token: tokens
stay inside the daemon (R-08).

A handler may return a `Reply`: its `result` is sent, and its `on_sent` runs only once the
response frame has been written and drained. A client that hangs up first, or a failed
write, never runs it. `subscribe` acknowledges its event there (#99).
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from crewchat.config import ServersConfig
from crewchat.protocol import MAX_FRAME_BYTES, ProtocolError, encode, err, ok, parse_frame
from crewchat.registry import RegisterError, Registry, mattermost_backend

SOCKET_PATH = Path("/run/crewchat/crewchat.sock")
SOCKET_MODE = 0o660
CLIENTS_GROUP = "crewchat-clients"

log = logging.getLogger("crewchat.server")


@dataclass(frozen=True)
class Reply:
    """A handler's result, and what to run once the client has been sent it."""

    result: dict[str, Any]
    on_sent: Callable[[], None]


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | Reply]]
Sent = Callable[[], None] | None


class OpError(Exception):
    """An op failed; `code` (and the detail) go to the caller as the error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class DaemonServer:
    """`hangup_cancels` names the ops a client's hangup cancels while they run: `subscribe`,
    which would otherwise take an event off the agent's queue for a client that has gone."""

    def __init__(
        self, handlers: dict[str, Handler], *, hangup_cancels: frozenset[str] = frozenset()
    ) -> None:
        self.handlers = handlers
        self.hangup_cancels = hangup_cancels
        self._server: asyncio.Server | None = None
        # For `close_clients` (#107): the handlers waiting for a frame, and the `hangup_cancels`
        # ops waiting for their result.
        self._closing = False
        self._idle: set[asyncio.Task[Any]] = set()
        self._waiting: set[asyncio.Future[Any]] = set()

    def close_clients(self) -> None:
        """End every connection (#107). Call it after the listener's `close()` and before
        `wait_closed()`: on Python 3.12, `wait_closed()` waits for every connection, and
        `close()` closes none of them, so a waiting `subscribe` would hold shutdown forever.

        A connection waiting for a frame is closed. A `hangup_cancels` op still waiting is
        cancelled and writes nothing, so the event a `subscribe` took stays held (#99). Any
        other op runs to its end and its reply is written: cancelling a `send` whose post had
        already reached the server would have the agent post it twice. A connection accepted
        from now on closes at once."""
        self._closing = True
        for task in list(self._idle):
            task.cancel()
        for op in list(self._waiting):
            op.cancel()

    @staticmethod
    def _op(line: bytes) -> str | None:
        try:
            return parse_frame(line).op
        except ProtocolError:
            return None

    async def handle_line(self, line: bytes) -> dict[str, Any]:
        """One request frame in, one response frame out. Never raises. A `Reply`'s
        `on_sent` runs here at once: the caller has the frame."""
        frame, on_sent = await self._handle(line)
        self._sent(on_sent)
        return frame

    async def _handle(self, line: bytes) -> tuple[dict[str, Any], Sent]:
        """The response frame, and what to run once it has been written."""
        try:
            req = parse_frame(line)
        except ProtocolError as exc:
            return err(exc.req_id, str(exc)), None
        handler = self.handlers.get(req.op)
        if handler is None:
            return err(req.id, "unsupported"), None
        try:
            result = await handler(req.args)
        except (OpError, RegisterError) as exc:
            return err(req.id, str(exc)), None
        except Exception:
            log.exception("%s failed", req.op)
            return err(req.id, "internal"), None
        if isinstance(result, Reply):
            return ok(req.id, result.result), result.on_sent
        return ok(req.id, result), None

    @staticmethod
    def _sent(on_sent: Sent) -> None:
        if on_sent is None:
            return
        try:
            on_sent()
        except Exception:
            log.exception("after sending a reply")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        next_line: asyncio.Future[bytes] | None = None
        handling: asyncio.Future[tuple[dict[str, Any], Sent]] | None = None
        task = asyncio.current_task()
        try:
            while not self._closing:  # set before this handler's first step, it closes at once
                if task is not None:
                    self._idle.add(task)
                try:
                    if next_line is None:
                        line = await reader.readline()
                    else:
                        line, next_line = await next_line, None
                except (ValueError, asyncio.LimitOverrunError):
                    writer.write(encode(err(None, "bad_request: frame too large")))
                    await writer.drain()
                    return
                finally:
                    if task is not None:
                        self._idle.discard(task)
                if not line:
                    return
                if not line.strip():
                    continue
                if self._op(line) not in self.hangup_cancels:
                    frame, on_sent = await self._handle(line)
                    writer.write(encode(frame))
                    await writer.drain()
                    self._sent(on_sent)
                    continue
                # Watch for the client hanging up while the op waits; a line it sends
                # meanwhile is kept for the next turn of the loop.
                handling = asyncio.ensure_future(self._handle(line))
                next_line = asyncio.ensure_future(reader.readline())
                self._waiting.add(handling)
                try:
                    await asyncio.wait({handling, next_line}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    self._waiting.discard(handling)
                if handling.cancelled():
                    return  # by `close_clients`: nothing is written, the event stays held
                # A hangup is end-of-file or a failed read (#107); a frame too large is
                # answered once the op's reply is written.
                hung_up = False
                if next_line.done():
                    exc = next_line.exception()
                    if exc is None:
                        hung_up = not next_line.result()
                    else:
                        hung_up = not isinstance(exc, ValueError | asyncio.LimitOverrunError)
                if hung_up:
                    # Gone before its reply: nothing is written and `on_sent` never runs,
                    # so an event `subscribe` took stays held for the next one (#99).
                    if not handling.done():
                        handling.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await handling
                    return
                frame, on_sent = await handling
                writer.write(encode(frame))
                await writer.drain()
                self._sent(on_sent)
        except ConnectionError:
            return
        finally:
            for pending in (handling, next_line):
                if pending is not None and not pending.done():
                    pending.cancel()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def start(self, path: Path, group: str | None = CLIENTS_GROUP) -> asyncio.Server:
        """Listen on `path`, mode 0660; with `group`, give the socket that group."""
        path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._client, path=str(path), limit=MAX_FRAME_BYTES
        )
        os.chmod(path, SOCKET_MODE)
        if group is not None:
            os.chown(path, -1, grp.getgrnam(group).gr_gid)
        return self._server


class HealthSource(Protocol):
    """A backend that reports its own state (the Discord adapter: scream-hole, D-106)."""

    async def health(self) -> dict[str, Any]: ...


def build_handlers(
    config: ServersConfig,
    registry: Registry,
    health_sources: Mapping[str, HealthSource] | None = None,
) -> dict[str, Handler]:
    """`register` and `health`; the chat ops are `crewchat.chat`'s.

    `health` names every backend in the one form (§5.4): `mattermost:<server_id>`, and
    `discord`. A backend in `health_sources` answers for itself; any other reports
    `configured`, from the daemon's start.
    """
    started = datetime.now(UTC).isoformat()
    sources = dict(health_sources or {})

    async def register(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, crew, session_id = args.get("dev_name"), args.get("crew"), args.get("session_id")
        if not isinstance(dev_name, str) or not isinstance(session_id, str):
            raise OpError("bad_request", "dev_name and session_id are required strings")
        if crew is not None and not isinstance(crew, str):
            raise OpError("bad_request", "crew is a string")
        dev_team, dev_avatar = args.get("dev_team", ""), args.get("dev_avatar", "")
        if not isinstance(dev_team, str) or not isinstance(dev_avatar, str):
            raise OpError("bad_request", "dev_team and dev_avatar are strings")
        reg = await registry.register(
            dev_name, crew, session_id, dev_team=dev_team, dev_avatar=dev_avatar
        )
        return reg.result()

    async def health(_args: dict[str, Any]) -> dict[str, Any]:
        names = [mattermost_backend(s.id) for s in config.mattermost]
        if config.discord_enabled:
            names.append("discord")
        backends: dict[str, Any] = {}
        for name in names:
            source = sources.get(name)
            backends[name] = (
                await source.health()
                if source is not None
                else {"state": "configured", "since": started}
            )
        return {"backends": backends}

    return {"register": register, "health": health}
