"""A local forge for the corpus builder (#140): bare repos served over `file://`, and a fake
gitleaks that flags any file containing `PLANTED`, so the unit job needs no gitleaks.
IT-29 uses the forge with the real gitleaks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crewkiosk.corpus import Manifest

TOKEN = "corpus-token-7f3a9c"  # the credential's value: no argv, URL, env, log or file holds it
SERVER = "dev"
FORGE_HOST = "forge.test"
CLEAN_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@forge.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@forge.test",
}

FAKE_GITLEAKS = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
report = Path(args[args.index("--report-path") + 1])
target = Path(args[-1])
log = Path(os.environ.get("FAKE_GITLEAKS_LOG", os.devnull))
seen = {{"cwd": os.getcwd(), "cwd_entries": os.listdir("."), "args": args,
        "env": sorted(k for k in os.environ if k.startswith("GITLEAKS_")),
        "files": {{str(p.relative_to(target)): p.read_text()
                  for p in target.rglob("*") if p.is_file()}}}}
with log.open("a") as fh:
    fh.write(json.dumps(seen) + "\\n")
if os.environ.get("FAKE_GITLEAKS_EXIT"):
    sys.exit(int(os.environ["FAKE_GITLEAKS_EXIT"]))
hits = [p for p in target.rglob("*") if p.is_file() and "PLANTED" in p.read_text()]
report.write_text(json.dumps([{{"RuleID": "fake-rule", "File": str(p), "Secret": "REDACTED",
                               "Match": "REDACTED"}} for p in hits]))
sys.exit(3 if hits else 0)
"""


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=CLEAN_ENV, check=True, capture_output=True, text=True
    ).stdout


@dataclass
class Forge:
    """Bare repos at `<root>/<host>/<group>/<name>.git`, and every repo a clone URL was made for."""

    root: Path
    cloned: list[str] = field(default_factory=list)

    def repo(
        self, name: str, files: dict[str, bytes], symlinks: dict[str, str] | None = None
    ) -> str:
        """Commit exactly `files` (and `symlinks`, path → target) as `forge.test/org/<name>`,
        on top of any earlier commit. Returns the repo path."""
        repo = f"{FORGE_HOST}/org/{name}"
        work = self.root / "work" / name
        bare = self.root / f"{repo}.git"
        if not work.exists():
            work.mkdir(parents=True)
            git("init", "-q", "-b", "main", str(work))
        for p in work.iterdir():
            if p.name == ".git":
                continue
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
        for path, data in files.items():
            dest = work / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        for path, target in (symlinks or {}).items():
            (work / path).symlink_to(target)
        git("add", "-A", cwd=work)
        git("commit", "-q", "--allow-empty", "-m", "c", cwd=work)
        if bare.exists():
            git("push", "-q", "--force", str(bare), "main", cwd=work)
        else:
            bare.parent.mkdir(parents=True, exist_ok=True)
            git("clone", "-q", "--bare", str(work), str(bare))
            # As a real forge does: without it, a file:// clone ignores --filter, and the
            # builder's missing-blob path would never run.
            git("config", "uploadpack.allowFilter", "true", cwd=bare)
        return repo

    def head(self, repo: str) -> str:
        return git("rev-parse", "HEAD", cwd=self.root / f"{repo}.git").strip()

    def url(self, repo: str) -> str:
        self.cloned.append(repo)
        return f"file://{self.root}/{repo}.git"


def install_fake_gitleaks(bindir: Path) -> Path:
    script = bindir / "gitleaks"
    bindir.mkdir(parents=True, exist_ok=True)
    script.write_text(FAKE_GITLEAKS)
    script.chmod(0o755)
    return script


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, minutes: int = 16) -> None:
        self.now += timedelta(minutes=minutes)


def manifest(*repos: str, exclude: tuple[str, ...] = (), refresh: int = 15) -> Manifest:
    return Manifest(SERVER, "2026-10-30T12:00:00+00:00", refresh, repos, frozenset(exclude))


def write_manifest(
    pool: Path, server: str = SERVER, *, filename: str | None = None, **body: object
) -> Path:
    """A manifest file as the daemon writes it, at `<filename or server>.json`, with `body`
    overriding its keys."""
    data: dict[str, object] = {
        "server": server,
        "generated_at": "2026-10-30T12:00:00+00:00",
        "refresh_minutes": 15,
        "repos": [],
        "exclude": [],
    }
    data.update(body)
    pool.mkdir(parents=True, exist_ok=True)
    path = pool / f"{filename or server}.json"
    path.write_text(json.dumps(data))
    return path
