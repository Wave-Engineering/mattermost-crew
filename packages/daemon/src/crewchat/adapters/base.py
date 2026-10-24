"""The contract every chat backend adapter implements (Dev Spec §5.4, §5.9; Story 2.1).

One daemon holds every agent on its host, so each operation names the agent it acts as
(`dev_name`). The adapter turns that into the agent's own credential for its backend: on
Mattermost, the token the store holds for (dev_name, server_id).

`InboundMessage` is what every backend hands the daemon, from a pull (`read`, `thread`) or the
push stream (`stream`). Its `proto` is the sender's protocol marker, 0 when the message has
none (R-14). `is_bot` and `kiosk` feed §5.9's membership rule. Ownership is not here: a bot
token can't read another bot's owner, so ownership comes from the brokers'
`crew_info.minted_bot_ids` (D-100).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class InboundMessage:
    backend: str  # e.g. "mattermost:<server_id>" or "discord"
    channel: str
    post_id: str
    root_id: str  # "" when the message is not a reply
    author_id: str
    author_name: str
    text: str
    mentions: list[str] = field(default_factory=list)  # the server's own markers (R-29)
    proto: int = 0  # the sender's protocol marker; 0 when absent (R-14)
    ts: float = 0.0  # creation time, seconds since the epoch
    is_bot: bool = False  # the author's user object `is_bot`, never a post prop
    kiosk: bool = False  # the post carries `props.crew_kiosk == 1` (§5.9)


class ChatBackend(Protocol):
    """send, read, thread, dm, react and the push stream, acting as one agent at a time."""

    async def send(
        self, dev_name: str, channel: str, text: str, thread: str | None = None
    ) -> InboundMessage:
        """Post `text` in `channel` (a reply when `thread` names a root post)."""

    async def read(
        self, dev_name: str, channel: str, since: float | None = None, limit: int | None = None
    ) -> list[InboundMessage]:
        """Recent messages in `channel`, oldest first."""

    async def thread(self, dev_name: str, post_id: str) -> list[InboundMessage]:
        """Every message in the thread `post_id` belongs to, oldest first."""

    async def dm(self, dev_name: str, to: str, text: str) -> InboundMessage:
        """A direct message to the user `to`."""

    async def react(self, dev_name: str, post_id: str, emoji: str) -> None:
        """Add `emoji` to `post_id`."""

    def stream(self, dev_name: str) -> AsyncIterator[InboundMessage]:
        """Messages as they arrive for `dev_name`, its own posts already dropped (R-04)."""
        ...
