"""The threads each agent started or posted in (Dev Spec §5.9; Stories 2.3, 2.4b, 2.4c; R-12,
R-19; D-104).

`MyThreads.record(dev_name, msg)` is called with every post the agent makes through the daemon
(`send` and `dm`, after the backend accepted it). It keeps the post's thread root: `root_id`
for a reply, the post's own ID for a new root post, which is the thread the agent started.
`addressed_to` consults `roots(dev_name)` for "a reply to me", and `thread_backend` consults
`backend_of(dev_name, root)` for the backend a thread lives on (R-12).

One daemon holds every agent on its host, so the store is keyed by Dev-Name. Each root carries
its backend (`mattermost:<server_id>` or `discord`, §5.4) and `last_seen` (UTC), refreshed
whenever the agent posts in that thread again.

**Persisted (D-104).** The daemon's instance comes from `MyThreads.load(state_dir, ...)`, and
every `add` saves the agent's set to `<state_dir>/threads/<dev>.json`, so "replies on my
threads" survive a daemon restart. The file is
`{"roots": {"<root_id>": {"backend": "<name>", "last_seen": "<iso8601>"}}}`, mode `0600`, in a
directory of mode `0700`, written atomically. `state_dir` is `/var/lib/crewchat`, the daemon's
`StateDirectory`. Roots whose `last_seen` is older than `my_threads_retention_days`
(`servers.toml`, default 90) are pruned on load and on save, and each agent keeps at most its
most recent `limit` roots after that. A corrupt or unreadable file is logged once and treated
as empty; it never stops the daemon.

A `MyThreads()` built directly is in memory only (no `state_dir`): what the unit tests use.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crewchat.adapters.base import InboundMessage
from crewchat.tokens import DIR_MODE, write_private_json

DEFAULT_LIMIT = 10_000
DEFAULT_RETENTION_DAYS = 90
STATE_DIR = Path("/var/lib/crewchat")
THREADS_SUBDIR = "threads"
# A Dev-Name names a file here, so the same rule `register` enforces: no `/`, no leading dot.
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# The one backend-name form (§5.4): `mattermost:<server_id>`, or `discord`.
BACKEND_NAME_RE = re.compile(r"^(discord|mattermost:[a-z0-9._-]+)$")

log = logging.getLogger("crewchat.threads")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Entry:
    backend: str
    last_seen: datetime


class MyThreads:
    def __init__(
        self,
        limit: int = DEFAULT_LIMIT,
        *,
        state_dir: Path | None = None,
        retention_days: int | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if retention_days is not None and retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        self.limit = limit
        self.retention_days = retention_days
        self.clock = clock
        self.state_dir = state_dir
        # dev_name -> root_id -> (backend, last_seen), oldest first
        self._roots: dict[str, OrderedDict[str, _Entry]] = {}

    # -- persistence ------------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        state_dir: Path = STATE_DIR,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        clock: Clock = utc_now,
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> MyThreads:
        """The persisted store under `<state_dir>/threads/`, pruned by `retention_days`."""
        store = cls(limit, state_dir=state_dir, retention_days=retention_days, clock=clock)
        directory = store.threads_dir
        assert directory is not None
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(directory, DIR_MODE)
        for path in sorted(directory.glob("*.json")):
            dev_name = path.stem
            if not SAFE_NAME_RE.match(dev_name):
                continue
            roots = store._read(path)
            if roots:
                store._roots[dev_name] = roots
                store._prune(dev_name)
        return store

    @property
    def threads_dir(self) -> Path | None:
        return None if self.state_dir is None else self.state_dir / THREADS_SUBDIR

    def path(self, dev_name: str) -> Path | None:
        directory = self.threads_dir
        if directory is None or not SAFE_NAME_RE.match(dev_name):
            return None
        return directory / f"{dev_name}.json"

    def _read(self, path: Path) -> OrderedDict[str, _Entry]:
        """One agent's file, oldest root first; a bad file is logged and read as empty."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw["roots"]
            if not isinstance(entries, dict):
                raise ValueError("roots is not an object")
            parsed: list[tuple[datetime, str, str]] = []
            for root_id, entry in entries.items():
                if not isinstance(root_id, str) or not root_id or not isinstance(entry, dict):
                    raise ValueError(f"bad entry {root_id!r}")
                backend, stamp = entry.get("backend"), entry.get("last_seen")
                if not isinstance(backend, str) or not BACKEND_NAME_RE.match(backend):
                    raise ValueError(f"entry {root_id!r} has no valid backend")
                if not isinstance(stamp, str):
                    raise ValueError(f"entry {root_id!r} has no last_seen")
                seen = datetime.fromisoformat(stamp)
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=UTC)
                parsed.append((seen, root_id, backend))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.error("ignoring unreadable thread set %s: %s", path, exc)
            return OrderedDict()
        parsed.sort()
        return OrderedDict((root_id, _Entry(backend, seen)) for seen, root_id, backend in parsed)

    def save(self, dev_name: str) -> None:
        """Write `dev_name`'s set to its file, pruned first. A failed write is logged."""
        path = self.path(dev_name)
        if path is None:
            return
        self._prune(dev_name)
        roots = self._roots.get(dev_name, OrderedDict())
        data = {
            "roots": {
                root_id: {"backend": e.backend, "last_seen": e.last_seen.isoformat()}
                for root_id, e in roots.items()
            }
        }
        try:
            path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            write_private_json(path, data)
        except OSError as exc:
            log.error("cannot save the thread set for %s at %s: %s", dev_name, path, exc)

    def _prune(self, dev_name: str) -> None:
        """Drop roots past retention, then the oldest past `limit`."""
        roots = self._roots.get(dev_name)
        if roots is None:
            return
        if self.retention_days is not None:
            cutoff = self.clock() - timedelta(days=self.retention_days)
            for root_id in [r for r, e in roots.items() if e.last_seen < cutoff]:
                del roots[root_id]
        while len(roots) > self.limit:
            roots.popitem(last=False)

    # -- the set ----------------------------------------------------------------------------
    def add(self, dev_name: str, root_id: str, backend: str) -> None:
        """Remember `root_id` on `backend` as one of `dev_name`'s threads, seen now (most
        recent last)."""
        if not root_id:
            return
        if not BACKEND_NAME_RE.match(backend):
            raise ValueError(f"backend {backend!r} is not mattermost:<server_id> or discord")
        roots = self._roots.setdefault(dev_name, OrderedDict())
        roots[root_id] = _Entry(backend, self.clock())
        roots.move_to_end(root_id)
        self._prune(dev_name)
        self.save(dev_name)

    def record(self, dev_name: str, msg: InboundMessage) -> str:
        """Record a post `dev_name` made; return the thread root it was filed under."""
        root = msg.root_id or msg.post_id
        self.add(dev_name, root, msg.backend)
        return root

    def roots(self, dev_name: str) -> set[str]:
        """The root IDs of every thread `dev_name` started or posted in."""
        return set(self._roots.get(dev_name, ()))

    def backend_of(self, dev_name: str, root_id: str) -> str | None:
        """The backend of `dev_name`'s thread `root_id`, or None when it is not one of them."""
        roots = self._roots.get(dev_name)
        entry = roots.get(root_id) if roots is not None else None
        return entry.backend if entry is not None else None

    def forget(self, dev_name: str) -> None:
        """Drop an agent's threads (it left rotation), and its file."""
        self._roots.pop(dev_name, None)
        path = self.path(dev_name)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.error("cannot delete the thread set %s: %s", path, exc)
