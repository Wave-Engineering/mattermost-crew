"""Addressing: is an inbound message for this agent? (Dev Spec §4.4, §5.9; Story 2.3; D-024.)

Every adapter inherits these rules (CP-04):

- `@agents` is the fleet broadcast on every backend. `@all` counts too, but only on Discord and
  only during the transition: on Mattermost `@all` pages every human in the channel (R-16).
- A token keeps the `.` inside it, so `@kessel.oaw` is one token, never `@kesseloaw` (R-18).
- Crew and team tokens (`@<crew>`, `@<dev-team>`) are matched here, by our own filter, never
  through Mattermost user groups, which are a paid feature (R-17, CT-01). The crew token always
  counts. Whether the team token counts is a per-backend policy, `team_scope` (D-103): on
  Discord any author's `@<dev-team>` counts; on Mattermost only an author of the agent's own
  crew (`author_crew`, which membership supplies in Phase 3), and none when it is unknown.
- The agent's avatar is display only: it never addresses anyone.
- "A reply to me" is a post in a thread the agent started or posted in (its root ID is in the
  agent's `MyThreads`), or a direct message to the agent (R-19, D-080).

`addressed_to` is the push-path test (R-21: pull skips addressing). `is_direct_address` is the
narrower one kiosk routing needs (§5.9): broadcasts and crew or team tokens never count.

Tokens are compared case-insensitively. Mattermost usernames are lower case already, and a
human typing `@Kessel` means the same agent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from crewchat.adapters.base import InboundMessage
from crewchat.registry import Registration

BROADCAST = "agents"
TRANSITION_BROADCAST = "all"  # Discord only, during the transition (R-16)

TeamScope = Literal["crew", "backend"]

# `@` at the start or after anything that can't be part of a name, so an email address
# (`bob@example.com`) is not a token. The name keeps `.`, `_` and `-` (R-18).
TOKEN_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9_][A-Za-z0-9._-]*)")


def tokens(text: str) -> set[str]:
    """The `@`-tokens in `text`, lower case and without the `@`; `.` inside is kept (R-18).

    A trailing `.` or `-` is punctuation, not part of the name: `ask @kessel.` is `kessel`.
    """
    found: set[str] = set()
    for match in TOKEN_RE.finditer(text):
        name = match.group(1).rstrip(".-").lower()
        if name:
            found.add(name)
    return found


def is_discord(backend: str) -> bool:
    return backend == "discord" or backend.startswith("discord:")


def is_broadcast(toks: Iterable[str], backend: str) -> bool:
    """`@agents` on every backend; `@all` on Discord only (R-16)."""
    toks = set(toks)
    if BROADCAST in toks:
        return True
    return TRANSITION_BROADCAST in toks and is_discord(backend)


def own_names(reg: Registration) -> set[str]:
    """The tokens that name this agent: `<dev-name>` and `<dev-name>.<crew>`."""
    dev = reg.dev_name.lower()
    return {dev, f"{dev}.{reg.crew.lower()}"}


def team_scope(backend: str) -> TeamScope:
    """Who may address an agent by its Dev-Team token on `backend` (D-103).

    `"backend"` on Discord, where everyone in the guild is trusted with `@<dev-team>`, so the
    token matches on `reg.dev_team` alone; `"crew"` on every Mattermost server, where it
    matches only for an author known to be of the agent's own crew.
    """
    return "backend" if is_discord(backend) else "crew"


def group_tokens(reg: Registration, backend: str, author_crew: str | None) -> set[str]:
    """The crew token and, when `team_scope` allows it, the Dev-Team token (R-17, D-103).

    Under `"crew"` scope an unknown author (`author_crew` None) never earns the team token:
    fail closed.
    """
    groups = {reg.crew.lower()}
    if reg.dev_team and (
        team_scope(backend) == "backend" or (author_crew is not None and author_crew == reg.crew)
    ):
        groups.add(reg.dev_team.lower())
    return groups


def is_direct_address(msg: InboundMessage, reg: Registration, my_threads: set[str]) -> bool:
    """`@<dev-name>`, `@<dev-name>.<crew>`, a DM to the agent, or a reply in its own thread.

    Kiosk routing's test (§5.9): a broadcast or a crew or team token is never direct address.
    """
    if msg.dm:
        return True
    if msg.root_id and msg.root_id in my_threads:
        return True
    return bool(tokens(msg.text) & own_names(reg))


def addressed_to(
    msg: InboundMessage,
    reg: Registration,
    my_threads: set[str],
    author_crew: str | None = None,
) -> bool:
    """Whether a pushed message is addressed to the agent `reg` (R-16 to R-19).

    `author_crew` is the author's crew when membership knows it (Phase 3); Phase 2 callers
    pass None, so on Mattermost the team token never matches (D-103).
    """
    if is_direct_address(msg, reg, my_threads):
        return True
    toks = tokens(msg.text)
    groups = group_tokens(reg, msg.backend, author_crew)
    return is_broadcast(toks, msg.backend) or bool(toks & groups)
