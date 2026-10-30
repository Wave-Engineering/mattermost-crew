"""crewkiosk-corpus: builds each research server's corpus from its pool manifest (#140; D-111).

A timer runs it as `crewcorpus`. For each `/var/lib/crewchat-pool/<server>.json` the daemon
writes (#139), it publishes a filtered, scrubbed, read-only copy of every listed repo's default
branch at `/var/lib/crewcorpus/<server>/current/<repo>/...`, for `crewkiosk-broker` to serve.
The kiosk never sees it.

Per repo:

1. **Resolve its canonical path** through the forge API, with that server's credential for that
   repo's forge host (`corpus-<server>@<host>.credential`): a forge is never sent another's
   token, and a host with no credential is never asked. A forge keeps redirects after a rename
   or transfer, so a manifest path can lead to a repo the operator excluded under its new name.
   A canonical path that is excluded, with or without `.wiki`, is never cloned, and its last
   good copy is dropped. A lookup that fails, or that redirects off its forge, is
   `clone_failed`: the unresolved path is never cloned.
2. **Clone** the canonical path at depth 1, blobs over 5 MB left out, no checkout, and no
   redirect followed. The token reaches git through `GIT_ASKPASS`, which reads the credential
   file itself, so it is never in an argv, a URL or the environment.
3. **Extract** the tree from the object store, not a checkout: a checkout would fetch every
   blob the filter left out. Only regular files of 5 MB or less that are UTF-8 text with no NUL
   are kept. Symlinks, submodules and `.git` never reach the tree, nor does any path with a
   component on the denylist.
4. **Scan** it with `gitleaks dir`. A finding or a gitleaks error withholds the repo, and its
   last good copy is served. Only the rule ids and file paths are logged, never a value. A repo
   may not exempt itself: `.gitleaks.toml` and `.gitleaksignore` never reach the tree, gitleaks
   runs with no `GITLEAKS_*` variables, and it scans a flat copy from inside it (`.`), each
   file as `f<n>`, so its default path allowlist (`node_modules/`, `*.svg`, lock files) matches
   nothing, with `gitleaks:allow` neutralized. A file keeps the suffix a path-scoped rule needs
   (`.yaml`, `.tf`, `.php`, `nuget.config`), as `f000001.yaml`. A missing gitleaks publishes
   nothing at all. A repo's last good copy is served only if it still passes the denylist and
   gitleaks today: a rule added since is applied to it too.

A repo that resolves into another org than its manifest entry's (a transfer) is `excluded`,
like one that resolves to an excluded path: the daemon admitted the entry by its host/org, and
no allowlist vouched for the new one. Each exclude entry is resolved too, so one written under a
repo's old name still covers it once renamed. If an exclude can't be resolved (anything but a
404), nothing from its forge is served that build, not even a last good copy.

A filesystem or any other error fails that one repo, and a failed build fails that one server:
neither stops the rest. A repo whose path lies under another pooled repo's is refused. A file
whose name isn't UTF-8 is left out.

Each server is published as a new generation, `gen-<utc>-<n>`, with dirs 0555 and files 0444,
and `current` is swapped onto it atomically. Generations older than the previous one are
removed, and the previous one, kept for a reader mid-read, loses at once any repo the new one
doesn't serve. `status/<server>.json` (0644) records each repo's state for the daemon's
`health`. It never holds a secret, a finding's value, a credential or an exclude. A server is
rebuilt once its `refresh_minutes` have passed since its last build, or sooner if its manifest
changes. A server with no manifest (research turned off) has its corpus and status removed.
Credential files must be 0600 or 0400.

`/var/lib/crewcorpus` is 2751 `crewcorpus:crewkiosk-broker` (`packaging/crewcorpus.tmpfiles.conf`).
The daemon can reach `status/` through it, and each `<server>` directory inherits the broker's
group and is 0750, so no one else can read the corpus.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

CORPUS_ROOT = Path("/var/lib/crewcorpus")
POOL_DIR = Path("/var/lib/crewchat-pool")
# One token per forge host: a pool may span forges, and no forge is ever sent another's token.
# `@` is in neither a server id nor a host, so no two (server, host) pairs share a file.
CREDENTIAL = "/etc/crewkiosk/corpus-{server}@{host}.credential"
MAX_FILE_BYTES = 5 * 1024 * 1024
TEXT_PROBE_BYTES = 8 * 1024
GIT_TIMEOUT_S = 600
LOOKUP_TIMEOUT_S = 30
DIR_MODE = 0o555
FILE_MODE = 0o444
SERVER_DIR_MODE = 0o750
STATUS_MODE = 0o644

# The daemon's `crewchat.config.REPO_ENTRY_RE`, copied: the builder can't import the daemon.
REPO_ENTRY_RE = re.compile(r"^[a-z0-9.-]+(/[A-Za-z0-9._-]+){2,}$")
SERVER_RE = re.compile(r"^[a-z0-9._-]+$")
# `<root>/status` is world-traversable, and `.` and `..` are no directory of their own.
RESERVED_SERVERS = frozenset({"status", ".", ".."})
MANIFEST_KEYS = frozenset({"server", "generated_at", "refresh_minutes", "repos", "exclude"})

# Matched, casefolded, against every path component, the basename among them.
DENYLIST = (
    "*.enc",
    ".sops.yaml",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    ".env*",
    "*credential*",
    "*secret*",
    "*.tfvars",
)
# Not secrets, but a repo carrying them could exempt itself from the scan.
SCANNER_CONTROL = (".gitleaks.toml", ".gitleaksignore")
ALLOW_MARKER = b"gitleaks:allow"
ALLOW_NEUTRALIZED = b"gitleaks-allow"
LEAKS_FOUND = 3  # the `--exit-code` gitleaks is given for a finding; its errors exit 1
# The suffixes the default config's path-scoped rules need (kubernetes-secret-yaml,
# hashicorp-tf-password, freemius-secret-key, nuget-config-password, gitleaks 8.30). None is on
# its path allowlist, so the shadow name keeps them.
RULE_SUFFIX = re.compile(r"(?i)(?:\.(?:ya?ml|tf|hcl|php)|nuget\.config)$")

State = Literal["ok", "withheld", "clone_failed", "excluded"]
Lookup = Callable[[str, str], str]
CloneUrl = Callable[[str], str]


class ManifestError(ValueError):
    pass


class LookupFailed(RuntimeError):
    pass


class NotFound(LookupFailed):
    """The forge has no repo by that name that the token can see (404)."""


class GitFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Manifest:
    server: str
    generated_at: str
    refresh_minutes: int
    repos: tuple[str, ...]
    exclude: frozenset[str]


@dataclass
class RepoResult:
    state: State
    commit: str | None = None
    files: int = 0
    bytes: int = 0
    last_ok: str | None = None


def is_repo(value: object) -> bool:
    """A full repo, `host/group/.../repo`, with no `.` or `..` segment (the daemon's rule)."""
    return (
        isinstance(value, str)
        and REPO_ENTRY_RE.fullmatch(value) is not None
        and not any(part in {".", ".."} for part in value.split("/"))
    )


def canon_key(repo: str) -> str:
    """The key a repo is compared by: casefolded, no trailing `.git` (`exclude_keys`'s rule)."""
    key = repo.casefold()
    while key.endswith(".git"):
        key = key[: -len(".git")]
    return key


def is_excluded(repo: str, exclude: Iterable[str]) -> bool:
    """`repo`, or `repo` without `.wiki`, is one of the manifest's `exclude` keys."""
    key = canon_key(repo)
    keys = set(exclude)
    return key in keys or key.removesuffix(".wiki") in keys


def load_manifest(path: Path) -> Manifest:
    """`path`, validated strictly. Raises ManifestError, naming what is wrong."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{path}: unreadable: {exc}") from exc
    if not isinstance(data, dict) or set(data) != MANIFEST_KEYS:
        keys = sorted(data) if isinstance(data, dict) else type(data).__name__
        raise ManifestError(f"{path}: keys must be {sorted(MANIFEST_KEYS)}, not {keys}")
    server, generated = data["server"], data["generated_at"]
    refresh, repos, exclude = data["refresh_minutes"], data["repos"], data["exclude"]
    if not isinstance(server, str) or not SERVER_RE.fullmatch(server):
        raise ManifestError(f"{path}: bad server {server!r}")
    if server in RESERVED_SERVERS:
        raise ManifestError(f"{path}: {server!r} is a reserved name")
    if path.name != f"{server}.json":
        raise ManifestError(f"{path}: names server {server!r}")
    if not isinstance(generated, str):
        raise ManifestError(f"{path}: generated_at must be a string")
    if isinstance(refresh, bool) or not isinstance(refresh, int) or refresh < 1:
        raise ManifestError(f"{path}: refresh_minutes must be a positive integer")
    if not isinstance(repos, list) or not all(is_repo(r) for r in repos):
        raise ManifestError(f"{path}: repos must be a list of host/group/.../repo")
    if not isinstance(exclude, list) or not all(
        isinstance(e, str) and is_repo(e) and e == canon_key(e) for e in exclude
    ):
        raise ManifestError(f"{path}: exclude must be canonical repo keys")
    return Manifest(server, generated, refresh, tuple(repos), frozenset(exclude))


class SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to https on the same host: urllib would carry the token's
    header to wherever a redirect points."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise urllib.error.HTTPError(newurl, code, "redirect off the forge", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def forge_canonical(repo: str, token: str) -> str:
    """`repo`'s canonical `host/path`, from the forge's API: GitHub's `full_name`, GitLab's
    `path_with_namespace`. A same-host redirect is followed, which is the point; any other is
    refused. Raises LookupFailed."""
    host, path = repo.split("/", 1)
    if host == "github.com":
        if path.count("/") != 1:
            raise LookupFailed(f"{repo}: a GitHub repo is owner/name")
        url, key = f"https://api.github.com/repos/{path}", "full_name"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    else:
        encoded = urllib.parse.quote(path, safe="")
        url, key = f"https://{host}/api/v4/projects/{encoded}", "path_with_namespace"
        headers = {"PRIVATE-TOKEN": token}
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(SameHostRedirects())
    try:
        with opener.open(request, timeout=LOOKUP_TIMEOUT_S) as reply:
            data = json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        kind = NotFound if exc.code == 404 else LookupFailed
        raise kind(f"{repo}: the forge answered {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise LookupFailed(f"{repo}: {type(exc).__name__}") from None
    name = data.get(key) if isinstance(data, dict) else None
    if not isinstance(name, str) or not is_repo(f"{host}/{name}"):
        raise LookupFailed(f"{repo}: the forge's answer has no usable {key}")
    return f"{host}/{name}"


def https_url(repo: str) -> str:
    return f"https://{repo}.git"


def read_token(credential: Path) -> str:
    """The credential's first line. Raises OSError, or ValueError when it is empty or any
    group or other user can read it."""
    with credential.open(encoding="utf-8") as fh:
        mode = stat.S_IMODE(os.fstat(fh.fileno()).st_mode)
        if mode & 0o077:
            raise ValueError(f"{credential} is mode {mode:04o}: make it 0600 or 0400")
        token = fh.readline().strip()
    if not token:
        raise ValueError(f"{credential} is empty")
    return token


class Git:
    """git in a clean environment: no system or global config, no prompt, no lazy fetch, and a
    `GIT_ASKPASS` helper that reads the credential file, so the token is in no argv or env."""

    def __init__(self, home: Path, credential: Path, user: str) -> None:
        askpass = home / "askpass"
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  Username*) printf '%s\\n' \"$CREWKIOSK_GIT_USER\" ;;\n"
            '  *) head -n 1 -- "$CREWKIOSK_CREDENTIAL" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ASKPASS": str(askpass),
            "CREWKIOSK_CREDENTIAL": str(credential),
            "CREWKIOSK_GIT_USER": user,
        }

    def run(self, *args: str, cwd: Path | None = None) -> bytes:
        # The canonical path needs no redirect, and git would answer one with the token.
        argv = ["git", "-c", "credential.helper=", "-c", "http.followRedirects=false", *args]
        try:
            done = subprocess.run(
                argv, cwd=cwd, env=self.env, capture_output=True, timeout=GIT_TIMEOUT_S
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitFailed(f"git {args[0]}: {type(exc).__name__}") from None
        if done.returncode != 0:
            tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
            raise GitFailed(f"git {args[0]} exited {done.returncode}: {' '.join(tail)}")
        return done.stdout

    def clone(self, url: str, dest: Path) -> str:
        """A depth-1 clone of the default branch, with no checkout. Returns its commit."""
        self.run(
            "clone",
            "--depth",
            "1",
            # `blob:limit=<n>` leaves out blobs of n bytes or more: keep exactly MAX_FILE_BYTES.
            f"--filter=blob:limit={MAX_FILE_BYTES + 1}",
            "--no-checkout",
            "--single-branch",
            "--quiet",
            url,
            str(dest),
        )
        return self.run("rev-parse", "HEAD", cwd=dest).decode().strip()


def denied(path: str) -> bool:
    """A component of `path` is `.git`, a scanner control file, or on the denylist."""
    for part in path.split("/"):
        part = part.casefold()
        if part == ".git" or part in SCANNER_CONTROL:
            return True
        if any(fnmatch.fnmatchcase(part, pattern) for pattern in DENYLIST):
            return True
    return False


def is_text(data: bytes) -> bool:
    if b"\0" in data[:TEXT_PROBE_BYTES]:
        return False
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return True


def extract(git: Git, clone: Path, tree: Path) -> tuple[int, int]:
    """Write HEAD's kept files under `tree`, from the object store. Returns (files, bytes)."""
    # Only the `?<oid>` lines, as bytes: the others carry paths, which need not be UTF-8.
    missing = {
        line[1:].decode("ascii")
        for line in git.run(
            "rev-list", "--objects", "--missing=print", "HEAD", cwd=clone
        ).splitlines()
        if line.startswith(b"?")
    }
    entries: list[tuple[str, str]] = []
    listing = git.run("ls-tree", "-r", "-z", "--full-tree", "HEAD", cwd=clone)
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, kind, oid = meta.decode("ascii").split(" ")
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            continue  # a name no op could be asked for, nor a status file or log hold
        # 120000 is a symlink and 160000 a submodule: neither is copied.
        if kind != "blob" or mode not in {"100644", "100755"} or oid in missing:
            continue
        parts = path.split("/")
        if path.startswith("/") or any(p in {"", ".", ".."} for p in parts) or denied(path):
            continue
        entries.append((oid, path))
    sizes = _sizes(git, clone, [oid for oid, _ in entries])
    files = total = 0
    for oid, path in entries:
        size = sizes.get(oid)
        if size is None or size > MAX_FILE_BYTES:
            continue
        data = git.run("cat-file", "blob", oid, cwd=clone)
        if not is_text(data):
            continue
        dest = tree / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        files += 1
        total += len(data)
    return files, total


def _sizes(git: Git, clone: Path, oids: list[str]) -> dict[str, int]:
    """Each present blob's size, from one `cat-file --batch-check`."""
    if not oids:
        return {}
    argv = ["git", "-c", "credential.helper=", "cat-file", "--batch-check"]
    try:
        done = subprocess.run(
            argv,
            cwd=clone,
            env=git.env,
            input="".join(f"{oid}\n" for oid in oids).encode(),
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitFailed(f"git cat-file: {type(exc).__name__}") from None
    if done.returncode != 0:
        raise GitFailed(f"git cat-file exited {done.returncode}")
    sizes: dict[str, int] = {}
    for line in done.stdout.decode().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob":
            sizes[parts[0]] = int(parts[2])
    return sizes


@dataclass(frozen=True)
class Scan:
    clean: bool
    reason: str = ""
    rules: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


def shadow_name(n: int, src: Path) -> str:
    """`f<n>`, plus the suffix a path-scoped rule needs, if `src` has one: `f000001.yaml`."""
    m = RULE_SUFFIX.search(src.name)
    return f"f{n:06d}" + ("." + m.group(0).lstrip(".") if m else "")


def scan(gitleaks: str, tree: Path, work: Path) -> Scan:
    """`gitleaks dir` over a flat copy of `tree`: each file as `f<n>`, a hard link, or a
    rewrite with `gitleaks:allow` neutralized if it carries the marker. The default config
    allowlists paths (`node_modules/`, `vendor/`, `*.svg`, lock files and more), so a file
    scanned under its own path could ship unscanned; under `f<n>` no path matches. A file
    keeps a `RULE_SUFFIX` so the rules scoped to it still fire. gitleaks runs from inside the
    copy and scans `.`, so every path it matches is `f<n>`, never one under the server's
    directory, whose name is the server id's: a server named `node_modules` would otherwise
    match the allowlist. The copy holds no `.gitleaks.toml` for it to load. Findings are mapped
    back to the tree's paths. The report keeps only rule ids and paths."""
    shadow, report = work / "scan", work / "leaks.json"
    shadow.mkdir()
    names: dict[str, str] = {}
    for n, src in enumerate(sorted(p for p in tree.rglob("*") if p.is_file()), 1):
        dest = shadow / shadow_name(n, src)
        names[dest.name] = str(src.relative_to(tree))
        data = src.read_bytes()
        if ALLOW_MARKER in data:
            dest.write_bytes(data.replace(ALLOW_MARKER, ALLOW_NEUTRALIZED))
        else:
            os.link(src, dest)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")}
    argv = [
        gitleaks,
        "dir",
        "--no-banner",
        "--redact",
        "--exit-code",
        str(LEAKS_FOUND),
        "--report-format",
        "json",
        "--report-path",
        str(report),
        ".",
    ]
    try:
        done = subprocess.run(
            argv, cwd=shadow, env=env, capture_output=True, timeout=GIT_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Scan(False, f"gitleaks failed: {type(exc).__name__}")
    if done.returncode == 0:
        return Scan(True)
    if done.returncode != LEAKS_FOUND:
        return Scan(False, f"gitleaks exited {done.returncode}")
    try:
        findings = json.loads(report.read_text(encoding="utf-8"))
        rules = sorted({str(f["RuleID"]) for f in findings})
        paths = sorted({names[Path(f["File"]).name] for f in findings})
    except (OSError, ValueError, KeyError, TypeError):
        return Scan(False, "gitleaks found leaks, and its report is unreadable")
    return Scan(False, "gitleaks found leaks", tuple(rules), tuple(paths))


def set_modes(root: Path) -> None:
    """Dirs 0555 and files 0444, bottom-up, so a dir is writable until its contents are set."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), FILE_MODE, follow_symlinks=False)
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), DIR_MODE)
    os.chmod(root, DIR_MODE)


def remove_tree(root: Path) -> None:
    """`rmtree` a published tree, its 0555 dirs made writable first."""
    if not root.exists():
        return
    for dirpath, _, _ in os.walk(root):
        os.chmod(dirpath, 0o755)
    shutil.rmtree(root)


def drop_published(gen: Path, repo: str) -> None:
    """Remove `repo` from a published generation, its parent made writable only meanwhile."""
    target = gen / repo
    if not target.is_dir():
        return
    parent = target.parent
    os.chmod(parent, 0o755)
    try:
        remove_tree(target)
    finally:
        os.chmod(parent, DIR_MODE)


def discard(target: Path) -> None:
    """Remove a repo's partial copy from a generation still being built; never raises."""
    with contextlib.suppress(OSError):
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            remove_tree(target)


def nested_repos(repos: Iterable[str]) -> set[str]:
    """Each repo whose path lies under another's (`host/org/a/b` under `host/org/a`)."""
    listed = set(repos)
    return {r for r in listed if any(r.startswith(f"{s}/") for s in listed if s != r)}


def link_copy(src: Path, dest: Path) -> None:
    """A last good copy, as hard links: its files are 0444 and never written again."""
    for dirpath, _, filenames in os.walk(src):
        here = Path(dirpath)
        target = dest / here.relative_to(src)
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o755)
        for name in filenames:
            os.link(here / name, target / name)


class Builder:
    """Builds one server's corpus. The forge lookup and the clone URL are injectable, so a
    test can build from local bare repos."""

    def __init__(
        self,
        root: Path = CORPUS_ROOT,
        *,
        gitleaks: str = "gitleaks",
        credential: Callable[[str, str], Path] = lambda s, h: Path(
            CREDENTIAL.format(server=s, host=h)
        ),
        lookup: Lookup = forge_canonical,
        clone_url: CloneUrl = https_url,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.root = root
        self.gitleaks = gitleaks
        self.credential = credential
        self.lookup = lookup
        self.clone_url = clone_url
        self.clock = clock

    def status_path(self, server: str) -> Path:
        return self.root / "status" / f"{server}.json"

    def read_status(self, server: str) -> dict[str, Any]:
        try:
            data = json.loads(self.status_path(server).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def due(self, manifest: Manifest) -> bool:
        """Its refresh interval has passed since its last build, or its manifest changed. The
        daemon rewrites a manifest, and its `generated_at`, only when what it says changes, so
        a new `generated_at` catches a change to `exclude` alone."""
        status = self.read_status(manifest.server)
        try:
            built = datetime.fromisoformat(status["built_at"])
        except (KeyError, TypeError, ValueError):
            return True
        if set(status.get("repos") or {}) != set(manifest.repos):
            return True
        if status.get("manifest_at") != manifest.generated_at:
            return True
        return self.clock() - built >= timedelta(minutes=manifest.refresh_minutes)

    def resolve_excludes(self, manifest: Manifest) -> tuple[frozenset[str], frozenset[str]]:
        """The manifest's exclude keys plus each one's canonical path today, and the hosts
        whose excludes could not be resolved. A 404 means nothing by that name can be cloned,
        so it adds nothing. A host with no credential is never cloned from at all."""
        keys, blocked = set(manifest.exclude), set()
        for entry in sorted(manifest.exclude):
            host = entry.split("/", 1)[0]
            try:
                token = read_token(self.credential(manifest.server, host))
            except (OSError, ValueError):
                continue
            try:
                canonical = self.lookup(entry, token)
            except NotFound:
                continue
            except LookupFailed as exc:
                log.error(
                    "corpus %s: an exclude can't be resolved (%s): nothing from %s is served",
                    manifest.server,
                    exc,
                    host,
                )
                blocked.add(host)
                continue
            if canonical.split("/", 1)[0] == host:
                keys.add(canon_key(canonical))
        return frozenset(keys), frozenset(blocked)

    def orphans(self, servers: set[str]) -> list[str]:
        """Servers with a corpus or a status here but no manifest: research was turned off."""
        found: set[str] = set()
        for p in self.root.iterdir():
            if p.is_dir() and not p.is_symlink() and p.name not in RESERVED_SERVERS:
                found.add(p.name)
        status_dir = self.root / "status"
        if status_dir.is_dir():
            found |= {p.stem for p in status_dir.glob("*.json")}
        return sorted(s for s in found if SERVER_RE.fullmatch(s) and s not in servers)

    def remove_server(self, server: str) -> None:
        remove_tree(self.root / server)
        with contextlib.suppress(FileNotFoundError):
            self.status_path(server).unlink()
        log.warning("corpus %s: no manifest (research is off): its corpus is removed", server)

    def build(self, manifest: Manifest) -> dict[str, Any]:
        """Publish a new generation for `manifest`, swap `current` onto it, write the status."""
        server = manifest.server
        server_root = (self.root / server).absolute()
        server_root.mkdir(mode=SERVER_DIR_MODE, exist_ok=True)
        previous = self.read_status(server).get("repos") or {}
        current = server_root / "current"
        last_gen = current.resolve() if current.is_symlink() else None
        now = self.clock()
        work = Path(tempfile.mkdtemp(dir=server_root, prefix=".work-"))
        gen: Path | None = None
        swapped = False
        try:
            gen = self._new_gen(server_root, now)
            exclude, blocked = self.resolve_excludes(manifest)
            nested = nested_repos(manifest.repos)
            results: dict[str, RepoResult] = {}
            for repo in manifest.repos:
                if repo in nested:
                    # Its tree would land inside another repo's.
                    log.error("corpus %s: %s nests inside another pooled repo", server, repo)
                    results[repo] = RepoResult("clone_failed")
                    continue
                if repo.split("/", 1)[0] in blocked:
                    # It may be the excluded repo under a new name: no last good copy either.
                    results[repo] = RepoResult("clone_failed")
                    continue
                results[repo] = self._repo(
                    manifest, exclude, repo, work, gen, previous.get(repo), last_gen, now
                )
            set_modes(gen)
            link = server_root / "current.new"
            with contextlib.suppress(FileNotFoundError):
                link.unlink()
            link.symlink_to(gen.name)
            os.replace(link, current)
            swapped = True
            for old in server_root.glob("gen-*"):
                if old.resolve() not in {gen.resolve(), last_gen}:
                    remove_tree(old)
            # The previous generation, kept for a reader mid-read, never serves a repo the new
            # one doesn't: one excluded, blocked, failing today's checks or out of the manifest.
            if last_gen is not None:
                for repo in sorted(set(previous) | set(results)):
                    if not (gen / repo).is_dir():
                        drop_published(last_gen, repo)
        finally:
            remove_tree(work)
            if gen is not None and not swapped:
                remove_tree(gen)  # a half-built generation, never published
        status = {
            "server": server,
            "built_at": now.isoformat(),
            "manifest_at": manifest.generated_at,
            "repos": {repo: vars(r) for repo, r in sorted(results.items())},
        }
        self._write_status(server, status)
        return status

    def _new_gen(self, server_root: Path, now: datetime) -> Path:
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        n = 1
        while (server_root / f"gen-{stamp}-{n}").exists():
            n += 1
        gen = server_root / f"gen-{stamp}-{n}"
        gen.mkdir(mode=0o755)
        return gen

    def _repo(
        self,
        manifest: Manifest,
        exclude: frozenset[str],
        repo: str,
        work: Path,
        gen: Path,
        before: Any,
        last_gen: Path | None,
        now: datetime,
    ) -> RepoResult:
        """One repo, published into `gen`: fresh, its last good copy, or nothing. Any error (a
        full disk, a name too long, anything unforeseen) fails this repo only, never the
        build. The message of an error that is not an OSError is never logged: it might quote
        a file's contents."""
        server = manifest.server
        try:
            result = self._fresh(manifest, exclude, repo, work, gen)
        except Exception as exc:
            reason = exc.strerror if isinstance(exc, OSError) else None
            log.error("corpus %s: %s: %s", server, repo, reason or type(exc).__name__)
            discard(gen / repo)
            result = RepoResult("clone_failed")
        if result.state == "ok":
            result.last_ok = now.isoformat()
            return result
        if result.state == "excluded":
            return result
        old = last_gen / repo if last_gen is not None else None
        if old is not None and old.is_dir() and isinstance(before, dict):
            try:
                stale = self._recheck(old, work)
            except Exception as exc:
                stale = f"the recheck failed: {type(exc).__name__}"
            if stale is not None:
                log.error(
                    "corpus %s: %s is %s, and its last good copy fails today's checks (%s): "
                    "serving nothing",
                    server,
                    repo,
                    result.state,
                    stale,
                )
                return RepoResult(result.state)
            try:
                link_copy(old, gen / repo)
            except OSError as exc:
                log.error(
                    "corpus %s: %s: its last good copy: %s",
                    server,
                    repo,
                    exc.strerror or type(exc).__name__,
                )
                discard(gen / repo)
                return RepoResult(result.state)
            log.warning(
                "corpus %s: %s is %s, serving its last good copy", server, repo, result.state
            )
            return RepoResult(
                result.state,
                commit=before.get("commit"),
                files=int(before.get("files") or 0),
                bytes=int(before.get("bytes") or 0),
                last_ok=before.get("last_ok"),
            )
        return result

    def _recheck(self, old: Path, work: Path) -> str | None:
        """Why a last good copy would fail today's denylist or gitleaks, or None if it
        passes: a rule or a denylist entry added since it was built applies to it too."""
        for p in sorted(old.rglob("*")):
            rel = str(p.relative_to(old))
            if p.is_file() and denied(rel):
                return f"denylisted {rel}"
        found = scan(self.gitleaks, old, Path(tempfile.mkdtemp(dir=work, prefix="recheck-")))
        if found.clean:
            return None
        return f"{found.reason}: rules {', '.join(found.rules) or '-'}"

    def _fresh(
        self, manifest: Manifest, exclude: frozenset[str], repo: str, work: Path, gen: Path
    ) -> RepoResult:
        server = manifest.server
        here = Path(tempfile.mkdtemp(dir=work, prefix="repo-"))
        host = repo.split("/", 1)[0]
        credential = self.credential(server, host)
        try:
            token = read_token(credential)
        except (OSError, ValueError) as exc:
            # No token for this forge: nothing is asked of it, and no other forge's is sent.
            log.error("corpus %s: %s: no credential for %s: %s", server, repo, host, _no_token(exc))
            return RepoResult("clone_failed")
        try:
            canonical = self.lookup(repo, token)
        except LookupFailed as exc:
            log.error("corpus %s: %s: lookup failed: %s", server, repo, exc)
            return RepoResult("clone_failed")
        if canonical.split("/", 1)[0] != host:
            log.error("corpus %s: %s resolves off its forge, to %s", server, repo, canonical)
            return RepoResult("clone_failed")
        if _org(canonical) != _org(repo):
            # The daemon admitted `repo` by its host/org; nothing vouched for this one.
            log.warning(
                "corpus %s: %s resolves to %s, in another org: not cloned", server, repo, canonical
            )
            return RepoResult("excluded")
        if is_excluded(canonical, exclude):
            log.warning(
                "corpus %s: %s resolves to %s, which is excluded: not cloned",
                server,
                repo,
                canonical,
            )
            return RepoResult("excluded")
        home, clone, tree = here / "home", here / "clone", here / "tree"
        home.mkdir()
        tree.mkdir()
        user = "x-access-token" if canonical.startswith("github.com/") else "oauth2"
        git = Git(home, credential, user)
        try:
            commit = git.clone(self.clone_url(canonical), clone)
            files, size = extract(git, clone, tree)
        except GitFailed as exc:
            log.error("corpus %s: %s: %s", server, repo, exc)
            return RepoResult("clone_failed")
        found = scan(self.gitleaks, tree, here)
        if not found.clean:
            log.error(
                "corpus %s: %s withheld: %s (rules %s, files %s)",
                server,
                repo,
                found.reason,
                ", ".join(found.rules) or "-",
                ", ".join(found.paths) or "-",
            )
            return RepoResult("withheld")
        (gen / repo).parent.mkdir(parents=True, exist_ok=True)
        os.rename(tree, gen / repo)
        log.info("corpus %s: %s at %s, %d files", server, repo, commit[:12], files)
        return RepoResult("ok", commit=commit, files=files, bytes=size)

    def _write_status(self, server: str, status: dict[str, Any]) -> None:
        path = self.status_path(server)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            os.fchmod(fd, STATUS_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(status, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise


def _org(repo: str) -> str:
    """`host/org`, casefolded: what the daemon's allowlist admits a repo by."""
    return "/".join(canon_key(repo).split("/")[:2])


def _no_token(exc: BaseException) -> str:
    """An exception's message, which never carries the token: lookups raise their own."""
    return str(exc) if isinstance(exc, LookupFailed) else type(exc).__name__


def run(pool_dir: Path, builder: Builder) -> int:
    """Build every due server, and remove the corpus of any server with no manifest. 2 if
    gitleaks, the corpus root or a listable pool dir is missing (nothing is published or
    removed), 1 if a manifest was malformed or a server's build failed, else 0. One server's
    failure never stops the others. A withheld or failed repo is a state in `health`, not an
    error."""
    if shutil.which(builder.gitleaks) is None:
        log.error("corpus: no gitleaks (%s): nothing is published unscanned", builder.gitleaks)
        return 2
    if not builder.root.is_dir():
        # Its mode and group are tmpfiles' to set (packaging/crewcorpus.tmpfiles.conf).
        log.error("corpus: no %s: install packaging/crewcorpus.tmpfiles.conf", builder.root)
        return 2
    try:
        # Listed, not globbed: an unreadable pool dir must never read as "no servers", which
        # would remove every corpus below.
        names = sorted(n for n in os.listdir(pool_dir) if n.endswith(".json"))
    except OSError as exc:
        log.error("corpus: cannot list %s: %s", pool_dir, exc.strerror or type(exc).__name__)
        return 2
    code = 0
    # A malformed manifest still names a server that has research on: its corpus stays.
    for server in builder.orphans({n.removesuffix(".json") for n in names}):
        try:
            builder.remove_server(server)
        except OSError as exc:
            log.error("corpus %s: cannot remove its corpus: %s", server, exc.strerror)
            code = 1
    for path in (pool_dir / n for n in names):
        try:
            manifest = load_manifest(path)
        except ManifestError as exc:
            log.error("corpus: skipping a manifest: %s", exc)
            code = 1
            continue
        if not builder.due(manifest):
            log.info("corpus %s: built within %d min", manifest.server, manifest.refresh_minutes)
            continue
        try:
            builder.build(manifest)
        except Exception as exc:
            log.error(
                "corpus %s: the build failed: %s: %s", manifest.server, type(exc).__name__, exc
            )
            code = 1
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crewkiosk-corpus")
    parser.add_argument("--pool-dir", type=Path, default=POOL_DIR)
    parser.add_argument("--root", type=Path, default=CORPUS_ROOT)
    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return run(ns.pool_dir, Builder(ns.root))
