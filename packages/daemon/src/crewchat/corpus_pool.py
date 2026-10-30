"""The kiosk's corpus pool manifest, per research server (#139; D-110, D-111, D-112).

A server with `kiosk_research = true` gets a manifest, `/var/lib/crewchat-pool/<server_id>.json`:
the full repos (`host/group/.../repo`) that `crewkiosk-corpus` clones for that server's kiosks
(#140). Its list is

    admitted(agent repos + corpus_extra) - corpus_exclude

where an **agent repo** is the `repo` that an agent holding a token on that server sent with
its last `register`, and **admitted** means `admits_repo()` passes: the repo's `host/org`
equals a forge entry in an explicit `agents_allow`, and no forge entry in `agents_deny`. Path
and `name:` entries never admit a repo (D-112). An agent repo must pass the agent's own crew's
entry for that server, since another crew's allowlist never vouched for that agent; an entry
in `corpus_extra`, the operator's own, may pass any crew's. The agent's own word is not a wall.
The two walls are the explicit allowlist, and the operator's corpus credential for that
server. So an entry in `corpus_extra` that no allowlist admits is dropped too.

An exclude covers the repo's wiki too (`<repo>.wiki`, a repo of its own on GitHub and GitLab).
A renamed or transferred repo whose old path still redirects is beyond the daemon's sight: the
corpus builder resolves each repo's canonical path and checks the excludes again (#140).

The file is `{"server", "generated_at", "refresh_minutes", "repos", "exclude"}`, where
`exclude` is `corpus_exclude` canonicalized (casefolded, no `.git`), for the builder's own check
on each repo's canonical path, since it can't read the daemon's config. Written atomically, mode
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
# crewkiosk-corpus's per-server build status (#140), which `health` reports.
CORPUS_STATUS_DIR = Path("/var/lib/crewcorpus/status")
# Listed by name in `health`. An `excluded` repo is only counted: the name it resolved to is
# the operator's to keep hidden, and the corpus status holds only the manifest's name anyway.
CORPUS_LISTED_STATES = ("withheld", "clone_failed")


def pool_repos(config: ServersConfig, agents: Iterable[AgentTokens], server_id: str) -> list[str]:
    """That server's pool, sorted: admitted (agent repos + corpus_extra) - corpus_exclude."""
    server = _server(config, server_id)
    entries = [
        crew.brokers[server_id] for crew in config.crews.values() if server_id in crew.brokers
    ]
    # Each candidate with the entries that may admit it.
    candidates = [(r, entries) for r in server.corpus_extra]
    for a in agents:
        crew = config.crews.get(a.crew)
        if server_id in a.servers and a.repo and crew and server_id in crew.brokers:
            candidates.append((a.repo, [crew.brokers[server_id]]))
    # Forges resolve paths without regard to case, and clone `repo.git` as `repo`, so one repo
    # spelled two ways is one repo, and an exclude matches it however it is spelled: an
    # exclude always wins, over the repo's wiki too.
    excluded = set(exclude_keys(server))
    pool: dict[str, str] = {}
    canon = [(_canon(r), admitting) for r, admitting in candidates]
    for repo, admitting in sorted((c for c in canon if is_repo(c[0])), key=lambda c: c[0]):
        key = repo.casefold()  # `host/org/.git` is no repo, filtered above
        if key in excluded or key.removesuffix(".wiki") in excluded or key in pool:
            continue
        if any(admits_repo(e, repo) for e in admitting):
            pool[key] = repo
    return sorted(pool.values())


def exclude_keys(server: MattermostServer) -> list[str]:
    """`corpus_exclude` as the keys repos are compared by, sorted: casefolded, no `.git`. A
    repo is excluded when its key, or its key without `.wiki`, is one of them."""
    return sorted({_canon(r).casefold() for r in server.corpus_exclude})


def _canon(repo: str) -> str:
    """`repo` without any trailing `.git`, in any case: the name a forge clones it by."""
    while repo.casefold().endswith(".git"):
        repo = repo[: -len(".git")]
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
        corpus_status: Path = CORPUS_STATUS_DIR,
    ) -> None:
        self.config = config
        self.agents = agents
        self.root = root
        self.clock = clock
        self.corpus_status = corpus_status
        self.sizes: dict[str, int] = {}
        self.errors: dict[str, str] = {}

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
            body = {
                "server": server.id,
                "refresh_minutes": server.corpus_refresh_minutes,
                "exclude": exclude_keys(server),
                "repos": repos,
            }
            # `health` counts what is on disk, never what failed to get there.
            if _current(path) != body:
                try:
                    _write(path, {**body, "generated_at": self.clock().isoformat()})
                except OSError as exc:
                    log.error("pool: cannot write %s: %s", path, exc)
                    self.sizes.pop(server.id, None)
                    self.errors[server.id] = f"cannot write {path}: {exc.strerror or exc}"
                    continue
                log.info("pool: %s has %d repos", server.id, len(repos))
            self.sizes[server.id] = len(repos)
            self.errors.pop(server.id, None)

    def health(self) -> dict[str, Any]:
        """`{<server_id>: {"pool_repos": N}}` for each research server, N being what its
        manifest on disk lists; `{"pool_repos": None, "error": ...}` while it can't be
        written, and `None` before the first refresh. Once crewkiosk-corpus has built that
        server, `corpus` holds its last build (`corpus_health`)."""
        out: dict[str, Any] = {}
        for s in self.config.mattermost:
            if not s.kiosk_research:
                continue
            out[s.id] = {"pool_repos": self.sizes.get(s.id)}
            if s.id in self.errors:
                out[s.id]["error"] = self.errors[s.id]
            corpus = corpus_health(self.corpus_status / f"{s.id}.json")
            if corpus is not None:
                out[s.id]["corpus"] = corpus
        return out


def corpus_health(path: Path) -> dict[str, Any] | None:
    """One server's corpus build, from its status file: `{"built_at", "repos", "ok",
    "withheld": [...], "clone_failed": [...], "excluded"}`, the lists naming repos and the
    rest counts. None before its first build; `{"error": ...}` when the file can't be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return {"error": f"cannot read {path}: {getattr(exc, 'strerror', None) or exc}"}
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return {"error": f"{path} has no repos"}
    states = {name: r.get("state") if isinstance(r, dict) else None for name, r in repos.items()}
    out: dict[str, Any] = {"built_at": data.get("built_at"), "repos": len(states)}
    out["ok"] = sum(1 for s in states.values() if s == "ok")
    for state in CORPUS_LISTED_STATES:
        out[state] = sorted(name for name, s in states.items() if s == state)
    out["excluded"] = sum(1 for s in states.values() if s == "excluded")
    return out


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
