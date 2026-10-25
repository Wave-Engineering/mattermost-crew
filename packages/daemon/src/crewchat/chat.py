"""The chat ops: `send`, `dm`, `read`, `thread` and `react`, plus the shim's pushes
`kiosk_brief` and `squawk_ids` (Dev Spec §5.4; Story 2.4).

Every chat frame names the agent it acts as (`dev_name`) and the backend it goes to
(`backend`, e.g. `mattermost:<server_id>` or `discord`). The thin MCP shim resolves the
backend before it sends the frame (R-11); the daemon does no default resolution. A frame the
shim marks `off_default` (the call named a backend other than the agent's default) is logged
as `off-default: <dev> <backend>` (R-13).

The daemon never reads a file on the agent's side (§5.3). What it knows about the agent's seed
brief and its squawks is what the shim pushed: `AgentContext` keeps it for the doorbell and the
kiosk (Phase 3).

The pull path's switch filter (R-21) arrives with Story 3.3; until then `read` and `thread`
return what the adapter returned.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from crewchat.adapters.base import ChatBackend, InboundMessage
from crewchat.registry import Registration, Registry
from crewchat.server import Handler, OpError
from crewchat.threads import MyThreads

MAX_BRIEF_CHARS = 32 * 1024
MAX_SQUAWK_IDS = 1000
MATTERMOST_PREFIX = "mattermost:"

log = logging.getLogger("crewchat.chat")


@dataclass
class AgentContext:
    """What the shims pushed, by Dev-Name: the `## Kiosk` brief and the squawk ids."""

    briefs: dict[str, str] = field(default_factory=dict)
    squawk_ids: dict[str, list[str]] = field(default_factory=dict)


def registered_name(backend: str) -> str:
    """How `register` lists a backend: a Mattermost server by its id, Discord as `discord`."""
    return backend.removeprefix(MATTERMOST_PREFIX)


def message_json(msg: InboundMessage) -> dict[str, Any]:
    return dataclasses.asdict(msg)


def _str(args: Mapping[str, Any], key: str, *, required: bool = True) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise OpError("bad_request", f"{key} is a required string")
    return value


def _text(args: Mapping[str, Any]) -> str:
    value = args.get("text")
    if not isinstance(value, str) or not value.strip():
        raise OpError("bad_request", "text is a required non-empty string")
    return value


def build_chat_handlers(
    registry: Registry,
    backends: Mapping[str, ChatBackend],
    threads: MyThreads,
    context: AgentContext,
) -> dict[str, Handler]:
    """The chat ops over `backends`, keyed by backend name."""

    def agent(args: Mapping[str, Any]) -> tuple[str, Registration, str, ChatBackend]:
        dev_name = _str(args, "dev_name")
        backend_name = _str(args, "backend")
        assert dev_name is not None and backend_name is not None
        reg = registry.get(dev_name)
        if reg is None:
            raise OpError("not_registered", dev_name)
        backend = backends.get(backend_name)
        if backend is None:
            raise OpError("unknown_backend", backend_name)
        if registered_name(backend_name) not in reg.backends:
            raise OpError("not_on_backend", f"{dev_name} is not registered on {backend_name}")
        if args.get("off_default") is True:
            log.info("off-default: %s %s", dev_name, backend_name)
        return dev_name, reg, backend_name, backend

    def backend_error(backend_name: str, op: str, exc: Exception) -> OpError:
        if isinstance(exc, OpError):
            return exc
        if isinstance(exc, ValueError | KeyError):
            return OpError("bad_request", f"{backend_name}: {exc}")
        log.error("%s on %s failed: %s: %s", op, backend_name, type(exc).__name__, exc)
        return OpError("backend_error", f"{backend_name}: {type(exc).__name__}")

    async def send(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        to, text = _str(args, "to"), _text(args)
        thread = _str(args, "thread", required=False)
        assert to is not None
        try:
            msg = await backend.send(dev_name, to, text, thread)
        except Exception as exc:
            raise backend_error(backend_name, "send", exc) from exc
        root = threads.record(dev_name, msg)
        return {"backend": backend_name, "post_id": msg.post_id, "thread": root}

    async def dm(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        to, text = _str(args, "to"), _text(args)
        assert to is not None
        try:
            msg = await backend.dm(dev_name, to, text)
        except Exception as exc:
            raise backend_error(backend_name, "dm", exc) from exc
        threads.record(dev_name, msg)
        return {"backend": backend_name, "post_id": msg.post_id, "channel": msg.channel}

    async def read(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        channel = _str(args, "channel")
        assert channel is not None
        since, limit = args.get("since"), args.get("limit")
        if since is not None and (isinstance(since, bool) or not isinstance(since, int | float)):
            raise OpError("bad_request", "since is a number of seconds since the epoch")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
            raise OpError("bad_request", "limit is an integer")
        if limit is not None and limit < 1:
            raise OpError("bad_request", "limit is at least 1")
        try:
            msgs = await backend.read(
                dev_name, channel, float(since) if since is not None else None, limit
            )
        except Exception as exc:
            raise backend_error(backend_name, "read", exc) from exc
        return {"backend": backend_name, "messages": [message_json(m) for m in msgs]}

    async def thread(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        post_id = _str(args, "post_id")
        assert post_id is not None
        try:
            msgs = await backend.thread(dev_name, post_id)
        except Exception as exc:
            raise backend_error(backend_name, "thread", exc) from exc
        return {"backend": backend_name, "messages": [message_json(m) for m in msgs]}

    async def react(args: dict[str, Any]) -> dict[str, Any]:
        dev_name, _reg, backend_name, backend = agent(args)
        post_id, emoji = _str(args, "post_id"), _str(args, "emoji")
        assert post_id is not None and emoji is not None
        try:
            await backend.react(dev_name, post_id, emoji)
        except Exception as exc:
            raise backend_error(backend_name, "react", exc) from exc
        return {}

    async def kiosk_brief(args: dict[str, Any]) -> dict[str, Any]:
        dev_name = _str(args, "dev_name")
        text = args.get("text")
        assert dev_name is not None
        if not isinstance(text, str):
            raise OpError("bad_request", "text is a string (empty when there is no brief)")
        if len(text) > MAX_BRIEF_CHARS:
            raise OpError("bad_request", f"the brief is longer than {MAX_BRIEF_CHARS} characters")
        context.briefs[dev_name] = text
        return {}

    async def squawk_ids(args: dict[str, Any]) -> dict[str, Any]:
        dev_name = _str(args, "dev_name")
        ids = args.get("ids")
        assert dev_name is not None
        if not isinstance(ids, list) or len(ids) > MAX_SQUAWK_IDS:
            raise OpError("bad_request", f"ids is a list of at most {MAX_SQUAWK_IDS} ids")
        out: list[str] = []
        for item in ids:
            if isinstance(item, bool) or not isinstance(item, int | str) or item == "":
                raise OpError("bad_request", "each id is an integer or a non-empty string")
            out.append(str(item))
        context.squawk_ids[dev_name] = out
        return {}

    return {
        "send": send,
        "dm": dm,
        "read": read,
        "thread": thread,
        "react": react,
        "kiosk_brief": kiosk_brief,
        "squawk_ids": squawk_ids,
    }
