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
file, no ignore files and no symlinks followed, and its query always comes after `--`. Its
output is read as a stream, one bounded line at a time, and rg is killed as soon as the search
has its results or has read RG_OUTPUT_MAX bytes: a one-character query over a large repo never
lands in memory whole.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import subprocess
import threading
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A result's size as JSON-encoded, the form the broker sends. A search result's line is cut to
# TEXT_MAX characters.
MAX_RESPONSE_BYTES = 64 * 1024
TEXT_MAX = 300
LIST_MAX = 2000
RG = "rg"
RG_TIMEOUT_S = 20
RG_PATH = "/usr/local/bin:/usr/bin:/bin"
# rg's JSON printer ignores --max-columns, so a minified 5 MB file is one 5 MB event: each
# line is read at most RG_LINE_MAX bytes, the rest of a longer one dropped, and one search
# reads at most RG_OUTPUT_MAX bytes of rg's output across its repos.
RG_LINE_MAX = 64 * 1024
RG_OUTPUT_MAX = 4 * 1024 * 1024
NAME_MAX = 64


class ToolError(Exception):
    """An op refused or failed. Its message goes back to the kiosk, so it never holds a
    filesystem path outside the corpus."""


class _Spent(Exception):
    """A search read its RG_OUTPUT_MAX of rg's output."""


@dataclass
class _Budget:
    """The bytes of rg's output a search may still read, across its repos."""

    left: int = RG_OUTPUT_MAX

    def spend(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise _Spent


def printable(value: object, limit: int = NAME_MAX) -> str:
    """A name the kiosk chose, as a log line, an audit line or a refusal may echo it: cut to
    `limit` characters, anything but printable ASCII as `?`."""
    return "".join(c if c.isascii() and c.isprintable() else "?" for c in str(value)[:limit])


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
        raise ToolError(f"unknown tool {printable(name)!r}")
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    fields = TOOLS[name][1]
    extra = sorted(set(args) - set(fields))
    if extra:
        raise ToolError(f"unknown argument {printable(extra[0])!r}")
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
        out: dict[str, Any] = {
            "repo": repo,
            "path": path,
            "commit": self.commit(repo),
            "total_lines": len(all_lines),
            "lines": [],
            "truncated": True,
        }
        size = _encoded(out)
        for n, line in enumerate(all_lines[offset : offset + limit], offset):
            entry = {"n": n, "text": line}
            size += _encoded(entry) + 2
            if size > MAX_RESPONSE_BYTES:
                return out
            out["lines"].append(entry)
        out["truncated"] = offset + limit < len(all_lines)
        return out

    def list(self, repo: str, glob: str | None = None) -> dict[str, Any]:
        top = self.repo_root(repo)
        paths: list[str] = []
        truncated = False
        size = _encoded({"repo": repo, "commit": "0" * 40, "paths": [], "truncated": True})
        for rel in _walk(top):
            if glob is not None and not fnmatch.fnmatchcase(rel, glob):
                continue
            size += len(json.dumps(rel)) + 2
            if len(paths) == LIST_MAX or size > MAX_RESPONSE_BYTES:
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
        size, truncated = _encoded({"query": query, "results": [], "truncated": True}), False
        budget = _Budget(RG_OUTPUT_MAX)
        try:
            for name, top in tops:
                # closing(): a break kills this repo's rg now, not when the generator is freed.
                with contextlib.closing(self._rg(top, query, path_glob, budget)) as hits:
                    for path, line, text in hits:
                        hit = {"repo": name, "path": path, "line": line, "text": text[:TEXT_MAX]}
                        size += _encoded(hit) + 2
                        if len(results) == max_results or size > MAX_RESPONSE_BYTES:
                            truncated = True
                            break
                        results.append(hit)
                if truncated:
                    break
        except _Spent:
            truncated = True
        return {"query": query, "results": results, "truncated": truncated}

    def _rg(
        self, top: Path, query: str, glob: str | None, budget: _Budget
    ) -> Generator[tuple[str, int, str], None, None]:
        """`(path, line, text)` for each match in one repo, from `rg --json`, read as it
        comes. Every byte read is spent from `budget`; past it, `_Spent`. rg is killed
        whenever the generator ends: exhausted, closed, spent or timed out."""
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
            proc = subprocess.Popen(
                argv,
                cwd=top,
                env={"PATH": RG_PATH, "LC_ALL": "C.UTF-8"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise ToolError("search is unavailable on this host (no rg)") from None
        except OSError as exc:
            raise ToolError(f"search failed: {type(exc).__name__}") from None
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(RG_TIMEOUT_S, expire)
        timer.start()
        try:
            for raw in _lines(proc, budget):
                hit = _match(raw)
                if hit is not None:
                    yield hit
            proc.wait()
        finally:
            timer.cancel()
            proc.kill()  # a no-op once rg has exited
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
        if timed_out.is_set():
            raise ToolError("search failed: timed out")
        # 0 found, 1 found nothing; 2 an error, though rg may still have printed matches.
        if proc.returncode not in (0, 1, 2):
            raise ToolError(f"search failed: rg exited {proc.returncode}")


def _lines(proc: subprocess.Popen[bytes], budget: _Budget) -> Iterator[bytes]:
    """rg's stdout, a line at a time, each at most RG_LINE_MAX bytes: a longer line is read
    to its end and dropped. Raises _Spent once `budget` is used up."""
    assert proc.stdout is not None
    long_line = False
    while chunk := proc.stdout.readline(RG_LINE_MAX):
        budget.spend(len(chunk))
        whole = chunk.endswith(b"\n")
        if whole and not long_line:
            yield chunk
        long_line = not whole


def _match(raw: bytes) -> tuple[str, int, str] | None:
    """One `rg --json` line's `(path, line, text)`, if it is a match."""
    try:
        event = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(event, dict) or event.get("type") != "match":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    path = (data.get("path") or {}).get("text")
    text = (data.get("lines") or {}).get("text")
    number = data.get("line_number")
    if not isinstance(path, str) or not isinstance(text, str):
        return None
    line = number if isinstance(number, int) and not isinstance(number, bool) else 0
    return path.removeprefix("./"), line, text.rstrip("\n")


def _encoded(obj: Any) -> int:
    """`obj`'s size as the broker sends it: `json.dumps`, so non-ASCII text is \\u-escaped."""
    return len(json.dumps(obj))


def _walk(top: Path) -> Iterator[str]:
    """Every regular file under `top`, relative and sorted, never following a symlink."""
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames.sort()
        base = Path(dirpath).relative_to(top)
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_file() and not full.is_symlink():
                yield str(base / name) if str(base) != "." else name
