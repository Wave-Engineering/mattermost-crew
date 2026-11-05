"""Agent registration (Dev Spec §4.2, §5.3; D-055).

`register{dev_name, dev_team, dev_avatar, crew, session_id}` keeps one registration per
Dev-Name and takes the declared name at its word: agents on one host are one trust boundary
(D-055). `dev_team` and `dev_avatar` are self-declared too; a re-register overwrites both, and
with Discord enabled they go to the Discord adapter's `set_identity` for the signature (D-103).
The daemon never reads a path on the agent's side (§5.3); everything it knows came over the
socket.

For each Mattermost server the crew has a broker for, and whose allowlist admits the agent's
`project_key` or Dev-Name (`crewchat.allowlist`, #117), `register` asks that broker to `mint`.
It is the **only** caller of `BrokerClient.mint` (R-30), through `_mint_on`, with
`retry_unavailable` finishing a register's failed servers (#186). For an agent that already
holds a token there (the shim's daily re-register), the new token goes through the rotation
sequence: swap, `on_token_rotated`, then `revoke` the previous token, so none is left live.
After each `mint`, that broker's `crew_info` and `switch_state` are refreshed, so the new bot
account is in `minted_bot_ids` before its first post (§5.9).

The agent's `repo` (its full forge path, #139) is recorded beside its tokens, and every
`register` refreshes the kiosk's corpus pool manifests (`crewchat.corpus_pool`, D-112).

A server whose broker didn't mint is in the registration's `unavailable`, and `register`
still succeeds on the others, so the shim won't register again for a day. `retry_unavailable`
mints on just those servers again (#186), never on a squatted one, and `crewchat.retry` calls
it in the background.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from crewchat.allowlist import allowed, in_legend_scope
from crewchat.config import ServersConfig
from crewchat.tokens import (
    BrokerClient,
    BrokerError,
    BrokerKey,
    BrokerUnavailable,
    TokenStore,
)

if TYPE_CHECKING:
    from crewchat.corpus_pool import PoolManifest
    from crewchat.filters import SwitchCache

ACCOUNT_NAME_MAX = 64  # Mattermost's UserNameMaxLength (CT-05)
# A Dev-Name is also a file name under /var/lib/crewchat/tokens, so no leading dot.
DEV_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CREW_RE = re.compile(r"^[a-z0-9._-]+$")

log = logging.getLogger("crewchat.registry")

Clock = Callable[[], datetime]


class IdentitySink(Protocol):
    """What `register` tells about an agent's signature: `DiscordBackend.set_identity`."""

    def set_identity(self, dev_name: str, *, avatar: str = "", team: str = "") -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class RegisterError(Exception):
    """`register` refused. `code` goes to the caller as the error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def mattermost_backend(server_id: str) -> str:
    """A Mattermost server's backend name, the one form on the socket and in files (§5.4)."""
    return f"mattermost:{server_id}"


def account_name(dev_name: str, crew: str) -> str:
    """`<dev_name>.<crew>`, as the broker names it: at most 64 of a-z 0-9 . _ - (CT-05)."""
    if not isinstance(dev_name, str) or not DEV_NAME_RE.fullmatch(dev_name):
        raise ValueError(f"dev_name {dev_name!r} must be a-z0-9._- and not start with a dot")
    if not isinstance(crew, str) or not CREW_RE.fullmatch(crew):
        raise ValueError(f"crew {crew!r} has characters outside a-z0-9._-")
    name = f"{dev_name}.{crew}"
    if len(name) > ACCOUNT_NAME_MAX:
        raise ValueError(f"account name {name!r} is longer than {ACCOUNT_NAME_MAX} characters")
    return name


@dataclass
class Registration:
    dev_name: str
    crew: str
    session_id: str
    account: str
    # Backend names in the one form (§5.4): `mattermost:<server_id>` per server, `discord`.
    backends: list[str]
    needs_list: str
    registered_at: datetime
    unavailable: list[str] = field(default_factory=list)
    # The servers in `unavailable` whose broker refused the name itself (#186).
    squatted: list[str] = field(default_factory=list)
    # The agent's Dev-Team (BJ's direction scope, S-11), for `@<dev-team>` addressing (R-17).
    # `register` carries it, self-declared from the shim's identity file (D-103).
    dev_team: str = ""
    # The agent's avatar: display only, for its Discord signature (D-103). Never read by
    # addressing, membership or any trust decision.
    dev_avatar: str = ""
    # The agent's full forge path (#139), for a retry's `install_minted` (#186).
    repo: str = ""
    # The shim's `project_key` and `project_dir`, for a retry's legend fields (#231).
    project_key: str = ""
    project_dir: str = ""

    @property
    def retryable(self) -> list[str]:
        """The servers worth another mint (#186): every `unavailable` one not `squatted`."""
        return [s for s in self.unavailable if s not in self.squatted]

    def result(self) -> dict[str, Any]:
        """What `register` answers. Never a token (R-08)."""
        # A copy: a retry adds to `backends` later (#186), and an answer never changes.
        return {
            "account": self.account,
            "backends": list(self.backends),
            "needs_list": self.needs_list,
        }


class Registry:
    def __init__(
        self,
        config: ServersConfig,
        brokers: Mapping[BrokerKey, BrokerClient],
        store: TokenStore,
        cache: SwitchCache,
        *,
        clock: Clock = utc_now,
        identity_sink: IdentitySink | None = None,
        pool: PoolManifest | None = None,
    ) -> None:
        self.config = config
        self.brokers = brokers
        self.store = store
        self.cache = cache
        self.clock = clock
        # The Discord adapter, when Discord is enabled: `set_identity` after each register.
        self.identity_sink = identity_sink
        # The kiosk's corpus pool manifests (#139): refreshed after each register.
        self.pool = pool
        self._registrations: dict[str, Registration] = {}

    def get(self, dev_name: str) -> Registration | None:
        return self._registrations.get(dev_name)

    def agents_on(self, crew: str, server_id: str) -> list[str]:
        """The Dev-Names registered in `crew` with a backend on Mattermost `server_id`."""
        backend = mattermost_backend(server_id)
        return sorted(
            r.dev_name
            for r in self._registrations.values()
            if r.crew == crew and backend in r.backends
        )

    async def register(
        self,
        dev_name: str,
        crew: str | None,
        session_id: str,
        *,
        dev_team: str = "",
        dev_avatar: str = "",
        project_key: str = "",
        repo: str = "",
        project_dir: str = "",
    ) -> Registration:
        crew = crew or self.config.default_crew
        try:
            account = account_name(dev_name, crew)
        except ValueError as exc:
            raise RegisterError("bad_request", str(exc)) from exc
        if not isinstance(session_id, str) or not session_id:
            raise RegisterError("bad_request", "session_id is a required string")
        entry = self.config.crews.get(crew)
        if entry is None:
            raise RegisterError("bad_request", f"crew {crew!r} has no [crews.{crew}] table")

        backends: list[str] = []
        unavailable: list[str] = []
        squatted: list[str] = []
        async with self.store.lock(dev_name):
            now = self.clock()
            for server in self.config.mattermost:
                if server.id not in entry.brokers:
                    continue
                if not allowed(entry.brokers[server.id], project_key, dev_name):
                    # Not `unavailable`: a skipped server never fails the register (#117).
                    log.info("register %s: not on %s's allowlist", account, server.id)
                    # Revokes a token still held there, or retries one whose revoke failed.
                    await self.store.retire_server(dev_name, crew, server.id)
                    continue
                legend = self._legend(crew, server.id, project_key, dev_name, project_dir)
                why = await self._mint_on(dev_name, crew, account, server.id, now, repo, legend)
                if why is None:
                    backends.append(mattermost_backend(server.id))
                    continue
                unavailable.append(server.id)
                if why == "squatted":
                    squatted.append(server.id)

        if self.pool is not None:
            # After the lock: a mint or a retire may have changed a server's pool.
            self.pool.refresh()
        if unavailable and not backends:
            if squatted == unavailable:
                # Every broker refused the name itself: a retry won't help, so say why.
                raise RegisterError(
                    "squatted", f"{account} is held by another owner on {', '.join(squatted)}"
                )
            raise RegisterError(
                "broker_unavailable", f"no broker minted for {account}: {', '.join(unavailable)}"
            )
        if self.config.discord_enabled:
            backends.append("discord")
        reg = Registration(
            dev_name=dev_name,
            crew=crew,
            session_id=session_id,
            account=account,
            backends=backends,
            needs_list=entry.needs_list,
            registered_at=now,
            unavailable=unavailable,
            squatted=squatted,
            dev_team=dev_team,
            dev_avatar=dev_avatar,
            repo=repo,
            project_key=project_key,
            project_dir=project_dir,
        )
        self._registrations[dev_name] = reg
        if self.config.discord_enabled and self.identity_sink is not None:
            # So the agent's Discord signature carries its avatar and team (D-103).
            self.identity_sink.set_identity(dev_name, avatar=dev_avatar, team=dev_team)
        log.info("registered %s (session %s): %s", account, session_id, ", ".join(backends))
        return reg

    async def retry_unavailable(self, reg: Registration) -> list[str]:
        """Mint again on each server in `reg.retryable` (#186), and never on one that already
        worked: a mint there would rotate a good token. Each server that mints moves from
        `reg.unavailable` to `reg.backends`. The backends gained; none once a newer `register`
        has replaced `reg`."""
        gained: list[str] = []
        async with self.store.lock(reg.dev_name):
            for server_id in reg.retryable:
                if self._registrations.get(reg.dev_name) is not reg:
                    break
                legend = self._legend(
                    reg.crew, server_id, reg.project_key, reg.dev_name, reg.project_dir
                )
                why = await self._mint_on(
                    reg.dev_name, reg.crew, reg.account, server_id, self.clock(), reg.repo, legend
                )
                if why == "squatted":
                    reg.squatted.append(server_id)
                if why is not None:
                    continue
                reg.unavailable.remove(server_id)
                backend = mattermost_backend(server_id)
                # Before `discord`, in the order `register` lists them.
                at = reg.backends.index("discord") if "discord" in reg.backends else None
                reg.backends.insert(len(reg.backends) if at is None else at, backend)
                gained.append(backend)
        if gained:
            if self.pool is not None:
                self.pool.refresh()
            log.info("registered %s on %s, on a retry", reg.account, ", ".join(gained))
        return gained

    def _legend(
        self, crew: str, server_id: str, project_key: str, dev_name: str, project_dir: str
    ) -> dict[str, Any]:
        """The `mint` frame's agent-legend fields for one server (#231)."""
        entry = self.config.crews[crew].brokers[server_id]
        return {
            "project_dir": project_dir,
            "in_scope": in_legend_scope(entry, project_key, dev_name),
        }

    async def _mint_on(
        self,
        dev_name: str,
        crew: str,
        account: str,
        server_id: str,
        now: datetime,
        repo: str,
        legend: dict[str, Any],
    ) -> str | None:
        """Mint `dev_name`'s token on `server_id` and install it. None, or why it failed:
        `"squatted"` (a retry won't help) or `"unavailable"`. The caller holds
        `store.lock(dev_name)`."""
        client = self.brokers[(crew, server_id)]
        try:
            held = self.store.held_token_id(dev_name, crew, server_id)
            minted = await client.mint(dev_name, crew, held, legend=legend)
        except (BrokerError, BrokerUnavailable) as exc:
            log.error("register %s on %s: %s", account, server_id, exc)
            if isinstance(exc, BrokerError) and exc.code == "squatted":
                return "squatted"
            return "unavailable"
        if minted.get("account") != account:
            log.error(
                "register %s on %s: the broker named the account %r",
                account,
                server_id,
                minted.get("account"),
            )
            return "unavailable"
        membership = minted.get("membership")
        if isinstance(membership, dict) and membership.get("failed"):
            # The broker logged each at ERROR; the token is good, so register goes on.
            log.warning(
                "register %s on %s: membership adds failed: %s",
                account,
                server_id,
                membership["failed"],
            )
        await self.store.install_minted(dev_name, crew, server_id, minted, now, repo)
        await self.cache.refresh(crew, server_id)
        return None
