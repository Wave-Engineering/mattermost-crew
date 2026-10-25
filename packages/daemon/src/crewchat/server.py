"""The daemon's socket server: `/run/crewchat/crewchat.sock`, mode `0660`, group
`crewchat-clients` (Dev Spec §5.3, §5.4).

Each connection carries JSON-line frames (`crewchat.protocol`); each frame is dispatched to
the handler for its `op`. Unknown ops get `unsupported`. No op ever returns a token: tokens
stay inside the daemon (R-08).
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crewchat.config import ServersConfig
from crewchat.protocol import MAX_FRAME_BYTES, ProtocolError, encode, err, ok, parse_frame
from crewchat.registry import RegisterError, Registry

SOCKET_PATH = Path("/run/crewchat/crewchat.sock")
SOCKET_MODE = 0o660
CLIENTS_GROUP = "crewchat-clients"

log = logging.getLogger("crewchat.server")

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class OpError(Exception):
    """An op failed; `code` (and the detail) go to the caller as the error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class DaemonServer:
    def __init__(self, handlers: dict[str, Handler]) -> None:
        self.handlers = handlers
        self._server: asyncio.Server | None = None

    async def handle_line(self, line: bytes) -> dict[str, Any]:
        """One request frame in, one response frame out. Never raises."""
        try:
            req = parse_frame(line)
        except ProtocolError as exc:
            return err(exc.req_id, str(exc))
        handler = self.handlers.get(req.op)
        if handler is None:
            return err(req.id, "unsupported")
        try:
            return ok(req.id, await handler(req.args))
        except (OpError, RegisterError) as exc:
            return err(req.id, str(exc))
        except Exception:
            log.exception("%s failed", req.op)
            return err(req.id, "internal")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    writer.write(encode(err(None, "bad_request: frame too large")))
                    await writer.drain()
                    return
                if not line:
                    return
                if not line.strip():
                    continue
                writer.write(encode(await self.handle_line(line)))
                await writer.drain()
        except ConnectionError:
            return
        finally:
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


def build_handlers(config: ServersConfig, registry: Registry) -> dict[str, Handler]:
    """The ops this story serves: `register` and `health`. Phase 2 adds the chat ops."""
    started = datetime.now(UTC).isoformat()

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
        # The adapters arrive in Phase 2 (Stories 2.1, 2.2); until then every configured
        # backend reports `configured`, from the daemon's start.
        backends = {s.id: {"state": "configured", "since": started} for s in config.mattermost}
        if config.discord_enabled:
            backends["discord"] = {"state": "configured", "since": started}
        return {"backends": backends}

    return {"register": register, "health": health}
