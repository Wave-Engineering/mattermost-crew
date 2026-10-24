"""The threads each agent started or posted in (Dev Spec §5.9; Story 2.3; R-19).

`MyThreads.record(dev_name, msg)` is called with every post the agent makes. It keeps the
post's thread root: `root_id` for a reply, the post's own ID for a new root post, which is the
thread the agent started. `addressed_to` consults `roots(dev_name)` for "a reply to me".

One daemon holds every agent on its host, so the store is keyed by Dev-Name. It lives in
memory and keeps each agent's most recent `limit` roots, so a long-lived agent's set can't
grow without bound.
"""

from __future__ import annotations

from collections import OrderedDict

from crewchat.adapters.base import InboundMessage

DEFAULT_LIMIT = 10_000


class MyThreads:
    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self._roots: dict[str, OrderedDict[str, None]] = {}

    def add(self, dev_name: str, root_id: str) -> None:
        """Remember `root_id` as one of `dev_name`'s threads (most recent last)."""
        if not root_id:
            return
        roots = self._roots.setdefault(dev_name, OrderedDict())
        roots[root_id] = None
        roots.move_to_end(root_id)
        while len(roots) > self.limit:
            roots.popitem(last=False)

    def record(self, dev_name: str, msg: InboundMessage) -> str:
        """Record a post `dev_name` made; return the thread root it was filed under."""
        root = msg.root_id or msg.post_id
        self.add(dev_name, root)
        return root

    def roots(self, dev_name: str) -> set[str]:
        """The root IDs of every thread `dev_name` started or posted in."""
        return set(self._roots.get(dev_name, ()))

    def forget(self, dev_name: str) -> None:
        """Drop an agent's threads (it left rotation)."""
        self._roots.pop(dev_name, None)
