"""Agent registration (Dev Spec §4.2, §5.3; D-055).

`register{dev_name, dev_team, dev_avatar, crew, session_id}` keeps one registration per
Dev-Name and takes the declared name at its word: agents on one host are one trust boundary
(D-055). `dev_team` and `dev_avatar` are self-declared too; a re-register overwrites both, and
with Discord enabled they go to the Discord adapter's `set_identity` for the signature (D-103).
The daemon never reads a path on the agent's side (§5.3); everything it knows came over the
socket.

For each Mattermost server the crew has a broker for, and whose allowlist admits the agent's
`project_key` or Dev-Name (`crewchat.allowlist`, #117), `register` asks that broker to `mint`.
It is the **only** caller of `BrokerClient.mint` (R-30). For an agent that already holds a
token there (the shim's daily re-register), the new token goes through the rotation sequence:
swap, `on_token_rotated`, then `revoke` the previous token, so none is left live. After each
`mint`, that broker's `crew_info` and `switch_state` are refreshed, so the new bot account is
in `minted_bot_ids` before its first post (§5.9).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from crewchat.allowlist import allowed
from crewchat.config import ServersConfig
from crewchat.tokens import (
    BrokerClient,
    BrokerError,
    BrokerKey,
    BrokerUnavailable,
    TokenStore,
)

if TYPE_CHECKING:
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
    # The agent's Dev-Team (BJ's direction scope, S-11), for `@<dev-team>` addressing (R-17).
    # `register` carries it, self-declared from the shim's identity file (D-103).
    dev_team: str = ""
    # The agent's avatar: display only, for its Discord signature (D-103). Never read by
    # addressing, membership or any trust decision.
    dev_avatar: str = ""

    def result(self) -> dict[str, Any]:
        """What `register` answers. Never a token (R-08)."""
        return {"account": self.account, "backends": self.backends, "needs_list": self.needs_list}


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
    ) -> None:
        self.config = config
        self.brokers = brokers
        self.store = store
        self.cache = cache
        self.clock = clock
        # The Discord adapter, when Discord is enabled: `set_identity` after each register.
        self.identity_sink = identity_sink
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
                client = self.brokers[(crew, server.id)]
                try:
                    held = self.store.held_token_id(dev_name, crew, server.id)
                    minted = await client.mint(dev_name, crew, held)
                except (BrokerError, BrokerUnavailable) as exc:
                    log.error("register %s on %s: %s", account, server.id, exc)
                    unavailable.append(server.id)
                    if isinstance(exc, BrokerError) and exc.code == "squatted":
                        squatted.append(server.id)
                    continue
                if minted.get("account") != account:
                    log.error(
                        "register %s on %s: the broker named the account %r",
                        account,
                        server.id,
                        minted.get("account"),
                    )
                    unavailable.append(server.id)
                    continue
                await self.store.install_minted(dev_name, crew, server.id, minted, now)
                await self.cache.refresh(crew, server.id)
                backends.append(mattermost_backend(server.id))

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
            dev_team=dev_team,
            dev_avatar=dev_avatar,
        )
        self._registrations[dev_name] = reg
        if self.config.discord_enabled and self.identity_sink is not None:
            # So the agent's Discord signature carries its avatar and team (D-103).
            self.identity_sink.set_identity(dev_name, avatar=dev_avatar, team=dev_team)
        log.info("registered %s (session %s): %s", account, session_id, ", ".join(backends))
        return reg
