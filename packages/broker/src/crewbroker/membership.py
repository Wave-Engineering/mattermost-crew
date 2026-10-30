"""Membership on mint (#132, D-109): the crew's team and default channels, by name.

The names come from the broker's own config file, which a person edits. Each name is a URL name
(`town-square`) or a display name (`Town Square`). `Membership.resolve` turns them into IDs when
the broker starts, logging each `name → id` at INFO, and `ensure` adds a minted bot to every
resolved target, the team first.

A URL name is tried first, for any entry that could be one. If there is none, the entry is
matched, ignoring case, against the display names the broker account can see: its own teams,
and for a channel, the team's public channels plus its own channels there (a private channel
is visible only to a member). Two or more matches is an error that names them.

A name that doesn't resolve never stops the broker, because a typo must not cost every agent
its token. It is logged at ERROR and reported in every mint's `membership.failed`. No match, an
ambiguous match or a 4xx other than 429 is final until the broker restarts with a corrected file.
Any other failure, such as a rate limit or the server being unreachable at startup, is retried on
the next mint.

The adds run after the token is minted and before the daemon has it, inside the daemon's
deadline for the call. A reply past that deadline is a token the daemon never receives, so
`ensure` stops adding once less than `DEADLINE_RESERVE_S` is left, and reports what it did not
reach as deferred. The next register repeats it. A single call that sits in `_call`'s 429
retries can still overrun; bounding that would need a deadline-aware `_call`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from crewbroker.mattermost import MattermostAPI, MattermostError, NotFound

log = logging.getLogger(__name__)

# What Mattermost allows in a URL name. An entry outside it, such as "Oak and Wave", can only
# be a display name, so it is never sent to a `/name/` endpoint.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CHANNEL_TYPES = {"O", "P"}  # public and private; never a DM or group message


def keep_channel(item: dict[str, Any]) -> bool:
    """A public or private channel that isn't archived: the only kind a name resolves to."""
    return item.get("type") in CHANNEL_TYPES and not item.get("delete_at")

# Time kept back before the caller's deadline for the reply to reach the daemon.
DEADLINE_RESERVE_S = 0.5
DEFERRED = "deferred: the caller's deadline is near"


class Unresolved(Exception):
    """No URL or display name matched, or more than one display name did. Final."""


@dataclass
class Target:
    kind: str  # "team" or "channel"
    name: str  # as configured
    id: str | None = None
    error: str = ""  # why it is unresolved
    final: bool = False  # not asked again until a restart

    def failure(self, error: str | None = None) -> dict[str, Any]:
        return {"name": self.name, "id": self.id, "error": error or self.error}


def _already_member(exc: MattermostError) -> bool:
    # The #132 probe saw 201 for an existing member; this is for a server that answers 400.
    return exc.error_id.endswith("save_member.exists.app_error")


def _match(
    target: Target,
    by_slug: Callable[[], dict[str, Any]],
    listings: list[Callable[[], list[dict[str, Any]]]],
    keep: Callable[[dict[str, Any]], bool] = lambda _: True,
) -> str:
    """The ID of the one team or channel whose URL name, else display name, is `target.name`."""
    if SLUG_RE.fullmatch(target.name):
        try:
            return str(by_slug()["id"])
        except NotFound:
            pass  # not a URL name: maybe a display name
    wanted = target.name.casefold()
    found: dict[str, dict[str, Any]] = {}
    for listing in listings:
        for item in listing():
            if keep(item) and str(item.get("display_name", "")).casefold() == wanted:
                found[str(item["id"])] = item
    if len(found) == 1:
        return next(iter(found))
    if not found:
        raise Unresolved(f"no {target.kind} has the URL name or display name {target.name!r}")
    candidates = ", ".join(
        f"{c.get('display_name')!r} ({c.get('name')}, {i})" for i, c in sorted(found.items())
    )
    raise Unresolved(f"{target.name!r} matches {len(found)} {target.kind}s: {candidates}")


class Membership:
    def __init__(self, client: MattermostAPI, team: str, channels: list[str]) -> None:
        self.client = client
        self.team = Target("team", team)
        self.channels = [Target("channel", name) for name in channels]
        self._lock = threading.Lock()

    def targets(self) -> list[Target]:
        return [self.team, *self.channels]

    def _pending(self) -> bool:
        return any(t.id is None and not t.final for t in self.targets())

    def resolve(self) -> None:
        """Look up each name not yet resolved, except those already found final."""
        with self._lock:
            team = self.team
            if team.id is None and not team.final:
                self._lookup(team, self._team)
            for channel in self.channels:
                if channel.id is not None or channel.final:
                    continue
                team_id = team.id
                if team_id is None:
                    channel.error = f"its team {team.name!r} is unresolved"
                    channel.final = team.final
                    continue
                self._lookup(channel, partial(self._channel, channel, team_id))

    def _team(self) -> str:
        by_slug = partial(self.client.get_team_by_name, self.team.name)
        return _match(self.team, by_slug, [self.client.list_my_teams])

    def _channel(self, channel: Target, team_id: str) -> str:
        return _match(
            channel,
            partial(self.client.get_channel_by_name, team_id, channel.name),
            [
                partial(self.client.list_team_channels, team_id),
                partial(self.client.list_my_team_channels, team_id),
            ],
            keep=keep_channel,
        )

    def _lookup(self, target: Target, resolve: Callable[[], str]) -> None:
        try:
            target.id = resolve()
        except Unresolved as exc:
            target.final = True
            target.error = str(exc)
            if target.kind == "channel":
                target.error += " (a private channel is visible only if the broker is a member)"
        except MattermostError as exc:
            status = exc.status
            target.final = status is not None and 400 <= status < 500 and status != 429
            target.error = f"{target.kind} {target.name!r} did not resolve: {exc}"
        else:
            target.error = ""
            log.info("membership: %s %s → %s", target.kind, target.name, target.id)
            return
        log.error("membership: %s", target.error)

    def ensure(
        self,
        account: str,
        bot_id: str,
        deadline: float | None = None,
        now: Callable[[], float] = time.time,
    ) -> dict[str, list[Any]]:
        """Add bot `bot_id` to the team, then to each default channel in order.

        Idempotent: an existing member counts as joined. A failed add is logged at ERROR and
        reported, never raised, so the mint it follows still returns its token. A failed team
        add skips the channels, which would fail the same way. With `deadline` (epoch seconds),
        nothing is looked up or added once less than `DEADLINE_RESERVE_S` is left: the rest is
        reported as deferred and logged at WARNING. The caller checks the owner.
        """

        def late() -> bool:
            return deadline is not None and deadline - now() < DEADLINE_RESERVE_S

        if self._pending() and not late():
            self.resolve()
        joined: list[Any] = []
        failed: list[Any] = []

        def add(target: Target, call: Callable[[str, str], None]) -> bool:
            if target.id is None and target.final:
                failed.append(target.failure())
                return False
            if late():
                failed.append(target.failure(DEFERRED))
                return False
            if target.id is None:
                failed.append(target.failure())
                return False
            try:
                call(target.id, bot_id)
            except MattermostError as exc:
                if not _already_member(exc):
                    log.error(
                        "membership: adding %s (%s) to %s %s (%s) failed: %s",
                        account,
                        bot_id,
                        target.kind,
                        target.name,
                        target.id,
                        exc,
                    )
                    failed.append(target.failure(str(exc)))
                    return False
            joined.append(target.name)
            return True

        if add(self.team, self.client.add_team_member):
            for channel in self.channels:
                add(channel, self.client.add_channel_member)
        elif failed[-1]["error"] == DEFERRED:
            failed.extend(c.failure(DEFERRED) for c in self.channels)
        elif self.team.id is None:  # unresolved: each channel says so for itself
            failed.extend(c.failure() for c in self.channels)
        else:
            skipped = f"skipped: the team {self.team.name!r} add failed"
            failed.extend(c.failure(skipped) for c in self.channels)
        deferred = [f["name"] for f in failed if f["error"] == DEFERRED]
        if deferred:
            log.warning(
                "membership: %s (%s): deferred to its next register, the caller's deadline is "
                "near: %s",
                account,
                bot_id,
                ", ".join(deferred),
            )
        return {"joined": joined, "failed": failed}
