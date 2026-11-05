"""Broker clients, the token store and daily rotation (Dev Spec §4.8, §5.6; R-05, R-08, R-30).

- `BrokerClient`: one per (crew, server_id) on this host, from `[crews.<crew>.<server_id>]
  broker_socket`. Every call has a short timeout (`broker_timeout_s`, default 2 s); a timeout
  raises `BrokerUnavailable` and never stalls the daemon.
- `SwitchCache` (`crewchat.filters`, Story 3.3) holds each broker's last good `crew_info` and
  `switch_state`, fetched through these clients.
- `TokenStore`: tokens by (dev_name, server_id), in memory and in
  `/var/lib/crewchat/tokens/<dev>.json` (file `0600`, directory `0700`, owner `crewchat`).
- `Rotator`: the daily remint -> swap -> `on_token_rotated` -> revoke, paced at 5/s or less.
- A dropped agent's tokens, or its old crew's after a crew change, go into a ledger
  (`.retired-revokes.json` in the tokens directory) before their revoke is tried, and leave it
  only once revoked. `retry_retired` works it off at daemon start and before each daily
  rotation (#151).

**Only `register` calls `BrokerClient.mint`** (`crewchat.registry`). A 401 and the daily
rotation both use `remint`, so neither refreshes the broker's `last_register` (R-30). The
rotation sequence never revokes before the swap and the reconnect hook, so no websocket is
left on a dead token (§4.8). A token retired on one server (the allowlist, #117, or a
deactivated account, #127) closes that agent's doorbell task and stream on that server, and
on no other (`Adapter.close_server`, #155).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar

from crewchat.config import ServersConfig
from crewchat.protocol import PROTO

TOKENS_DIR = Path("/var/lib/crewchat/tokens")
FILE_MODE = 0o600
DIR_MODE = 0o700
IDLE_DAYS = 30
ROTATIONS_PER_S = 5.0
# A failed rotation is retried well inside the 26 h native expiry, not 24 h later.
RETRY_FIRST_S = 5 * 60.0
RETRY_MAX_S = 3600.0
MAX_REPLY_BYTES = 1024 * 1024
# Retired tokens still owed a revoke, by "<crew>/<server_id>" (#151). A dot-file, so `load`
# never reads it as an agent.
RETIRED_FILE = ".retired-revokes.json"

log = logging.getLogger("crewchat.tokens")

T = TypeVar("T")
BrokerKey = tuple[str, str]  # (crew, server_id)


class BrokerUnavailable(Exception):
    """The broker did not answer within the timeout, or its socket is not there."""


class BrokerError(Exception):
    """The broker answered `ok: false`. `code` is the first word of its error."""

    def __init__(self, op: str, error: str) -> None:
        self.op = op
        self.error = error
        self.code = error.split(":", 1)[0].strip()
        super().__init__(f"{op}: {error}")


class Unauthorized(Exception):
    """A backend answered 401 for the token it was given. Adapters raise it (Story 2.1)."""


class BrokerClient:
    """The daemon's side of one broker socket. Each call is one connection and one frame."""

    def __init__(self, socket_path: Path | str, timeout_s: float, *, name: str = "") -> None:
        self.socket_path = str(socket_path)
        self.timeout_s = timeout_s
        self.name = name or self.socket_path

    async def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        frame = {"proto": PROTO, "id": str(uuid.uuid4()), "op": op, "args": args}
        try:
            async with asyncio.timeout(self.timeout_s):
                reader, writer = await asyncio.open_unix_connection(
                    self.socket_path, limit=MAX_REPLY_BYTES
                )
                try:
                    writer.write(json.dumps(frame).encode() + b"\n")
                    await writer.drain()
                    line = await reader.readline()
                finally:
                    writer.close()
        except TimeoutError as exc:
            raise BrokerUnavailable(f"{self.name}: no answer to {op} in {self.timeout_s}s") from exc
        except (OSError, ValueError) as exc:
            # ValueError: a reply line past MAX_REPLY_BYTES.
            raise BrokerUnavailable(f"{self.name}: {op}: {exc}") from exc
        if not line:
            raise BrokerUnavailable(f"{self.name}: {op}: the broker closed the connection")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise BrokerUnavailable(f"{self.name}: {op}: the reply is not JSON") from exc
        if not isinstance(reply, dict):
            raise BrokerUnavailable(f"{self.name}: {op}: the reply is not an object")
        if not reply.get("ok"):
            raise BrokerError(op, str(reply.get("error", "error")))
        result = reply.get("result")
        if not isinstance(result, dict):
            raise BrokerUnavailable(f"{self.name}: {op}: the result is not an object")
        return result

    async def mint(
        self,
        dev_name: str,
        crew: str,
        current_token_id: str | None,
        *,
        legend: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """`{account, token, token_id}`. Only `Registry.register` may call this (R-30).

        `current_token_id` is the token the daemon holds for this account on this server, or
        None. The broker revokes the account's other tokens, so one minted by a call whose
        reply timed out here is not left live (#41). `legend` is the agent's `project_dir` and
        `in_scope` for the broker's agent legend (#231).
        """
        args = self._minting(dev_name, crew, current_token_id)
        return await self.call("mint", {**args, **(legend or {})})

    async def remint(self, dev_name: str, crew: str, current_token_id: str) -> dict[str, Any]:
        return await self.call("remint", self._minting(dev_name, crew, current_token_id))

    def _minting(self, dev_name: str, crew: str, current_token_id: str | None) -> dict[str, Any]:
        # `deadline`: once this call has timed out here, the broker refuses it rather than
        # mint late, after a newer call, and supersede the token that call swapped in (#41).
        return {
            "dev_name": dev_name,
            "crew": crew,
            "current_token_id": current_token_id,
            "deadline": time.time() + self.timeout_s,
        }

    async def revoke(self, token_id: str, *, out_of_scope: bool = False) -> dict[str, Any]:
        """`out_of_scope` (#231) is sent only when true: a broker older than #231 ignores it."""
        args: dict[str, Any] = {"token_id": token_id}
        if out_of_scope:
            args["out_of_scope"] = True
        return await self.call("revoke", args)

    async def crew_info(self) -> dict[str, Any]:
        return await self.call("crew_info", {})


def broker_clients(config: ServersConfig) -> dict[BrokerKey, BrokerClient]:
    """One client per (crew, server_id) in `servers.toml`."""
    return {
        (b.crew, b.server_id): BrokerClient(
            b.broker_socket, config.broker_timeout_s, name=f"{b.crew}-{b.server_id}"
        )
        for b in config.broker_entries()
    }


class Adapter(Protocol):
    """The token hooks every backend adapter provides. Story 2.1 fills them in."""

    async def on_token_rotated(self, dev_name: str) -> None:
        """Reconnect `dev_name`'s connections with the token now in the store."""

    async def close_agent(self, dev_name: str) -> None:
        """Close `dev_name`'s websockets: it has left rotation."""

    async def close_server(self, dev_name: str, server_id: str) -> None:
        """Close `dev_name`'s connections to `server_id`: it no longer holds a token there."""


@dataclass
class ServerToken:
    account: str
    token: str
    token_id: str


@dataclass
class AgentTokens:
    dev_name: str
    crew: str
    last_register: datetime | None = None
    servers: dict[str, ServerToken] = field(default_factory=dict)
    # Tokens whose revoke failed, by server_id; retried before the next swap on that server, or,
    # on a server the agent is no longer allowed on, at each `register` that skips it (#117).
    pending_revoke: dict[str, list[str]] = field(default_factory=dict)
    # The agent's full forge repo from its last `register`, "" for none: every server it holds
    # a token on pools it for kiosk research (#139, D-112).
    repo: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "dev_name": self.dev_name,
            "crew": self.crew,
            "last_register": self.last_register.isoformat() if self.last_register else None,
            "servers": {
                sid: {"account": t.account, "token": t.token, "token_id": t.token_id}
                for sid, t in self.servers.items()
            },
            "pending_revoke": {sid: ids for sid, ids in self.pending_revoke.items() if ids},
            "repo": self.repo,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AgentTokens:
        last = data.get("last_register")
        return cls(
            dev_name=str(data["dev_name"]),
            crew=str(data["crew"]),
            last_register=datetime.fromisoformat(last) if isinstance(last, str) else None,
            servers={
                sid: ServerToken(str(t["account"]), str(t["token"]), str(t["token_id"]))
                for sid, t in dict(data.get("servers", {})).items()
            },
            pending_revoke={
                sid: [str(i) for i in ids]
                for sid, ids in dict(data.get("pending_revoke", {})).items()
            },
            repo=str(data.get("repo", "")),  # absent before #139
        )


def write_private_json(path: Path, data: Any) -> None:
    """Write `data` to `path` as JSON, mode 0600, atomically (R-08)."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


class TokenStore:
    """Every agent's tokens, keyed by (dev_name, server_id).

    The rotation sequence (`_swap`) is the one way a token changes: swap the new token in and
    persist it, call each adapter's `on_token_rotated`, and only then `revoke` the old one.
    `register` (with `mint`), the daily rotation and a 401 refresh (both with `remint`) all
    run it.
    """

    def __init__(
        self,
        root: Path,
        brokers: Mapping[BrokerKey, BrokerClient],
        adapters: Iterable[Adapter] = (),
    ) -> None:
        self.root = root
        self.brokers = brokers
        self.adapters: list[Adapter] = list(adapters)
        self._agents: dict[str, AgentTokens] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ledger_lock = asyncio.Lock()  # every agent's `_retire` shares `RETIRED_FILE`
        root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(root, DIR_MODE)

    # -- reading ---------------------------------------------------------------------------
    def path_for(self, dev_name: str) -> Path:
        return self.root / f"{dev_name}.json"

    def load(self) -> None:
        """Read every agent's file, so rotation continues across a daemon restart."""
        for path in sorted(self.root.iterdir()):
            if path.name.startswith(".") or path.suffix != ".json":
                continue
            try:
                agent = AgentTokens.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, KeyError, TypeError) as exc:
                log.error("skipping %s: %s", path, exc)
                continue
            self._agents[agent.dev_name] = agent

    def token_for(self, dev_name: str, server_id: str) -> str | None:
        agent = self._agents.get(dev_name)
        if agent is None or server_id not in agent.servers:
            return None
        return agent.servers[server_id].token

    def agent(self, dev_name: str) -> AgentTokens | None:
        return self._agents.get(dev_name)

    def held_token_id(self, dev_name: str, crew: str, server_id: str) -> str | None:
        """The token id `dev_name` holds in `crew` on `server_id`, if any."""
        agent = self._agents.get(dev_name)
        if agent is None or agent.crew != crew or server_id not in agent.servers:
            return None
        return agent.servers[server_id].token_id

    def agents(self) -> list[AgentTokens]:
        return [self._agents[name] for name in sorted(self._agents)]

    def lock(self, dev_name: str) -> asyncio.Lock:
        return self._locks.setdefault(dev_name, asyncio.Lock())

    # -- the rotation sequence -------------------------------------------------------------
    def _persist(self, agent: AgentTokens) -> None:
        write_private_json(self.path_for(agent.dev_name), agent.to_json())

    def _client(self, agent: AgentTokens, server_id: str) -> BrokerClient:
        client = self.brokers.get((agent.crew, server_id))
        if client is None:
            raise BrokerUnavailable(f"no broker for crew {agent.crew} on {server_id}")
        return client

    async def _swap(self, agent: AgentTokens, server_id: str, new: ServerToken) -> None:
        """Swap `new` in, reconnect, then revoke the previous token. The caller holds the lock."""
        old = agent.servers.get(server_id)
        agent.servers[server_id] = new
        if old is not None and old.token_id != new.token_id:
            # Recorded before the revoke is tried, so a crash mid-revoke still retries it.
            agent.pending_revoke.setdefault(server_id, []).append(old.token_id)
        self._persist(agent)
        for adapter in self.adapters:
            try:
                await adapter.on_token_rotated(agent.dev_name)
            except Exception:
                log.exception("on_token_rotated(%s) failed", agent.dev_name)
        await self._revoke_pending(agent, server_id)

    async def _revoke_pending(
        self, agent: AgentTokens, server_id: str, *, out_of_scope: bool = False
    ) -> None:
        """Revoke every token still owed on `server_id`. `out_of_scope`: the agent has left
        that server's allowlist, so the broker drops its legend row to the name (#231)."""
        pending = agent.pending_revoke.get(server_id, [])
        if not pending:
            return
        client = self._client(agent, server_id)
        left: list[str] = []
        for token_id in pending:
            try:
                await client.revoke(token_id, out_of_scope=out_of_scope)
            except BrokerError as exc:
                if exc.code != "not_found":  # not_found: the broker no longer holds it
                    log.error("revoke %s for %s: %s", token_id, agent.dev_name, exc)
                    left.append(token_id)
            except BrokerUnavailable as exc:
                log.error("revoke %s for %s: %s; will retry", token_id, agent.dev_name, exc)
                left.append(token_id)
        agent.pending_revoke[server_id] = left
        self._persist(agent)

    async def install_minted(
        self,
        dev_name: str,
        crew: str,
        server_id: str,
        minted: dict[str, Any],
        now: datetime,
        repo: str = "",
    ) -> None:
        """A `register`-driven `mint`'s token: record `last_register` and the agent's `repo`
        (#139), then the swap sequence, which persists both.

        The caller (`Registry.register`) holds `lock(dev_name)`.
        """
        agent = self._agents.get(dev_name)
        if agent is None or agent.crew != crew:
            if agent is not None:
                # The agent moved crews: its old crew's tokens are retired, not kept live.
                await self._retire(agent)
            agent = AgentTokens(dev_name=dev_name, crew=crew)
            self._agents[dev_name] = agent
        agent.last_register = now
        agent.repo = repo
        await self._swap(agent, server_id, _server_token(minted, minted.get("account")))

    async def retire_server(self, dev_name: str, crew: str, server_id: str) -> None:
        """Revoke the one token `dev_name` holds in `crew` on `server_id`, and take that server
        out of its rotation: the agent is no longer allowed there (#117). With no token held,
        it retries any revoke still pending there: no swap or rotation runs on that server
        again, so each `register` that skips it is what retries a failed one.

        The caller (`Registry.register`) holds `lock(dev_name)`.
        """
        agent = self._agents.get(dev_name)
        if agent is None or agent.crew != crew:
            return
        await self._retire_server_locked(
            agent, server_id, "no longer allowed there", out_of_scope=True
        )

    async def _retire_server_locked(
        self, agent: AgentTokens, server_id: str, why: str, *, out_of_scope: bool = False
    ) -> None:
        tok = agent.servers.pop(server_id, None)
        if tok is not None:
            log.info("retiring %s's token on %s: %s", agent.dev_name, server_id, why)
            # Recorded before the revoke is tried, as in `_swap`, so a crash mid-revoke keeps it.
            agent.pending_revoke.setdefault(server_id, []).append(tok.token_id)
            self._persist(agent)
            # Closed before the revoke, so no stream is left reconnecting on a dead token (#155).
            for adapter in self.adapters:
                try:
                    await adapter.close_server(agent.dev_name, server_id)
                except Exception:
                    log.exception("close_server(%s, %s) failed", agent.dev_name, server_id)
        elif not agent.pending_revoke.get(server_id):
            return
        # Off the allowlist, each retry comes back here from the next `register` that skips
        # this server, so a failed revoke still carries the scope change.
        await self._revoke_pending(agent, server_id, out_of_scope=out_of_scope)

    async def rotate(self, dev_name: str, server_id: str) -> None:
        """The daily rotation for one agent on one server: remint, swap, reconnect, revoke."""
        async with self.lock(dev_name):
            await self._remint_locked(dev_name, server_id)

    async def _remint_locked(self, dev_name: str, server_id: str) -> None:
        agent = self._agents.get(dev_name)
        if agent is None or server_id not in agent.servers:
            raise KeyError(f"{dev_name} holds no token on {server_id}")
        client = self._client(agent, server_id)
        held = agent.servers[server_id]
        try:
            result = await client.remint(dev_name, agent.crew, held.token_id)
        except BrokerError as exc:
            if exc.code == "deactivated":
                # Pruned, or `verify-install`'s canary `deactivate` (#127): no remint succeeds
                # until a `register` mints again. Drop the token, or every reconnect and daily
                # rotation tries the dead login against the server again.
                await self._retire_server_locked(agent, server_id, "its account is deactivated")
            raise
        await self._swap(agent, server_id, _server_token(result, held.account))

    async def refresh(self, dev_name: str, server_id: str, failed_token: str) -> None:
        """A 401 on `failed_token`: the rotation sequence with `remint`, never `mint` (R-30).

        If another request already refreshed past `failed_token`, nothing more is done.
        """
        async with self.lock(dev_name):
            if self.token_for(dev_name, server_id) != failed_token:
                return
            log.info("401 for %s on %s: reminting", dev_name, server_id)
            await self._remint_locked(dev_name, server_id)

    async def call_with_refresh(
        self, dev_name: str, server_id: str, request: Callable[[str], Awaitable[T]]
    ) -> T:
        """Run `request(token)`; on `Unauthorized`, refresh the token and retry once."""
        token = self.token_for(dev_name, server_id)
        if token is None:
            raise KeyError(f"{dev_name} holds no token on {server_id}")
        try:
            return await request(token)
        except Unauthorized:
            await self.refresh(dev_name, server_id, token)
        retry = self.token_for(dev_name, server_id)
        if retry is None:
            raise KeyError(f"{dev_name} holds no token on {server_id}")
        return await request(retry)

    async def drop(self, dev_name: str) -> None:
        """Take an agent out of the store and rotation, revoking what it held."""
        async with self.lock(dev_name):
            await self._drop_locked(dev_name)

    async def drop_if_idle(self, dev_name: str, cutoff: datetime) -> bool:
        """Drop `dev_name` only if, under its lock, it still has no `register` since `cutoff`.

        The rotation's idle check runs outside the lock, so a `register` can land between the
        check and the drop; re-checking here keeps that agent's fresh token and file (R-30).
        """
        async with self.lock(dev_name):
            agent = self._agents.get(dev_name)
            if agent is None:
                return False
            if agent.last_register is not None and agent.last_register >= cutoff:
                return False
            await self._drop_locked(dev_name)
            return True

    async def _drop_locked(self, dev_name: str) -> None:
        agent = self._agents.pop(dev_name, None)
        if agent is None:
            return
        await self._retire(agent)
        self.path_for(dev_name).unlink(missing_ok=True)

    async def _retire(self, agent: AgentTokens) -> None:
        """Revoke every token `agent` holds or still owes a revoke for. Each goes into the
        ledger (`RETIRED_FILE`) before any revoke is tried, and stays there until it is
        revoked, so one the broker didn't revoke outlives the agent and its file (#151)."""
        for server_id, tok in list(agent.servers.items()):
            agent.pending_revoke.setdefault(server_id, []).append(tok.token_id)
        agent.servers.clear()
        async with self._ledger_lock:
            ledger = self._load_retired()
            for server_id, ids in agent.pending_revoke.items():
                owed = ledger.setdefault(_ledger_key(agent.crew, server_id), [])
                owed.extend(i for i in ids if i not in owed)
            self._save_retired(ledger)
            for server_id in list(agent.pending_revoke):
                key = _ledger_key(agent.crew, server_id)
                ledger[key] = await self._revoke_owed(agent.crew, server_id, ledger.get(key, []))
            self._save_retired(ledger)

    # -- retired tokens still owed a revoke (#151) -----------------------------------------
    def _load_retired(self) -> dict[str, list[str]]:
        path = self.root / RETIRED_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pending = data["pending"]
            return {str(k): [str(i) for i in v] for k, v in dict(pending).items()}
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, TypeError) as exc:
            # Unreadable: start over, loudly. Losing the ledger only loses revokes that are
            # owed, which a token's own expiry still bounds.
            log.error("%s is unreadable (%s): starting a new ledger", path, exc)
            return {}

    def _save_retired(self, ledger: dict[str, list[str]]) -> None:
        pending = {k: v for k, v in sorted(ledger.items()) if v}
        write_private_json(self.root / RETIRED_FILE, {"pending": pending})

    async def _revoke_owed(self, crew: str, server_id: str, owed: list[str]) -> list[str]:
        """Try to revoke each of `owed` through (crew, server_id)'s broker; the ones left."""
        client = self.brokers.get((crew, server_id))
        if client is None:
            if owed:
                log.error(
                    "no broker for crew %s on %s: %d revokes kept", crew, server_id, len(owed)
                )
            return list(owed)
        left: list[str] = []
        for token_id in owed:
            try:
                await client.revoke(token_id)
            except BrokerError as exc:
                if exc.code != "not_found":  # not_found: the broker no longer holds it
                    log.error("revoke retired %s (%s on %s): %s", token_id, crew, server_id, exc)
                    left.append(token_id)
            except BrokerUnavailable as exc:
                log.error("revoke retired %s (%s on %s): %s", token_id, crew, server_id, exc)
                left.append(token_id)
        return left

    async def retry_retired(self) -> int:
        """Retry every revoke the ledger still owes; the number still owed after it. Run at
        daemon start and before each daily rotation (#151)."""
        async with self._ledger_lock:
            ledger = self._load_retired()
            for key in list(ledger):
                crew, _, server_id = key.partition("/")
                ledger[key] = await self._revoke_owed(crew, server_id, ledger[key])
            self._save_retired(ledger)
            return sum(len(v) for v in ledger.values())


def _ledger_key(crew: str, server_id: str) -> str:
    return f"{crew}/{server_id}"


def _server_token(result: dict[str, Any], account: Any) -> ServerToken:
    token, token_id = result.get("token"), result.get("token_id")
    if not isinstance(token, str) or not token or not isinstance(token_id, str) or not token_id:
        raise BrokerUnavailable("the broker's reply has no token and token_id")
    return ServerToken(account=str(account or ""), token=token, token_id=token_id)


@dataclass
class RotationReport:
    rotated: list[tuple[str, str]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str, str]] = field(default_factory=list)


class Rotator:
    """The daemon's daily rotation (§4.8; R-05, CT-06)."""

    def __init__(
        self,
        store: TokenStore,
        *,
        idle_days: int = IDLE_DAYS,
        rate_per_s: float = ROTATIONS_PER_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store = store
        self.idle_days = idle_days
        self.interval_s = 1.0 / rate_per_s
        self.sleep = sleep

    async def run_daily(self, now: datetime) -> RotationReport:
        """Close and drop agents idle for `idle_days`; rotate the rest, one server at a time.

        Rotations start at most `rate_per_s` per second, so a host's fleet stays inside the
        server's per-user rate limit (CT-06).
        """
        report = RotationReport()
        try:
            owed = await self.store.retry_retired()
        except Exception:
            log.exception("rotation: retrying retired revokes failed")
        else:
            if owed:
                log.warning("rotation: %d retired tokens still owed a revoke", owed)
        cutoff = now - timedelta(days=self.idle_days)
        first = True
        for agent in self.store.agents():
            if agent.last_register is None or agent.last_register < cutoff:
                if not await self.store.drop_if_idle(agent.dev_name, cutoff):
                    # A register landed after the check: its token is fresh, so leave it.
                    log.info("rotation: %s registered meanwhile; kept", agent.dev_name)
                    continue
                for adapter in self.store.adapters:
                    try:
                        await adapter.close_agent(agent.dev_name)
                    except Exception:
                        log.exception("close_agent(%s) failed", agent.dev_name)
                report.dropped.append(agent.dev_name)
                log.info(
                    "rotation: dropped %s, last registered %s", agent.dev_name, agent.last_register
                )
                continue
            for server_id in sorted(agent.servers):
                if not first:
                    await self.sleep(self.interval_s)
                first = False
                try:
                    await self.store.rotate(agent.dev_name, server_id)
                except (BrokerError, BrokerUnavailable, KeyError) as exc:
                    log.error("rotation: %s on %s: %s", agent.dev_name, server_id, exc)
                    report.failed.append((agent.dev_name, server_id, str(exc)))
                    continue
                report.rotated.append((agent.dev_name, server_id))
        return report

    async def retry(self, failed: Iterable[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        """Rotate each failed (dev_name, server_id) again; return the ones still failing.

        An agent that has left the store meanwhile is done, not failed.
        """
        left: list[tuple[str, str, str]] = []
        first = True
        for dev_name, server_id, _ in failed:
            if not first:
                await self.sleep(self.interval_s)
            first = False
            agent = self.store.agent(dev_name)
            if agent is None or server_id not in agent.servers:
                continue
            try:
                await self.store.rotate(dev_name, server_id)
            except (BrokerError, BrokerUnavailable) as exc:
                log.error("rotation retry: %s on %s: %s", dev_name, server_id, exc)
                left.append((dev_name, server_id, str(exc)))
                continue
            except KeyError:
                continue
            log.info("rotation retry: rotated %s on %s", dev_name, server_id)
        return left


def retry_delays(first_s: float = RETRY_FIRST_S, cap_s: float = RETRY_MAX_S) -> Iterator[float]:
    """A failed rotation's retry waits: 5 min, doubling, capped at 1 h."""
    delay = first_s
    while True:
        yield min(delay, cap_s)
        delay = min(delay * 2, cap_s)
