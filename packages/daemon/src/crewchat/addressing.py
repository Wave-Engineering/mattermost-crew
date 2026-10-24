"""Addressing: is an inbound message for this agent? (Dev Spec §4.4, §5.9; Story 2.3; D-024.)

Every adapter inherits these rules (CP-04):

- `@agents` is the fleet broadcast on every backend. `@all` counts too, but only on Discord and
  only during the transition: on Mattermost `@all` pages every human in the channel (R-16).
- A token keeps the `.` inside it, so `@kessel.oaw` is one token, never `@kesseloaw` (R-18).
- Crew and team tokens (`@<crew>`, `@<dev-team>`) are matched here, by our own filter, never
  through Mattermost user groups, which are a paid feature (R-17, CT-01).
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

from crewchat.adapters.base import InboundMessage
from crewchat.registry import Registration

BROADCAST = "agents"
TRANSITION_BROADCAST = "all"  # Discord only, during the transition (R-16)

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


def group_tokens(reg: Registration) -> set[str]:
    """The crew token and, when the agent has one, its Dev-Team token (R-17)."""
    groups = {reg.crew.lower()}
    if reg.dev_team:
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


def addressed_to(msg: InboundMessage, reg: Registration, my_threads: set[str]) -> bool:
    """Whether a pushed message is addressed to the agent `reg` (R-16 to R-19)."""
    if is_direct_address(msg, reg, my_threads):
        return True
    toks = tokens(msg.text)
    return is_broadcast(toks, msg.backend) or bool(toks & group_tokens(reg))
