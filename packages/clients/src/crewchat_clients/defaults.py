"""The agent's default backend (Dev Spec §5.10; R-11).

`resolve_default(identity, repo_root)` runs in the shim, as the agent's user, and follows
R-11's order:

1. the identity file's `home_backend`;
2. `~/.claude/chat-defaults.json`, under the repo's key: `<forge-host>/<org>` from
   `git remote get-url origin`, or the absolute `repo_root` when there is no remote;
3. otherwise `NeedsHuman`, which carries the key the answer is to be recorded under.

A backend is `mattermost:<server-id>` or `discord`. The daemon never reads the defaults file
(§5.3); the shim sends the resolved backend in every frame.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Backend = str
DEFAULTS_PATH = Path("~/.claude/chat-defaults.json")
BACKEND_RE = re.compile(r"^(discord|mattermost:[a-z0-9._-]+)$")
# scp-like SSH: `git@host:org/repo.git`
SCP_RE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")

RemoteFor = Callable[[Path], str | None]


@dataclass(frozen=True)
class NeedsHuman:
    """No default yet: the human partner is asked, and the answer goes under `key`."""

    key: str


class DefaultsError(ValueError):
    """The defaults file, or a backend named in it, is malformed."""


def is_backend(value: Any) -> bool:
    return isinstance(value, str) and bool(BACKEND_RE.fullmatch(value))


def defaults_path(home: Path | None = None) -> Path:
    return (home / ".claude" / "chat-defaults.json") if home else DEFAULTS_PATH.expanduser()


def forge_key(remote: str) -> str | None:
    """`<host>/<org>` from an SSH (`git@host:org/r.git`, `ssh://git@host:22/org/r`) or HTTPS
    (`https://host/org/r.git`) remote; None for a local path or anything unparseable."""
    parts = _remote_parts(remote)
    if parts is None:
        return None
    host, path = parts
    return f"{host}/{path.partition('/')[0]}"


def forge_repo(remote: str) -> str | None:
    """`<host>/<group>/.../<repo>` from the same remotes as `forge_key`, with `.git` dropped:
    the full repo the kiosk's corpus pool clones (#139, D-112). None where `forge_key` is."""
    parts = _remote_parts(remote)
    if parts is None:
        return None
    host, path = parts
    path = path.removesuffix(".git").rstrip("/")
    return f"{host}/{path}" if path else None


def _remote_parts(remote: str) -> tuple[str, str] | None:
    """A remote's lower-cased host and its path, stripped of slashes; None for a local path,
    anything unparseable, or no org."""
    remote = remote.strip()
    if not remote:
        return None
    m = re.match(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<rest>.*)$", remote, re.IGNORECASE)
    if m:
        if m.group("scheme").lower() == "file":
            return None
        authority, _, path = m.group("rest").partition("/")
        host = authority.rpartition("@")[2]
        # An IPv6 literal keeps its brackets; the port after it, or after a name, goes.
        host = host[: host.find("]") + 1] if host.startswith("[") else host.partition(":")[0]
    else:
        scp = SCP_RE.match(remote)
        if scp is None or remote.startswith(("/", ".")):
            return None
        host, path = scp.group("host"), scp.group("path")
    path = path.strip("/")
    if not host or not path.partition("/")[0]:
        return None
    return host.lower(), path


def git_remote(repo_root: Path) -> str | None:
    """`git remote get-url origin`, or None when there is no such remote."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def repo_key(repo_root: Path, remote_for: RemoteFor = git_remote) -> str:
    """The defaults file's key for this repo: its forge org, else its absolute path."""
    remote = remote_for(repo_root)
    key = forge_key(remote) if remote else None
    return key or str(repo_root.resolve())


def repo_ref(repo_root: Path, remote_for: RemoteFor = git_remote) -> str:
    """This repo's full forge path for the kiosk's corpus pool (#139), or "" with no forge
    remote: a path-keyed repo adds nothing to any pool (D-112)."""
    remote = remote_for(repo_root)
    return (forge_repo(remote) if remote else None) or ""


def load_defaults(path: Path) -> dict[str, Backend]:
    """The defaults file; {} when it doesn't exist."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise DefaultsError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DefaultsError(f"{path}: not a JSON object")
    return {str(k): v for k, v in raw.items() if is_backend(v)}


def resolve_default(
    identity: Mapping[str, Any],
    repo_root: Path,
    *,
    path: Path | None = None,
    remote_for: RemoteFor = git_remote,
) -> Backend | NeedsHuman:
    """R-11: the identity file's `home_backend`, then the defaults file, then `NeedsHuman`."""
    home = identity.get("home_backend")
    if home is not None:
        if not is_backend(home):
            raise DefaultsError(f"home_backend {home!r} is not mattermost:<server-id> or discord")
        return str(home)
    key = repo_key(repo_root, remote_for)
    found = load_defaults(path or defaults_path()).get(key)
    if found is not None:
        return found
    return NeedsHuman(key=key)


def write_default(key: str, backend: Backend, path: Path | None = None) -> None:
    """Record `key -> backend` in the defaults file, atomically (a temp file, then rename)."""
    if not isinstance(key, str) or not key.strip():
        raise DefaultsError("key is a non-empty string")
    if not is_backend(backend):
        raise DefaultsError(f"backend {backend!r} is not mattermost:<server-id> or discord")
    path = path or defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as exc:
        raise DefaultsError(f"{path}: {exc}; not overwriting it") from exc
    if not isinstance(raw, dict):
        raise DefaultsError(f"{path}: not a JSON object; not overwriting it")
    raw[key] = backend
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
