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

Tagged channels (#167). A channel joins the crew by carrying its tag (`crew-<crew>` by default)
in its header or purpose, as a whole token, ignoring case. `reconcile` runs on the broker's loop:
each pass lists the team's channels once, then adds every bot the broker owns to the team, each
default channel and each tagged channel. The broker account joins a tagged public channel it is
not in first, because a `team_user` can add its bots only to a channel it is in (#132's probe);
a private one is visible only once a person has added the broker account. It joins only in a
pass with bots to add. A mint adds its bot to the tagged channels the last pass published, as
well as the default channels; a channel is published only once the broker is in it.

The server answers 201 for an existing member as for a new one, so a pass can't tell them
apart. Each (bot, target) pair an add has succeeded for is remembered in memory and not sent
again until the broker restarts: the first pass after a start re-sends every pair, and logs each
as added. Nothing is ever removed, and a bot that leaves a channel, or is removed from one, is
not added back while the broker runs.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
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


def tag_pattern(tag: str) -> re.Pattern[str]:
    """`tag` as a whole token, ignoring case: `crew-oaw` matches, `crew-oawx` and `xcrew-oaw`
    do not (#167)."""
    # A dot that joins name characters is part of the token, as crew names may hold dots
    # (`crew-bj.baker` is not `crew-bj`'s; `crew-x.crew-oaw` is not `crew-oaw`'s), but a
    # sentence's closing dot is not (`… crew-oaw.` is). Two lookbehinds: `re` needs each fixed.
    return re.compile(
        rf"(?<![\w-])(?<!\w\.){re.escape(tag)}(?![\w-]|\.\w)", re.IGNORECASE
    )


def tagged(item: dict[str, Any], pattern: re.Pattern[str]) -> bool:
    """A channel a name could resolve to, whose header or purpose carries the tag."""
    text = f"{item.get('header') or ''}\n{item.get('purpose') or ''}"
    return keep_channel(item) and pattern.search(text) is not None


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
    def __init__(
        self,
        client: MattermostAPI,
        team: str,
        channels: list[str],
        tag: str = "",
        broker_user_id: str = "",
    ) -> None:
        self.client = client
        self.team = Target("team", team)
        self.channels = [Target("channel", name) for name in channels]
        self.tag = tag
        self.broker_user_id = broker_user_id
        self._lock = threading.Lock()
        # The tagged channels the last scan found (#167), and of those, the public ones the
        # broker account is not in, which it joins before adding its bots.
        self.tagged: list[Target] = []
        self._join_first: dict[str, Target] = {}
        # (bot id, target id) pairs an add has succeeded for since the broker started.
        self._joined: set[tuple[str, str]] = set()

    def targets(self) -> list[Target]:
        return [self.team, *self.channels]

    def channel_targets(self) -> list[Target]:
        """The default channels, in order, then each tagged channel that isn't one of them."""
        with self._lock:
            found = list(self.tagged)
        listed = {c.id for c in self.channels if c.id is not None}
        return [*self.channels, *(t for t in found if t.id not in listed)]

    def _remember(self, bot_id: str, target_id: str) -> None:
        with self._lock:
            self._joined.add((bot_id, target_id))

    def _known(self, bot_id: str, target_id: str) -> bool:
        with self._lock:
            return (bot_id, target_id) in self._joined

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
            self._remember(bot_id, target.id)
            joined.append(target.name)
            return True

        channels = self.channel_targets()
        if add(self.team, self.client.add_team_member):
            for channel in channels:
                add(channel, self.client.add_channel_member)
        elif failed[-1]["error"] == DEFERRED:
            failed.extend(c.failure(DEFERRED) for c in channels)
        elif self.team.id is None:  # unresolved: each channel says so for itself
            failed.extend(c.failure() for c in channels)
        else:
            skipped = f"skipped: the team {self.team.name!r} add failed"
            failed.extend(c.failure(skipped) for c in channels)
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

    # -- tagged channels and the reconcile pass (#167) --------------------------------------
    def scan(self) -> bool:
        """List the team's channels, once each way, and keep those carrying the tag.

        False, with the last scan's channels kept, when there is no tag, the team is
        unresolved, or a listing fails (logged at ERROR).
        """
        team_id = self.team.id
        if not self.tag or team_id is None:
            return False
        found: dict[str, dict[str, Any]] = {}
        mine: set[str] = set()
        try:
            for item in self.client.list_team_channels(team_id):
                found.setdefault(str(item["id"]), item)
            for item in self.client.list_my_team_channels(team_id):
                found[str(item["id"])] = item
                mine.add(str(item["id"]))
        except MattermostError as exc:
            log.error("membership: reconcile: listing the team's channels failed: %s", exc)
            return False
        pattern = tag_pattern(self.tag)
        hits = sorted(
            (item for item in found.values() if tagged(item, pattern)),
            key=lambda item: (str(item.get("name")), str(item["id"])),
        )
        # Only channels the broker is in are published: a mint never adds its bot to one it
        # would be refused. A public one it isn't in waits for `join_tagged`.
        targets = {
            str(i["id"]): Target("channel", str(i.get("name") or i["id"]), str(i["id"]))
            for i in hits
        }
        with self._lock:
            self.tagged = [t for cid, t in targets.items() if cid in mine]
            self._join_first = {
                cid: t
                for cid, t in targets.items()
                if cid not in mine and found[cid].get("type") == "O"
            }
        return True

    def refresh(self) -> None:
        """Retry any name not yet resolved, then scan for tagged channels."""
        if self._pending():
            self.resolve()
        self.scan()

    def join_tagged(self) -> int:
        """Join each tagged public channel the broker account is not in, then publish it.

        A `team_user` adds its bots only to a channel it is in (#132's probe). A channel it
        can't join is logged at WARNING, once per pass, and stays unpublished. Returns how many
        it published.
        """
        with self._lock:
            pending = list(self._join_first.values())
        published = 0
        for channel in pending:
            assert channel.id is not None
            try:
                self.client.add_channel_member(channel.id, self.broker_user_id)
            except MattermostError as exc:
                if not _already_member(exc):
                    log.warning(
                        "membership: reconcile: tagged channel %s (%s) refused the broker: "
                        "the broker account can't join it: %s",
                        channel.name,
                        channel.id,
                        exc,
                    )
                    continue
            with self._lock:
                self._join_first.pop(channel.id, None)
                self.tagged = sorted([*self.tagged, channel], key=lambda t: (t.name, str(t.id)))
            published += 1
            log.info("membership: reconcile: the broker joined %s", channel.name)
        return published

    def reconcile(
        self,
        bot_ids: list[str],
        names: Mapping[str, str] | None = None,
        *,
        refresh: bool = True,
    ) -> dict[str, list[Any]]:
        """Add each bot in `bot_ids` to the team, then to every channel target.

        `names` gives each bot's account name for the logs. With `refresh` (the default), it
        first scans and, when there are bots to add, joins the tagged public channels; a caller
        that has done both passes False. Never removes anyone. A failed add is reported and
        never raised: at ERROR, or at WARNING once per pass for a tagged channel that refuses
        the broker (403), where the rest of the pass's adds to it are skipped. No deadline
        applies: no mint is waiting.
        """
        if refresh:
            self.refresh()
            if bot_ids:  # the broker never joins a channel for no bot
                self.join_tagged()
        names = names or {}
        channels = self.channel_targets()
        with self._lock:
            tagged_ids = {t.id for t in self.tagged}
        refused: dict[str, str] = {}  # tagged channel id -> why, warned once below
        added: list[Any] = []
        failed: list[Any] = []

        def add(target: Target, call: Callable[[str, str], None], bot_id: str) -> bool:
            account = names.get(bot_id, bot_id)
            if target.id is None:
                failed.append({**target.failure(), "account": account})
                return False
            if target.id in refused:
                failed.append({**target.failure(refused[target.id]), "account": account})
                return False
            if self._known(bot_id, target.id):
                return True
            try:
                call(target.id, bot_id)
            except MattermostError as exc:
                if not _already_member(exc):
                    if target.id in tagged_ids and exc.status == 403:
                        refused[target.id] = str(exc)
                    else:
                        log.error(
                            "membership: reconcile: adding %s to %s %s (%s) failed: %s",
                            account,
                            target.kind,
                            target.name,
                            target.id,
                            exc,
                        )
                    failed.append({**target.failure(str(exc)), "account": account})
                    return False
            else:
                added.append({"account": account, "target": target.name})
                log.info("membership: reconcile: added %s to %s", account, target.name)
            self._remember(bot_id, target.id)
            return True

        for bot_id in bot_ids:
            if add(self.team, self.client.add_team_member, bot_id):
                for channel in channels:
                    add(channel, self.client.add_channel_member, bot_id)
            elif self.team.id is not None:
                skipped = f"skipped: the team {self.team.name!r} add failed"
                account = names.get(bot_id, bot_id)
                failed.extend({**c.failure(skipped), "account": account} for c in channels)
        for target in channels:
            if target.id in refused:
                log.warning(
                    "membership: reconcile: tagged channel %s (%s) refused the broker: %s",
                    target.name,
                    target.id,
                    refused[target.id],
                )
        log.info(
            "membership: reconcile: %d bots, %d channels (%d tagged), %d added, %d failed",
            len(bot_ids),
            len(channels),
            len(tagged_ids),
            len(added),
            len(failed),
        )
        return {"added": added, "failed": failed}
