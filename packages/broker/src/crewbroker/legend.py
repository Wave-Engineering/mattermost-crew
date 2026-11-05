"""The agent legend (#231): one pinned post in the crew's `oaw` channel, naming each agent's bot
account and the last element of the directory it works in.

The broker owns the post. Each reconcile pass renders the legend from `accounts.json` and
compares it with the text it last posted: unchanged, it makes no call at all; changed, it edits
the post in place. A burst of registrations between two passes is one edit. With no post, or
when the stored one is gone (404) or refuses the edit (403, as under a server's post-edit time
limit), it creates a new post and pins it. `legend.json` keeps the post's id and text, so a
restart edits the same post rather than posting again. The first pass after a start reads the
post back once, and pins it again if someone unpinned it.

The entries are the agents' own word, sent at `register` (the allowlist is an accident guard,
not a security boundary; D-055), so the post says it is informational. An agent the server's
`agents_allow` doesn't scope, or one whose directory is unknown (an older shim), is listed by
name only. A deactivated bot is left out. Every name and directory is a code span, so nothing
in the post renders as markdown or as a mention.

A failed call never raises: it is logged, and the next pass tries again. A 429 is waited out
by the client's own handling first (`crewbroker.mattermost`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from crewbroker.mattermost import MattermostAPI, MattermostError, NotFound
from crewbroker.state import Account, read_json, write_private_json

log = logging.getLogger(__name__)

LEGEND_CHANNEL = "oaw"
# In the post's props, never its text: the server renders no HTML, so a comment would show.
MARKER_PROP = "crew_legend"
MARKER_VERSION = "v1"

HEADING = "#### Agent legend"
NOTE = (
    "Each agent's bot account, and the last element of the directory it works in. The agents "
    "report these themselves: informational, not verified."
)


def _code(text: str) -> str:
    """`text` as a code span that can't close early or break the table: no backtick, no pipe,
    no line break."""
    clean = "".join(ch for ch in text if ch.isprintable()).replace("`", "'").replace("|", "/")
    return f"`{clean}`" if clean else ""


def directory_cell(acct: Account) -> str:
    """The directory column: empty when out of scope or unknown, and only the last element
    even if a full path were ever stored."""
    if not acct.in_scope or not acct.project_dir:
        return ""
    return _code(acct.project_dir.rstrip("/").rsplit("/", 1)[-1])


def render(accounts: Iterable[Account]) -> str:
    """The legend's text: a table of every active account, by name."""
    rows = sorted((a for a in accounts if a.pruned_at is None), key=lambda a: a.name)
    lines = [HEADING, "", NOTE, "", "| Agent | Directory |", "|---|---|"]
    lines += [f"| {_code(a.name)} | {directory_cell(a)} |" for a in rows]
    if not rows:
        lines.append("| (none yet) | |")
    return "\n".join(lines)


class Legend:
    """The legend post on one server, and `legend.json` beside the broker's other state."""

    def __init__(self, client: MattermostAPI, state_dir: Path) -> None:
        self.client = client
        self.path = state_dir / "legend.json"
        self._checked = False  # whether this process has read the stored post back yet

    def _load(self) -> dict[str, Any]:
        try:
            return read_json(self.path, {})
        except ValueError:
            log.warning("legend: %s is unreadable; posting a new legend", self.path)
            return {}

    def sync(self, channel_id: str, accounts: Iterable[Account]) -> str:
        """Bring the post up to date: `"unchanged"`, `"edited"`, `"pinned"` (only re-pinned),
        `"created"` or `"failed"`."""
        text = render(accounts)
        state = self._load()
        post_id = state.get("post_id") if state.get("channel_id") == channel_id else None
        try:
            if not isinstance(post_id, str) or not post_id:
                return self._create(channel_id, text)
            if not self._checked and not self._check(state, post_id):
                return self._create(channel_id, text)
            return self._edit(state, post_id, text)
        except MattermostError as exc:
            log.warning("legend: not updated; the next pass retries: %s", exc)
            return "failed"

    def _check(self, state: dict[str, Any], post_id: str) -> bool:
        """Once per process: whether the stored post is still there. One that someone
        unpinned is marked so, and `_edit` pins it again."""
        try:
            post = self.client.get_post(post_id)
        except NotFound:
            log.info("legend: post %s is gone; posting a new one", post_id)
            self._checked = True
            return False
        if not post.get("is_pinned"):
            state["pinned"] = False
        self._checked = True
        return True

    def _edit(self, state: dict[str, Any], post_id: str, text: str) -> str:
        changed, pinned = state.get("text") != text, state.get("pinned", True)
        if not changed and pinned:
            return "unchanged"
        if changed:
            try:
                self.client.patch_post(post_id, text)
            except MattermostError as exc:
                if not isinstance(exc, NotFound) and exc.status != 403:
                    raise
                log.info("legend: post %s can't be edited (%s); posting a new one", post_id, exc)
                return self._create(str(state["channel_id"]), text)
            # Kept before any pin, so a failed pin doesn't send the same edit again.
            write_private_json(self.path, {**state, "text": text})
        if not pinned:
            self.client.pin_post(post_id)
            log.info("legend: pinned post %s again", post_id)
        write_private_json(self.path, {**state, "text": text, "pinned": True})
        if changed:
            log.info("legend: edited post %s", post_id)
            return "edited"
        return "pinned"

    def _create(self, channel_id: str, text: str) -> str:
        post = self.client.create_post(channel_id, text, {MARKER_PROP: MARKER_VERSION})
        post_id = str(post["id"])
        # Kept before the pin, so a failed pin is retried on this post, not a new one.
        state = {"channel_id": channel_id, "post_id": post_id, "text": text, "pinned": False}
        write_private_json(self.path, state)
        self._checked = True
        self.client.pin_post(post_id)
        write_private_json(self.path, {**state, "pinned": True})
        log.info("legend: posted and pinned post %s", post_id)
        return "created"
