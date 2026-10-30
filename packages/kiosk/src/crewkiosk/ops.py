"""The broker's typed corpus ops (#141; D-111, D-112): `corpus_search`, `corpus_read` and
`corpus_list`, over one research server's published corpus.

An op names no server: its `Corpus` is the one bound to the socket the call arrived on. Every
op checks its scope first. The repo must be in that server's pool manifest and published under
`<root>/<server>/current/`. A path must be relative, with no `..` component and no NUL, and must
resolve (`realpath`) under the repo's root. The corpus holds no symlinks (#140), so the last
check is a backstop. A refusal is a `ToolError`, which the broker returns as an MCP tool error.

Arguments are checked against each op's schema, the one `tools/list` publishes: no property it
doesn't name, every string and integer bounded. No op takes a command, a URL or a path outside
the pool. `corpus_search` runs `rg --fixed-strings` over each repo in turn, with no config
file, no ignore files and no symlinks followed, and its query always comes after `--`.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A response's encoded size. A search result's line is cut to TEXT_MAX characters.
MAX_RESPONSE_BYTES = 64 * 1024
TEXT_MAX = 300
LIST_MAX = 2000
RG = "rg"
RG_TIMEOUT_S = 20
RG_PATH = "/usr/local/bin:/usr/bin:/bin"


class ToolError(Exception):
    """An op refused or failed. Its message goes back to the kiosk, so it never holds a
    filesystem path outside the corpus."""


@dataclass(frozen=True)
class Field:
    kind: type
    required: bool = False
    default: Any = None
    max_len: int | None = None  # str
    minimum: int | None = None  # int
    maximum: int | None = None  # int
    description: str = ""

    def schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "string" if self.kind is str else "integer"}
        if self.max_len is not None:
            out["maxLength"] = self.max_len
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.default is not None:
            out["default"] = self.default
        if self.description:
            out["description"] = self.description
        return out


REPO = Field(str, required=True, max_len=255, description="a pooled repo, host/group/.../repo")
TOOLS: dict[str, tuple[str, dict[str, Field]]] = {
    "corpus_search": (
        "Search the pooled repos for a fixed string (not a regex). Up to 5 matches a file.",
        {
            "query": Field(str, required=True, max_len=200, description="the text to find"),
            "repo": Field(str, max_len=255, description="only this pooled repo"),
            "path_glob": Field(str, max_len=100, description="only paths matching this glob"),
            "max_results": Field(int, default=20, minimum=1, maximum=50),
        },
    ),
    "corpus_read": (
        "Read a file from a pooled repo, as numbered lines, with the commit it came from.",
        {
            "repo": REPO,
            "path": Field(str, required=True, max_len=1024, description="relative to the repo"),
            "offset": Field(int, default=0, minimum=0, description="the first line, from 0"),
            "limit": Field(int, default=400, minimum=1, maximum=2000),
        },
    ),
    "corpus_list": (
        "List the files of a pooled repo, optionally matching a glob.",
        {"repo": REPO, "glob": Field(str, max_len=100, description="e.g. docs/*.md")},
    ),
}


def tool_list() -> list[dict[str, Any]]:
    """What `tools/list` returns: exactly the three ops, each with a strict schema."""
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": {key: f.schema() for key, f in fields.items()},
                "required": [key for key, f in fields.items() if f.required],
                "additionalProperties": False,
            },
        }
        for name, (description, fields) in TOOLS.items()
    ]


def check_args(name: str, args: object) -> dict[str, Any]:
    """`args` for op `name`, defaults filled in, or a ToolError naming what is wrong."""
    if name not in TOOLS:
        raise ToolError(f"unknown tool {name!r}")
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    fields = TOOLS[name][1]
    extra = sorted(set(args) - set(fields))
    if extra:
        raise ToolError(f"unknown argument {extra[0]!r}")
    out: dict[str, Any] = {}
    for key, f in fields.items():
        if key not in args or args[key] is None:
            if f.required:
                raise ToolError(f"{key} is required")
            if f.default is not None:
                out[key] = f.default
            continue
        value = args[key]
        # bool is an int to Python, never to the schema.
        if isinstance(value, bool) or not isinstance(value, f.kind):
            raise ToolError(f"{key} must be a{'n integer' if f.kind is int else ' string'}")
        if isinstance(value, str):
            if f.max_len is not None and len(value) > f.max_len:
                raise ToolError(f"{key} is longer than {f.max_len} characters")
            if "\0" in value:
                raise ToolError(f"{key} holds a NUL")
        elif isinstance(value, int) and (
            (f.minimum is not None and value < f.minimum)
            or (f.maximum is not None and value > f.maximum)
        ):
            raise ToolError(f"{key} must be from {f.minimum} to {f.maximum}")
        out[key] = value
    return out


class Corpus:
    """One research server's corpus: its manifest's repos, published under `root/server`."""

    def __init__(self, server: str, repos: tuple[str, ...], root: Path, *, rg: str = RG):
        self.server = server
        self.repos = repos
        self.root = root
        self.rg = rg

    def call(self, name: str, args: object) -> dict[str, Any]:
        checked = check_args(name, args)
        if name == "corpus_search":
            return self.search(**checked)
        if name == "corpus_read":
            return self.read(**checked)
        return self.list(**checked)

    # -- scope ----------------------------------------------------------------------------
    def repo_root(self, repo: str) -> Path:
        """The repo's published root, resolved, or a ToolError: never another server's."""
        if repo not in self.repos:
            raise ToolError(f"{repo!r} is not in this server's pool")
        current = Path(os.path.realpath(self.root / self.server / "current"))
        top = Path(os.path.realpath(current / repo))
        if not top.is_relative_to(current) or not top.is_dir():
            raise ToolError(f"{repo!r} is not published (withheld, failed, or not built yet)")
        return top

    def resolve(self, top: Path, path: str) -> Path:
        """`path` under `top`: relative, no `..`, no NUL, and still under `top` once real."""
        parts = path.split("/")
        if not path or path.startswith("/") or "\0" in path or ".." in parts:
            raise ToolError(f"path {path!r} must be relative to the repo, with no '..'")
        real = Path(os.path.realpath(top / path))
        if not real.is_relative_to(top):
            raise ToolError(f"path {path!r} is outside the repo")
        return real

    def commit(self, repo: str) -> str | None:
        """The repo's commit, from the builder's status file (#140), if it says."""
        try:
            status = json.loads((self.root / "status" / f"{self.server}.json").read_text())
            commit = status["repos"][repo]["commit"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return commit if isinstance(commit, str) else None

    # -- the ops --------------------------------------------------------------------------
    def read(self, repo: str, path: str, offset: int, limit: int) -> dict[str, Any]:
        top = self.repo_root(repo)
        real = self.resolve(top, path)
        if not real.is_file():
            raise ToolError(f"{path!r} is not a file in {repo}")
        try:
            text = real.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"{path!r} cannot be read: {exc.strerror}") from None
        all_lines = text.splitlines()
        lines: list[dict[str, Any]] = []
        size, truncated = 0, False
        for n, line in enumerate(all_lines[offset : offset + limit], offset):
            size += len(line.encode()) + 16
            if size > MAX_RESPONSE_BYTES:
                truncated = True
                break
            lines.append({"n": n, "text": line})
        return {
            "repo": repo,
            "path": path,
            "commit": self.commit(repo),
            "total_lines": len(all_lines),
            "lines": lines,
            "truncated": truncated or offset + limit < len(all_lines),
        }

    def list(self, repo: str, glob: str | None = None) -> dict[str, Any]:
        top = self.repo_root(repo)
        paths: list[str] = []
        truncated = False
        for rel in _walk(top):
            if glob is not None and not fnmatch.fnmatchcase(rel, glob):
                continue
            if len(paths) == LIST_MAX:
                truncated = True
                break
            paths.append(rel)
        return {"repo": repo, "commit": self.commit(repo), "paths": paths, "truncated": truncated}

    def search(
        self,
        query: str,
        max_results: int,
        repo: str | None = None,
        path_glob: str | None = None,
    ) -> dict[str, Any]:
        if not query.strip() or any(c in query for c in "\r\n"):
            raise ToolError("query must be one line of text")
        repos = [repo] if repo is not None else list(self.repos)
        tops: list[tuple[str, Path]] = []
        for name in repos:
            try:
                tops.append((name, self.repo_root(name)))
            except ToolError:
                if repo is not None:
                    raise
        results: list[dict[str, Any]] = []
        size, truncated = 0, False
        for name, top in tops:
            for path, line, text in self._rg(top, query, path_glob):
                hit = {"repo": name, "path": path, "line": line, "text": text[:TEXT_MAX]}
                size += len(json.dumps(hit).encode())
                if len(results) == max_results or size > MAX_RESPONSE_BYTES:
                    truncated = True
                    break
                results.append(hit)
            if truncated:
                break
        return {"query": query, "results": results, "truncated": truncated}

    def _rg(self, top: Path, query: str, glob: str | None) -> Iterator[tuple[str, int, str]]:
        """`(path, line, text)` for each match in one repo, from `rg --json`."""
        argv = [
            self.rg,
            "--json",
            "--no-config",
            "--no-ignore",
            "--hidden",
            "--no-follow",
            "--fixed-strings",
            "--max-count",
            "5",
            "--max-filesize",
            "5M",
            "--no-messages",
        ]
        if glob is not None:
            argv += ["--glob", glob]
        argv += ["--", query, "."]
        try:
            done = subprocess.run(
                argv,
                cwd=top,
                env={"PATH": RG_PATH, "LC_ALL": "C.UTF-8"},
                capture_output=True,
                timeout=RG_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise ToolError("search is unavailable on this host (no rg)") from None
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError(f"search failed: {type(exc).__name__}") from None
        # 0 found, 1 found nothing; 2 an error, though rg may still have printed matches.
        if done.returncode not in (0, 1, 2):
            raise ToolError(f"search failed: rg exited {done.returncode}")
        for raw in done.stdout.splitlines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") != "match":
                continue
            data = event["data"]
            path = data.get("path", {}).get("text")
            text = data.get("lines", {}).get("text")
            if not isinstance(path, str) or not isinstance(text, str):
                continue
            yield path.removeprefix("./"), int(data.get("line_number") or 0), text.rstrip("\n")


def _walk(top: Path) -> Iterator[str]:
    """Every regular file under `top`, relative and sorted, never following a symlink."""
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames.sort()
        base = Path(dirpath).relative_to(top)
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_file() and not full.is_symlink():
                yield str(base / name) if str(base) != "." else name
