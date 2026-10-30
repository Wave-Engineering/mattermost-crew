"""The kiosk's corpus pool manifest, per research server (#139; D-110, D-111, D-112).

A server with `kiosk_research = true` gets a manifest, `/var/lib/crewchat-pool/<server_id>.json`:
the full repos (`host/group/.../repo`) that `crewkiosk-corpus` clones for that server's kiosks
(#140). Its list is

    admitted(agent repos + corpus_extra) - corpus_exclude

where an **agent repo** is the `repo` that an agent holding a token on that server sent with
its last `register`, and **admitted** means `admits_repo()` passes for that server's broker
entry of some crew: the repo's `host/org` equals a forge entry in an explicit `agents_allow`,
and no forge entry in `agents_deny`. Path and `name:` entries never admit a repo (D-112).
The agent's own word is not a wall. The two walls are the explicit allowlist, and the
operator's corpus credential for that server. So an entry in `corpus_extra` that the allowlist
doesn't admit is dropped too.

The file is `{"server", "generated_at", "refresh_minutes", "repos"}`, written atomically, mode
0640, into a directory the corpus user's group can read (`packaging/crewchat-pool.tmpfiles.conf`).
It is rewritten only when its contents other than `generated_at` change. The daemon writes it
at start and after each `register`; an agent that loses its token another way (a deactivated
account during rotation) leaves the pool at the next `register` or restart. A server with
research off has no manifest: any left from before is removed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crewchat.allowlist import admits_repo
from crewchat.config import MattermostServer, ServersConfig, is_repo
from crewchat.tokens import AgentTokens

log = logging.getLogger(__name__)

POOL_DIR = Path("/var/lib/crewchat-pool")
MANIFEST_MODE = 0o640


def pool_repos(config: ServersConfig, agents: Iterable[AgentTokens], server_id: str) -> list[str]:
    """That server's pool, sorted: admitted (agent repos + corpus_extra) - corpus_exclude."""
    server = _server(config, server_id)
    entries = [
        crew.brokers[server_id] for crew in config.crews.values() if server_id in crew.brokers
    ]
    held = [a.repo for a in agents if server_id in a.servers and a.repo and is_repo(a.repo)]
    # Forges resolve paths without regard to case, and clone `repo.git` as `repo`, so one repo
    # spelled two ways is one repo, and an exclude matches it however it is spelled: an
    # exclude always wins.
    excluded = {_canon(r).casefold() for r in server.corpus_exclude}
    pool: dict[str, str] = {}
    for repo in sorted(_canon(r) for r in [*server.corpus_extra, *held]):
        key = repo.casefold()
        if key in excluded or key in pool:
            continue
        if any(admits_repo(e, repo) for e in entries):
            pool[key] = repo
    return sorted(pool.values())


def _canon(repo: str) -> str:
    """`repo` without any trailing `.git`, the name a forge clones it by."""
    while repo.endswith(".git"):
        repo = repo.removesuffix(".git")
    return repo


def _server(config: ServersConfig, server_id: str) -> MattermostServer:
    for server in config.mattermost:
        if server.id == server_id:
            return server
    raise KeyError(server_id)


class PoolManifest:
    """Writes each research server's manifest, and removes a non-research server's."""

    def __init__(
        self,
        config: ServersConfig,
        agents: Callable[[], Iterable[AgentTokens]],
        root: Path = POOL_DIR,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.agents = agents
        self.root = root
        self.clock = clock
        self.sizes: dict[str, int] = {}

    def path_for(self, server_id: str) -> Path:
        return self.root / f"{server_id}.json"

    def refresh(self) -> None:
        """Bring every server's manifest up to date. A write that fails is logged, never
        raised: a `register` never fails over the pool."""
        agents = list(self.agents())
        for server in self.config.mattermost:
            path = self.path_for(server.id)
            if not server.kiosk_research:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    log.error("pool: cannot remove %s: %s", path, exc)
                else:
                    log.info("pool: removed %s, research is off on %s", path, server.id)
                continue
            repos = pool_repos(self.config, agents, server.id)
            self.sizes[server.id] = len(repos)
            body = {
                "server": server.id,
                "refresh_minutes": server.corpus_refresh_minutes,
                "repos": repos,
            }
            if _current(path) == body:
                continue
            try:
                _write(path, {**body, "generated_at": self.clock().isoformat()})
            except OSError as exc:
                log.error("pool: cannot write %s: %s", path, exc)
                continue
            log.info("pool: %s has %d repos", server.id, len(repos))

    def health(self) -> dict[str, Any]:
        """`{<server_id>: {"pool_repos": N}}` for each research server."""
        return {
            s.id: {"pool_repos": self.sizes.get(s.id, 0)}
            for s in self.config.mattermost
            if s.kiosk_research
        }


def _current(path: Path) -> dict[str, Any] | None:
    """The manifest on disk without its `generated_at`, or None when absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data.pop("generated_at", None)
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    """Atomically: a temp file beside it at MANIFEST_MODE, then `os.replace`."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, MANIFEST_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
