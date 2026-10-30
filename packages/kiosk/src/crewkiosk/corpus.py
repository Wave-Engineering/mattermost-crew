"""crewkiosk-corpus: builds each research server's corpus from its pool manifest (#140; D-111).

A timer runs it as `crewcorpus`. For each `/var/lib/crewchat-pool/<server>.json` the daemon
writes (#139), it publishes a filtered, scrubbed, read-only copy of every listed repo's default
branch at `/var/lib/crewcorpus/<server>/current/<repo>/...`, for `crewkiosk-broker` to serve.
The kiosk never sees it.

Per repo:

1. **Resolve its canonical path** through the forge API, with the server's corpus credential.
   A forge keeps redirects after a rename or transfer, so a manifest path can lead to a repo
   the operator excluded under its new name. A canonical path that is excluded, with or without
   `.wiki`, is never cloned, and its last good copy is dropped. A lookup that fails is
   `clone_failed`: the unresolved path is never cloned.
2. **Clone** the canonical path at depth 1, blobs over 5 MB left out, no checkout. The token
   reaches git through `GIT_ASKPASS`, which reads the credential file itself, so it is never in
   an argv, a URL or the environment.
3. **Extract** the tree from the object store, not a checkout: a checkout would fetch every
   blob the filter left out. Only regular files of 5 MB or less that are UTF-8 text with no NUL
   are kept. Symlinks, submodules and `.git` never reach the tree, nor does any path with a
   component on the denylist.
4. **Scan** it with `gitleaks dir`. A finding or a gitleaks error withholds the repo, and its
   last good copy is served. Only the rule ids and file paths are logged, never a value. A repo
   may not exempt itself: `.gitleaks.toml` and `.gitleaksignore` never reach the tree, gitleaks
   runs from an empty directory with no `GITLEAKS_*` variables, and it scans a copy in which
   `gitleaks:allow` is neutralized. A missing gitleaks publishes nothing at all.

Each server is published as a new generation, `gen-<utc>-<n>`, with dirs 0555 and files 0444,
and `current` is swapped onto it atomically. Generations older than the previous one are
removed. `status/<server>.json` (0644) records each repo's state for the daemon's `health`.
It never holds a secret, a finding's value or a credential. A server is rebuilt once its
`refresh_minutes` have passed since its last build, or sooner if its manifest's repos change.

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
CREDENTIAL = "/etc/crewkiosk/corpus-{server}.credential"
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

State = Literal["ok", "withheld", "clone_failed", "excluded"]
Lookup = Callable[[str, str], str]
CloneUrl = Callable[[str], str]


class ManifestError(ValueError):
    pass


class LookupFailed(RuntimeError):
    pass


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


def forge_canonical(repo: str, token: str) -> str:
    """`repo`'s canonical `host/path`, from the forge's API: GitHub's `full_name`, GitLab's
    `path_with_namespace`. A redirect is followed, which is the point. Raises LookupFailed."""
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
    try:
        with urllib.request.urlopen(request, timeout=LOOKUP_TIMEOUT_S) as reply:
            data = json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        raise LookupFailed(f"{repo}: the forge answered {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise LookupFailed(f"{repo}: {type(exc).__name__}") from None
    name = data.get(key) if isinstance(data, dict) else None
    if not isinstance(name, str) or not is_repo(f"{host}/{name}"):
        raise LookupFailed(f"{repo}: the forge's answer has no usable {key}")
    return f"{host}/{name}"


def https_url(repo: str) -> str:
    return f"https://{repo}.git"


def read_token(credential: Path) -> str:
    """The credential's first line. Raises OSError, or ValueError when it is empty."""
    with credential.open(encoding="utf-8") as fh:
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
        argv = ["git", "-c", "credential.helper=", *args]
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
            "--filter=blob:limit=5m",
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
    missing = {
        line[1:]
        for line in git.run("rev-list", "--objects", "--missing=print", "HEAD", cwd=clone)
        .decode()
        .splitlines()
        if line.startswith("?")
    }
    entries: list[tuple[str, str]] = []
    listing = git.run("ls-tree", "-r", "-z", "--full-tree", "HEAD", cwd=clone)
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, kind, oid = meta.decode().split(" ")
        path = os.fsdecode(raw_path)
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
    done = subprocess.run(
        argv,
        cwd=clone,
        env=git.env,
        input="".join(f"{oid}\n" for oid in oids).encode(),
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
    )
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


def scan(gitleaks: str, tree: Path, work: Path) -> Scan:
    """`gitleaks dir` over a copy of `tree` with `gitleaks:allow` neutralized: hard links,
    except for the files that carry the marker. The report keeps only rule ids and paths."""
    shadow, empty, report = work / "scan", work / "empty", work / "leaks.json"
    empty.mkdir()
    for src in sorted(p for p in tree.rglob("*") if p.is_file()):
        dest = shadow / src.relative_to(tree)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        if ALLOW_MARKER in data:
            dest.write_bytes(data.replace(ALLOW_MARKER, ALLOW_NEUTRALIZED))
        else:
            os.link(src, dest)
    shadow.mkdir(exist_ok=True)
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
        str(shadow),
    ]
    try:
        done = subprocess.run(argv, cwd=empty, env=env, capture_output=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Scan(False, f"gitleaks failed: {type(exc).__name__}")
    if done.returncode == 0:
        return Scan(True)
    if done.returncode != LEAKS_FOUND:
        return Scan(False, f"gitleaks exited {done.returncode}")
    try:
        findings = json.loads(report.read_text(encoding="utf-8"))
        rules = sorted({str(f["RuleID"]) for f in findings})
        paths = sorted({str(Path(f["File"]).relative_to(shadow)) for f in findings})
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
        credential: Callable[[str], Path] = lambda s: Path(CREDENTIAL.format(server=s)),
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
        """Its refresh interval has passed since its last build, or its repos changed."""
        status = self.read_status(manifest.server)
        try:
            built = datetime.fromisoformat(status["built_at"])
        except (KeyError, TypeError, ValueError):
            return True
        if set(status.get("repos") or {}) != set(manifest.repos):
            return True
        return self.clock() - built >= timedelta(minutes=manifest.refresh_minutes)

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
        try:
            gen = self._new_gen(server_root, now)
            results = {
                repo: self._repo(manifest, repo, work, gen, previous.get(repo), last_gen, now)
                for repo in manifest.repos
            }
            set_modes(gen)
            link = server_root / "current.new"
            with contextlib.suppress(FileNotFoundError):
                link.unlink()
            link.symlink_to(gen.name)
            os.replace(link, current)
            for old in server_root.glob("gen-*"):
                if old.resolve() not in {gen.resolve(), last_gen}:
                    remove_tree(old)
            # An excluded repo's last good copy goes now, not with the previous generation.
            for repo, r in results.items():
                if r.state == "excluded" and last_gen is not None:
                    drop_published(last_gen, repo)
        finally:
            remove_tree(work)
        status = {
            "server": server,
            "built_at": now.isoformat(),
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
        repo: str,
        work: Path,
        gen: Path,
        before: Any,
        last_gen: Path | None,
        now: datetime,
    ) -> RepoResult:
        """One repo, published into `gen`: fresh, its last good copy, or nothing."""
        server = manifest.server
        result = self._fresh(manifest, repo, work, gen)
        if result.state == "ok":
            result.last_ok = now.isoformat()
            return result
        if result.state == "excluded":
            return result
        old = last_gen / repo if last_gen is not None else None
        if old is not None and old.is_dir() and isinstance(before, dict):
            link_copy(old, gen / repo)
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

    def _fresh(self, manifest: Manifest, repo: str, work: Path, gen: Path) -> RepoResult:
        server = manifest.server
        here = Path(tempfile.mkdtemp(dir=work, prefix="repo-"))
        credential = self.credential(server)
        try:
            token = read_token(credential)
            canonical = self.lookup(repo, token)
        except (OSError, ValueError, LookupFailed) as exc:
            log.error("corpus %s: %s: lookup failed: %s", server, repo, _no_token(exc))
            return RepoResult("clone_failed")
        if is_excluded(canonical, manifest.exclude):
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


def _no_token(exc: BaseException) -> str:
    """An exception's message, which never carries the token: lookups raise their own."""
    return str(exc) if isinstance(exc, LookupFailed) else type(exc).__name__


def run(pool_dir: Path, builder: Builder) -> int:
    """Build every due server. 2 if gitleaks or the corpus root is missing (nothing is
    published), 1 if a manifest was malformed, else 0. A withheld or failed repo is a state in
    `health`, not an error."""
    if shutil.which(builder.gitleaks) is None:
        log.error("corpus: no gitleaks (%s): nothing is published unscanned", builder.gitleaks)
        return 2
    if not builder.root.is_dir():
        # Its mode and group are tmpfiles' to set (packaging/crewcorpus.tmpfiles.conf).
        log.error("corpus: no %s: install packaging/crewcorpus.tmpfiles.conf", builder.root)
        return 2
    code = 0
    for path in sorted(pool_dir.glob("*.json")):
        try:
            manifest = load_manifest(path)
        except ManifestError as exc:
            log.error("corpus: skipping a manifest: %s", exc)
            code = 1
            continue
        if not builder.due(manifest):
            log.info("corpus %s: built within %d min", manifest.server, manifest.refresh_minutes)
            continue
        builder.build(manifest)
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
