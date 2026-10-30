"""#140: crewkiosk-corpus builds a filtered, scrubbed, read-only corpus per research server."""

from __future__ import annotations

import http.client
import json
import logging
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from crewkiosk import corpus
from crewkiosk.corpus import Builder, LookupFailed, ManifestError, load_manifest, run

from tests.fakes.forge import SERVER, TOKEN, Clock, Forge, manifest, write_manifest

MB = 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64

MakeBuilder = Callable[..., Builder]


def current(builder: Builder) -> Path:
    return builder.root / SERVER / "current"


def published(builder: Builder, repo: str) -> set[str]:
    """Every path under that repo's published copy, files and dirs, relative to it."""
    top = current(builder) / repo
    return {str(p.relative_to(top)) for p in top.rglob("*")}


def states(status: dict[str, Any]) -> dict[str, str]:
    return {repo: r["state"] for repo, r in status["repos"].items()}


def scans(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "gitleaks.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# -- the manifest ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        {"repos": ["forge.test/org"]},  # not a full repo
        {"repos": ["forge.test/org/../x"]},
        {"repos": "forge.test/org/a"},
        {"exclude": ["forge.test/org/A.git"]},  # not canonical
        {"refresh_minutes": 0},
        {"refresh_minutes": True},
        {"server": "other", "filename": SERVER},  # names another server than its file
        {"server": "status", "filename": "status"},  # reserved: the world-traversable dir
        {"server": "..", "filename": ".."},
        {"extra": 1},
    ],
)
def test_manifest_validation(tmp_path: Path, body: dict[str, Any]) -> None:
    path = write_manifest(tmp_path / "pool", **body)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_manifest_missing_key_and_bad_json(tmp_path: Path) -> None:
    path = write_manifest(tmp_path / "pool")
    data = json.loads(path.read_text())
    del data["exclude"]
    path.write_text(json.dumps(data))
    with pytest.raises(ManifestError):
        load_manifest(path)
    path.write_text("{")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_a_malformed_manifest_skips_only_that_server(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder, caplog: pytest.LogCaptureFixture
) -> None:
    repo = forge.repo("a", {"README.md": b"hello\n"})
    pool = tmp_path / "pool"
    write_manifest(pool, repos=[repo])
    write_manifest(pool, server="bad", repos=["not/a"])
    builder = make_builder()
    with caplog.at_level(logging.ERROR):
        assert run(pool, builder) == 1
    assert "bad.json" in caplog.text
    assert published(builder, repo) == {"README.md"}
    assert not (builder.root / "bad").exists()


# -- the forge lookup ------------------------------------------------------------------------
@pytest.mark.parametrize("suffix", ["", ".wiki"])
def test_redirect_to_excluded_repo_is_not_cloned(
    forge: Forge, make_builder: MakeBuilder, caplog: pytest.LogCaptureFixture, suffix: str
) -> None:
    repo = forge.repo("a", {"README.md": b"hello\n"})
    builder = make_builder()
    builder.build(manifest(repo))
    first_gen = current(builder).resolve()
    # The repo was renamed, within its org, into an excluded one; the old path still redirects.
    builder.lookup = lambda r, t: (
        r if r == "forge.test/org/secret-repo" else f"forge.test/Org/Secret-Repo{suffix}"
    )
    forge.cloned.clear()
    with caplog.at_level(logging.WARNING):
        status = builder.build(manifest(repo, exclude=("forge.test/org/secret-repo",)))
    assert states(status) == {repo: "excluded"}
    assert forge.cloned == []
    assert not (current(builder) / repo).exists()
    assert not (first_gen / repo).exists()  # the previous generation drops it too
    assert "which is excluded" in caplog.text and "forge.test/Org/Secret-Repo" in caplog.text


def test_a_transfer_to_another_org_is_excluded(
    forge: Forge, make_builder: MakeBuilder, caplog: pytest.LogCaptureFixture
) -> None:
    """The daemon admitted the entry by its host/org. A repo transferred into an org no
    allowlist vouched for (a partner's, say) is never cloned, and its last good copy goes."""
    repo = forge.repo("a", {"README.md": b"ours\n"})
    builder = make_builder()
    builder.build(manifest(repo))
    first_gen = current(builder).resolve()
    builder.lookup = lambda r, t: "forge.test/other-org/a"
    forge.cloned.clear()
    with caplog.at_level(logging.WARNING):
        status = builder.build(manifest(repo))
    assert states(status) == {repo: "excluded"}
    assert forge.cloned == []
    assert not (current(builder) / repo).exists() and not (first_gen / repo).exists()
    assert "in another org" in caplog.text
    # A rename within the org still builds.
    renamed = forge.repo("b", {"README.md": b"renamed\n"})
    builder.lookup = lambda r, t: renamed
    assert states(builder.build(manifest(repo))) == {repo: "ok"}


def test_failed_lookup_is_clone_failed_with_no_clone(
    forge: Forge, make_builder: MakeBuilder
) -> None:
    repo = forge.repo("a", {"README.md": b"hello\n"})

    def fail(r: str, t: str) -> str:
        raise LookupFailed(f"{r}: the forge answered 404")

    status = make_builder(lookup=fail).build(manifest(repo))
    assert states(status) == {repo: "clone_failed"}
    assert forge.cloned == []


def test_the_canonical_path_is_cloned(forge: Forge, make_builder: MakeBuilder) -> None:
    """A redirect to a repo not excluded: the canonical path is cloned, published under the
    manifest's."""
    new = forge.repo("renamed", {"README.md": b"new home\n"})
    old = "forge.test/org/old-name"
    builder = make_builder(lookup=lambda r, t: new)
    assert states(builder.build(manifest(old))) == {old: "ok"}
    assert forge.cloned == [new]
    assert (current(builder) / old / "README.md").read_text() == "new home\n"


def test_forge_canonical_asks_each_forge_for_its_name(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, dict[str, str]]] = []

    class Reply:
        def __init__(self, body: dict[str, Any]) -> None:
            self.body = body

        def __enter__(self) -> Reply:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(self.body).encode()

    class Opener:
        def open(self, request: Any, timeout: float) -> Reply:
            asked.append((request.full_url, dict(request.header_items())))
            if "api.github.com" in request.full_url:
                return Reply({"full_name": "NewOwner/Repo"})
            return Reply({"path_with_namespace": "grp/sub/moved"})

    handlers: list[Any] = []

    def build_opener(*given: Any) -> Opener:
        handlers.extend(given)
        return Opener()

    # Never the network: the opener is the only way out.
    monkeypatch.setattr(corpus.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(corpus.urllib.request, "urlopen", None)
    assert corpus.forge_canonical("github.com/old/repo", TOKEN) == "github.com/NewOwner/Repo"
    assert corpus.forge_canonical("gl.test/grp/sub/x", TOKEN) == "gl.test/grp/sub/moved"
    assert asked[0][0] == "https://api.github.com/repos/old/repo"
    assert asked[1][0] == "https://gl.test/api/v4/projects/grp%2Fsub%2Fx"
    assert TOKEN in asked[0][1]["Authorization"] and asked[1][1]["Private-token"] == TOKEN
    assert all(TOKEN not in url for url, _ in asked)
    assert all(isinstance(h, corpus.SameHostRedirects) for h in handlers) and handlers
    # No GitHub repo has a deeper path: a 404, never a reason to block github.com.
    with pytest.raises(corpus.NotFound):
        corpus.forge_canonical("github.com/a/b/c", TOKEN)


@pytest.mark.parametrize(
    "error",
    [
        http.client.IncompleteRead(b"{"),
        http.client.BadStatusLine("x"),
        http.client.LineTooLong("header line"),
    ],
)
def test_forge_canonical_maps_http_client_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """urllib lets http.client's own errors out unwrapped: each is a failed lookup."""

    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            raise error

    monkeypatch.setattr(corpus.urllib.request, "build_opener", lambda *h: Opener())
    with pytest.raises(LookupFailed, match=type(error).__name__):
        corpus.forge_canonical("gl.test/grp/x", TOKEN)


# -- what reaches the tree -------------------------------------------------------------------
def test_size_and_text_filters(forge: Forge, make_builder: MakeBuilder) -> None:
    repo = forge.repo(
        "a",
        {
            "big.txt": b"a" * (6 * MB),
            "edge.txt": b"b" * (5 * MB),
            "logo.png": PNG,
            "nul.txt": b"text\x00more\n",
            "latin1.txt": b"caf\xe9\n",
            "ok.md": "café ✓\n".encode(),
        },
    )
    builder = make_builder()
    status = builder.build(manifest(repo))
    assert published(builder, repo) == {"edge.txt", "ok.md"}
    assert status["repos"][repo]["files"] == 2
    assert status["repos"][repo]["bytes"] == 5 * MB + len("café ✓\n".encode())


def test_the_blob_filter_runs_and_keeps_exactly_5_mib(
    forge: Forge, make_builder: MakeBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forge honors the filter (as a real one does): a blob over 5 MiB is never fetched,
    so extract sees it as missing and never sizes it; one of exactly 5 MiB is fetched."""
    repo = forge.repo("a", {"big.txt": b"a" * (5 * MB + 1), "edge.txt": b"b" * (5 * MB)})

    def oid(data: bytes) -> str:
        done = subprocess.run(["git", "hash-object", "--stdin"], input=data, capture_output=True)
        return done.stdout.decode().strip()

    sized: list[str] = []
    real = corpus._sizes
    monkeypatch.setattr(corpus, "_sizes", lambda g, c, oids: sized.extend(oids) or real(g, c, oids))
    builder = make_builder()
    builder.build(manifest(repo))
    assert published(builder, repo) == {"edge.txt"}
    assert oid(b"b" * (5 * MB)) in sized and oid(b"a" * (5 * MB + 1)) not in sized


def test_a_non_utf8_file_name_is_left_out(forge: Forge, make_builder: MakeBuilder) -> None:
    """Not a reason to fail the repo, let alone the server's build: that file alone is out."""
    repo = forge.repo("a", {os.fsdecode(b"caf\xe9.txt"): b"x\n", "ok.md": b"ok\n"})
    builder = make_builder()
    assert states(builder.build(manifest(repo))) == {repo: "ok"}
    assert published(builder, repo) == {"ok.md"}


def test_denylist(forge: Forge, make_builder: MakeBuilder) -> None:
    denied = [
        "x.enc",
        ".sops.yaml",
        "tls.pem",
        "id.KEY",
        "c.p12",
        "c.pfx",
        ".env",
        ".env.local",
        "aws_credentials",
        "my-secret.txt",
        "prod.tfvars",
        ".gitleaks.toml",
        ".gitleaksignore",
    ]
    files = {name: b"x\n" for name in denied}
    files |= {f"nested/deep/{name}": b"x\n" for name in denied}
    files |= {"secrets/readme.md": b"x\n", "keep.md": b"x\n", "nested/keep.py": b"x\n"}
    repo = forge.repo("a", files)
    builder = make_builder()
    builder.build(manifest(repo))
    assert published(builder, repo) == {"keep.md", "nested", "nested/keep.py"}


def test_no_git_no_symlinks(forge: Forge, make_builder: MakeBuilder) -> None:
    repo = forge.repo(
        "a",
        {"README.md": b"hi\n", "docs/a.md": b"a\n"},
        symlinks={"link.md": "README.md", "escape": "/etc/passwd", "docs/up": ".."},
    )
    builder = make_builder()
    builder.build(manifest(repo))
    assert published(builder, repo) == {"README.md", "docs", "docs/a.md"}
    assert not any(p.is_symlink() for p in (current(builder) / repo).rglob("*"))


# -- the scan --------------------------------------------------------------------------------
def test_gitleaks_withholds_repo(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder, caplog: pytest.LogCaptureFixture
) -> None:
    leaky = forge.repo("leaky", {"README.md": b"v1\n"})
    clean = forge.repo("clean", {"README.md": b"clean\n"})
    builder = make_builder()
    builder.build(manifest(leaky, clean))
    first = forge.head(leaky)
    value = "PLANTED-value-0123456789"
    forge.repo("leaky", {"README.md": b"v2\n", "conf/app.py": f"tok = '{value}'\n".encode()})
    forge.repo("clean", {"README.md": b"clean v2\n"})
    with caplog.at_level(logging.INFO):
        status = builder.build(manifest(leaky, clean))
    assert states(status) == {leaky: "withheld", clean: "ok"}
    # The last good copy is served, and the status keeps its commit.
    assert (current(builder) / leaky / "README.md").read_text() == "v1\n"
    assert status["repos"][leaky]["commit"] == first
    assert (current(builder) / clean / "README.md").read_text() == "clean v2\n"
    assert "fake-rule" in caplog.text and "conf/app.py" in caplog.text
    assert value not in caplog.text
    assert value not in (builder.root / "status" / f"{SERVER}.json").read_text()


def test_gitleaks_withholds_a_new_repo_with_nothing_to_serve(
    forge: Forge, make_builder: MakeBuilder
) -> None:
    repo = forge.repo("leaky", {"a.py": b"PLANTED\n"})
    builder = make_builder()
    status = builder.build(manifest(repo))
    assert states(status) == {repo: "withheld"}
    assert status["repos"][repo]["commit"] is None
    assert not (current(builder) / repo).exists()


def test_a_gitleaks_error_withholds(
    forge: Forge, make_builder: MakeBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = forge.repo("a", {"README.md": b"hi\n"})
    monkeypatch.setenv("FAKE_GITLEAKS_EXIT", "1")
    assert states(make_builder().build(manifest(repo))) == {repo: "withheld"}


def test_a_repo_cannot_exempt_itself(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gitleaks:allow` is neutralized in what gitleaks scans (the published file is
    untouched), it runs from an empty directory with no GITLEAKS_* variables, and the repo's
    own `.gitleaks.toml` never reaches it."""
    monkeypatch.setenv("GITLEAKS_CONFIG", "/elsewhere/allow-all.toml")
    repo = forge.repo(
        "a",
        {
            "a.py": b"tok = 'x'  # gitleaks:allow\n",
            "b.py": b"plain\n",
            ".gitleaks.toml": b"[allowlist]\npaths = ['.*']\n",
        },
    )
    builder = make_builder()
    assert states(builder.build(manifest(repo))) == {repo: "ok"}
    (seen,) = scans(tmp_path)
    # Flat names: no path the default config allowlists (node_modules/, *.svg, ...) survives.
    assert seen["files"] == {"f000001": "tok = 'x'  # gitleaks-allow\n", "f000002": "plain\n"}
    # gitleaks runs inside the flat copy and scans `.`: every path it sees is an f<n> name,
    # never one under the server's directory (a server named node_modules is allowlisted).
    assert seen["env"] == [] and seen["args"][-1] == "."
    assert Path(seen["cwd"]).name == "scan" and sorted(seen["cwd_entries"]) == sorted(
        seen["files"]
    )
    assert (current(builder) / repo / "a.py").read_text() == "tok = 'x'  # gitleaks:allow\n"


def test_shadow_names_keep_rule_suffixes(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder
) -> None:
    """The suffixes path-scoped rules need survive the flat names; nothing else does, so
    `pnpm-lock.yaml` keeps `.yaml` but not the lock-file name the allowlist matches."""
    repo = forge.repo(
        "a",
        {
            "k8s/Db.YML": b"a\n",
            "main.tf": b"b\n",
            "pnpm-lock.yaml": b"c\n",
            "src/NuGet.Config": b"d\n",
            "web/index.php": b"e\n",
            "logo.svg": b"f\n",
        },
    )
    assert states(make_builder().build(manifest(repo))) == {repo: "ok"}
    (seen,) = scans(tmp_path)
    assert set(seen["files"]) == {
        "f000001.YML",
        "f000002",
        "f000003.tf",
        "f000004.yaml",
        "f000005.NuGet.Config",
        "f000006.php",
    }


def test_gitleaks_missing_is_fatal(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder, caplog: pytest.LogCaptureFixture
) -> None:
    repo = forge.repo("a", {"README.md": b"hi\n"})
    pool = tmp_path / "pool"
    write_manifest(pool, repos=[repo])
    builder = make_builder(gitleaks=str(tmp_path / "no-such-gitleaks"))
    with caplog.at_level(logging.ERROR):
        assert run(pool, builder) == 2
    assert "nothing is published unscanned" in caplog.text
    assert forge.cloned == [] and list(builder.root.iterdir()) == []


def test_a_missing_root_is_fatal(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder, caplog: pytest.LogCaptureFixture
) -> None:
    pool = tmp_path / "pool"
    write_manifest(pool, repos=[forge.repo("a", {"README.md": b"hi\n"})])
    builder = make_builder()
    builder.root.rmdir()
    with caplog.at_level(logging.ERROR):
        assert run(pool, builder) == 2
    assert "crewcorpus.tmpfiles.conf" in caplog.text and forge.cloned == []


# -- publishing ------------------------------------------------------------------------------
def test_atomic_swap(forge: Forge, make_builder: MakeBuilder, clock: Clock) -> None:
    repo = forge.repo("a", {"README.md": b"v1\n", "src/m.py": b"x = 1\n"})
    builder = make_builder()
    gens = []
    for n in range(1, 4):
        forge.repo("a", {"README.md": f"v{n}\n".encode(), "src/m.py": b"x = 1\n"})
        builder.build(manifest(repo))
        link = current(builder)
        assert link.is_symlink() and not os.path.isabs(os.readlink(link))
        gens.append(link.resolve())
        assert (link / repo / "README.md").read_text() == f"v{n}\n"
        clock.tick()
    # The current generation and the one before it remain, nothing older, and no work dirs.
    left = sorted(p.name for p in (builder.root / SERVER).iterdir())
    assert left == sorted([gens[1].name, gens[2].name, "current"])
    for p in [gens[2], *gens[2].rglob("*")]:
        mode = stat.S_IMODE(p.lstat().st_mode)
        assert mode == (0o555 if p.is_dir() else 0o444), p


def test_clone_failure_keeps_last_good(
    forge: Forge, make_builder: MakeBuilder, clock: Clock
) -> None:
    repo = forge.repo("a", {"README.md": b"v1\n"})
    builder = make_builder()
    first = builder.build(manifest(repo))["repos"][repo]
    clock.tick()
    builder.clone_url = lambda r: "file:///nonexistent/repo.git"
    status = builder.build(manifest(repo))
    assert states(status) == {repo: "clone_failed"}
    assert (current(builder) / repo / "README.md").read_text() == "v1\n"
    for key in ("commit", "files", "bytes", "last_ok"):
        assert status["repos"][repo][key] == first[key]
    assert first["last_ok"] == "2026-10-30T12:00:00+00:00"


def test_status_file(forge: Forge, make_builder: MakeBuilder) -> None:
    repo = forge.repo("a", {"README.md": b"hi\n"})
    builder = make_builder()
    builder.build(manifest(repo))
    path = builder.root / "status" / f"{SERVER}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert json.loads(path.read_text()) == {
        "server": SERVER,
        "built_at": "2026-10-30T12:00:00+00:00",
        "manifest_at": "2026-10-30T12:00:00+00:00",  # the manifest's generated_at, for due()
        "repos": {
            repo: {
                "state": "ok",
                "commit": forge.head(repo),
                "files": 1,
                "bytes": 3,
                "last_ok": "2026-10-30T12:00:00+00:00",
            }
        },
    }


def test_a_server_is_rebuilt_when_due_or_its_repos_change(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder, clock: Clock
) -> None:
    a = forge.repo("a", {"README.md": b"a\n"})
    b = forge.repo("b", {"README.md": b"b\n"})
    pool = tmp_path / "pool"
    write_manifest(pool, repos=[a], refresh_minutes=30)
    builder = make_builder()
    assert run(pool, builder) == 0
    assert forge.cloned == [a]
    clock.tick(10)
    assert run(pool, builder) == 0
    assert forge.cloned == [a]  # not due
    write_manifest(pool, repos=[a, b], refresh_minutes=30)
    assert run(pool, builder) == 0
    assert forge.cloned == [a, a, b]  # its repos changed
    clock.tick(31)
    run(pool, builder)
    assert forge.cloned == [a, a, b, a, b]


# -- the credential --------------------------------------------------------------------------
def test_credential_never_in_argv(
    forge: Forge,
    make_builder: MakeBuilder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = forge.repo("a", {"README.md": b"hi\n"})
    calls: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.run

    def spy(argv: list[str], **kw: Any) -> Any:
        calls.append(([str(a) for a in argv], dict(kw.get("env") or os.environ)))
        return real(argv, **kw)

    monkeypatch.setattr(corpus.subprocess, "run", spy)
    seen_tokens: list[str] = []
    builder = make_builder(lookup=lambda r, t: seen_tokens.append(t) or r)
    with caplog.at_level(logging.DEBUG):
        status = builder.build(manifest(repo))
    assert seen_tokens == [TOKEN]  # the lookup got it, as a header value
    clone = next(argv for argv, _ in calls if "clone" in argv)
    assert clone[-2].startswith("file://") and TOKEN not in clone[-2]
    assert "http.followRedirects=false" in clone
    for argv, env in calls:
        assert all(TOKEN not in a for a in argv)
        assert all(TOKEN not in v for v in env.values())
    git_env = next(env for argv, env in calls if "clone" in argv)
    assert git_env["GIT_TERMINAL_PROMPT"] == "0" and git_env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert git_env["CREWKIOSK_CREDENTIAL"].endswith("corpus-dev.credential")
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(status)


def test_the_askpass_helper_answers_from_the_file(tmp_path: Path, credential: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    git = corpus.Git(home, credential, "oauth2")
    helper = git.env["GIT_ASKPASS"]
    env = git.env
    user = subprocess.run([helper, "Username for 'https://x': "], env=env, capture_output=True)
    password = subprocess.run([helper, "Password for 'https://x': "], env=env, capture_output=True)
    assert (user.stdout, password.stdout) == (b"oauth2\n", f"{TOKEN}\n".encode())
    assert TOKEN not in (home / "askpass").read_text()


def test_each_forge_gets_only_its_own_token(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder
) -> None:
    """The credential is per (server, forge host). A host with none is never asked, and a
    lookup that lands on another host is refused."""
    repo = forge.repo("a", {"README.md": b"hi\n"})
    other = "elsewhere.test/org/b"
    asked: list[tuple[str, str]] = []
    keys: list[tuple[str, str]] = []
    builder = make_builder(lookup=lambda r, t: asked.append((r, t)) or r)
    real = builder.credential
    builder.credential = lambda s, h: (
        keys.append((s, h)) or (real(s, h) if h == "forge.test" else tmp_path / "none")
    )
    status = builder.build(manifest(repo, other))
    assert states(status) == {repo: "ok", other: "clone_failed"}
    assert keys == [(SERVER, "forge.test"), (SERVER, "elsewhere.test")]
    assert asked == [(repo, TOKEN)]  # elsewhere.test was never sent anything
    assert corpus.CREDENTIAL.format(server="dev", host="gitlab.example") == (
        "/etc/crewkiosk/corpus-dev@gitlab.example.credential"
    )
    # Server `oaw` on host `dev-github.com` is not server `oaw-dev` on host `github.com`.
    assert corpus.CREDENTIAL.format(server="oaw", host="dev-github.com") != (
        corpus.CREDENTIAL.format(server="oaw-dev", host="github.com")
    )

    builder.lookup = lambda r, t: "elsewhere.test/org/a"
    assert states(builder.build(manifest(repo))) == {repo: "clone_failed"}


def test_lookup_redirects_stay_on_the_forge() -> None:
    handler = corpus.SameHostRedirects()
    req = corpus.urllib.request.Request(
        "https://gl.test/api/v4/projects/a%2Fb", headers={"PRIVATE-TOKEN": TOKEN}
    )
    same = handler.redirect_request(req, None, 301, "moved", {}, "https://gl.test/api/v4/x")
    assert same is not None and same.full_url == "https://gl.test/api/v4/x"
    for off in ("https://evil.test/api/v4/x", "http://gl.test/api/v4/x"):
        with pytest.raises(corpus.urllib.error.HTTPError):
            handler.redirect_request(req, None, 301, "moved", {}, off)


def test_a_filesystem_error_fails_only_that_repo(
    forge: Forge, make_builder: MakeBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk or a too-long name while writing one repo's tree: that repo is
    `clone_failed` and served from its last good copy; the rest of the build goes on."""
    bad = forge.repo("bad", {"README.md": b"bad\n"})
    good = forge.repo("good", {"README.md": b"ok\n"})
    builder = make_builder()
    builder.build(manifest(bad, good))  # a clean first build: bad has a last good copy
    real_fresh = builder._fresh

    def fresh(m: Any, exclude: Any, repo: str, work: Path, gen: Path) -> Any:
        if repo == bad:
            (gen / repo).mkdir(parents=True)  # a partial copy, which must not be published
            (gen / repo / "half.md").write_text("half\n")
            raise OSError(28, "No space left on device")
        return real_fresh(m, exclude, repo, work, gen)

    monkeypatch.setattr(builder, "_fresh", fresh)
    status = builder.build(manifest(bad, good))
    assert states(status) == {bad: "clone_failed", good: "ok"}
    assert published(builder, bad) == {"README.md"}  # the last good copy, not the partial
    assert published(builder, good) == {"README.md"}


def test_nested_repos_are_refused(forge: Forge, make_builder: MakeBuilder) -> None:
    outer = forge.repo("a", {"README.md": b"outer\n"})
    inner = f"{outer}/b"
    status = make_builder().build(manifest(outer, inner))
    assert states(status) == {outer: "ok", inner: "clone_failed"}
    assert inner not in forge.cloned


def test_a_hung_size_lookup_fails_only_that_repo(
    forge: Forge, make_builder: MakeBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = forge.repo("slow", {"README.md": b"slow\n"})
    good = forge.repo("good", {"README.md": b"ok\n"})
    real = subprocess.run
    oid = real(["git", "hash-object", "--stdin"], input=b"slow\n", capture_output=True).stdout

    def run(argv: list[str], **kw: Any) -> Any:
        if "--batch-check" in argv and oid.strip() in kw.get("input", b""):
            raise subprocess.TimeoutExpired(argv, corpus.GIT_TIMEOUT_S)
        return real(argv, **kw)

    monkeypatch.setattr(corpus.subprocess, "run", run)
    assert states(make_builder().build(manifest(slow, good))) == {slow: "clone_failed", good: "ok"}


def test_a_failed_build_leaves_no_half_generation(
    forge: Forge, make_builder: MakeBuilder, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    repo = forge.repo("a", {"README.md": b"v1\n"})
    builder = make_builder()
    builder.build(manifest(repo))
    live = current(builder).resolve()
    clock.tick()

    def boom(root: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(corpus, "set_modes", boom)
    with pytest.raises(OSError):
        builder.build(manifest(repo))
    assert current(builder).resolve() == live
    assert sorted(p.name for p in (builder.root / SERVER).iterdir()) == sorted(
        [live.name, "current"]
    )


def test_one_server_failing_never_stops_the_others(
    tmp_path: Path,
    forge: Forge,
    make_builder: MakeBuilder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = forge.repo("a", {"README.md": b"hi\n"})
    pool = tmp_path / "pool"
    write_manifest(pool, server="aa", repos=[repo])
    write_manifest(pool, server="bb", repos=[repo])
    builder = make_builder()
    real = builder.build

    def build(m: Any) -> Any:
        if m.server == "aa":
            raise OSError(28, "No space left on device")
        return real(m)

    monkeypatch.setattr(builder, "build", build)
    with caplog.at_level(logging.ERROR):
        assert run(pool, builder) == 1
    assert "corpus aa: the build failed" in caplog.text
    assert (builder.root / "bb" / "current" / repo / "README.md").exists()


def test_a_missing_credential_fails_every_repo(
    forge: Forge, make_builder: MakeBuilder, credential: Path
) -> None:
    repo = forge.repo("a", {"README.md": b"hi\n"})
    credential.unlink()
    assert states(make_builder().build(manifest(repo))) == {repo: "clone_failed"}
    assert forge.cloned == []


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_a_credential_others_can_read_is_refused(
    forge: Forge,
    make_builder: MakeBuilder,
    credential: Path,
    caplog: pytest.LogCaptureFixture,
    mode: int,
) -> None:
    repo = forge.repo("a", {"README.md": b"hi\n"})
    credential.chmod(mode)
    with caplog.at_level(logging.ERROR):
        assert states(make_builder().build(manifest(repo))) == {repo: "clone_failed"}
    assert forge.cloned == []
    with pytest.raises(ValueError, match=f"mode {mode:04o}: make it 0600 or 0400"):
        corpus.read_token(credential)
    credential.chmod(0o400)
    assert corpus.read_token(credential) == TOKEN


def test_any_error_in_one_repo_fails_only_that_repo(
    forge: Forge, make_builder: MakeBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = forge.repo("bad", {"README.md": b"bad\n"})
    good = forge.repo("good", {"README.md": b"ok\n"})
    builder = make_builder()
    real = builder._fresh

    def fresh(m: Any, exclude: Any, repo: str, work: Path, gen: Path) -> Any:
        if repo == bad:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return real(m, exclude, repo, work, gen)

    monkeypatch.setattr(builder, "_fresh", fresh)
    assert states(builder.build(manifest(bad, good))) == {bad: "clone_failed", good: "ok"}


def test_a_repo_out_of_the_manifest_leaves_the_previous_generation_too(
    forge: Forge, make_builder: MakeBuilder
) -> None:
    a = forge.repo("a", {"README.md": b"a\n"})
    b = forge.repo("b", {"README.md": b"b\n"})
    builder = make_builder()
    builder.build(manifest(a, b))
    first_gen = current(builder).resolve()
    builder.build(manifest(a))  # the operator excluded b: the daemon dropped it
    assert not (current(builder) / b).exists()
    assert not (first_gen / b).exists() and (first_gen / a / "README.md").exists()


def test_a_server_with_no_manifest_has_its_corpus_removed(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder, caplog: pytest.LogCaptureFixture
) -> None:
    repo = forge.repo("a", {"README.md": b"hi\n"})
    pool = tmp_path / "pool"
    write_manifest(pool, server="on", repos=[repo])
    off = write_manifest(pool, server="off", repos=[repo])
    bad = write_manifest(pool, server="bad", repos=[repo])
    builder = make_builder()
    assert run(pool, builder) == 0
    bad.write_text("{not json")  # malformed, but research is still on: its corpus stays
    off.unlink()  # research turned off on "off"
    with caplog.at_level(logging.WARNING):
        assert run(pool, builder) == 1
    root = builder.root
    assert not (root / "off").exists() and not (root / "status" / "off.json").exists()
    assert (root / "on" / "current" / repo).is_dir() and (root / "bad" / "current").exists()
    assert (root / "status" / "on.json").exists() and (root / "status").is_dir()
    assert "corpus off: no manifest" in caplog.text


def test_an_unlistable_pool_removes_nothing(
    tmp_path: Path, forge: Forge, make_builder: MakeBuilder
) -> None:
    pool = tmp_path / "pool"
    write_manifest(pool, repos=[forge.repo("a", {"README.md": b"hi\n"})])
    builder = make_builder()
    assert run(pool, builder) == 0
    assert run(tmp_path / "no-pool", builder) == 2
    assert (current(builder)).exists()


def test_a_new_generated_at_is_due(forge: Forge, make_builder: MakeBuilder) -> None:
    """The daemon rewrites a manifest only when it changes, so an exclude-only change shows
    as a new generated_at: the build is due, though the repos and the interval are not."""
    repo = forge.repo("a", {"README.md": b"hi\n"})
    builder = make_builder()
    m = manifest(repo)
    builder.build(m)
    assert not builder.due(m)
    changed = corpus.Manifest(
        m.server, "2026-10-30T12:05:00+00:00", m.refresh_minutes, m.repos, frozenset({"x/y/z"})
    )
    assert builder.due(changed)


def test_an_exclude_covers_its_repo_under_a_new_name(
    forge: Forge, make_builder: MakeBuilder
) -> None:
    """An exclude written under a repo's old name is resolved too: the repo, pooled under its
    new name, stays out."""
    new = forge.repo("new", {"README.md": b"hidden\n"})
    builder = make_builder(lookup=lambda r, t: new if r == "forge.test/org/old" else r)
    status = builder.build(manifest(new, exclude=("forge.test/org/old",)))
    assert states(status) == {new: "excluded"}


def test_an_unresolvable_exclude_serves_nothing_from_its_forge(
    forge: Forge, make_builder: MakeBuilder, caplog: pytest.LogCaptureFixture
) -> None:
    """Any repo on that forge might be the excluded one renamed: none is served, not even a
    last good copy. A 404 (no such repo for this token) excludes nothing more."""
    repo = forge.repo("a", {"README.md": b"hi\n"})
    builder = make_builder()
    builder.build(manifest(repo))
    first_gen = current(builder).resolve()

    def down(r: str, t: str) -> str:
        if r == "forge.test/org/gone":
            raise LookupFailed(f"{r}: the forge answered 503")
        return r

    builder.lookup = down
    with caplog.at_level(logging.ERROR):
        status = builder.build(manifest(repo, exclude=("forge.test/org/gone",)))
    assert states(status) == {repo: "clone_failed"}
    assert not (current(builder) / repo).exists() and not (first_gen / repo).exists()
    assert "nothing from forge.test is served" in caplog.text

    def missing(r: str, t: str) -> str:
        if r == "forge.test/org/gone":
            raise corpus.NotFound(f"{r}: the forge answered 404")
        return r

    builder.lookup = missing
    assert states(builder.build(manifest(repo, exclude=("forge.test/org/gone",)))) == {repo: "ok"}

    def broken(r: str, t: str) -> str:
        if r == "forge.test/org/gone":
            raise RuntimeError("a lookup error nothing maps yet")
        return r

    # Any other error blocks the forge too: it never escapes build() to keep the old generation.
    builder.lookup = broken
    status = builder.build(manifest(repo, exclude=("forge.test/org/gone",)))
    assert states(status) == {repo: "clone_failed"}
    assert not (current(builder) / repo).exists()


@pytest.mark.parametrize("why", ["gitleaks", "denylist"])
def test_a_last_good_copy_must_pass_todays_checks(
    forge: Forge,
    make_builder: MakeBuilder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    why: str,
) -> None:
    """A rule or a denylist entry added since the copy was built applies to it: a repo that
    now fails to clone serves nothing rather than a copy today's checks would refuse."""
    repo = forge.repo("a", {"notes.md": b"hi\n"})
    builder = make_builder()
    builder.build(manifest(repo))
    builder.lookup = lambda r, t: (_ for _ in ()).throw(LookupFailed("down"))
    if why == "gitleaks":
        monkeypatch.setenv("FAKE_GITLEAKS_EXIT", "1")
    else:
        monkeypatch.setattr(corpus, "DENYLIST", (*corpus.DENYLIST, "notes.*"))
    with caplog.at_level(logging.ERROR):
        status = builder.build(manifest(repo))
    assert states(status) == {repo: "clone_failed"}
    assert not (current(builder) / repo).exists()
    assert "fails today's checks" in caplog.text


def test_a_gitleaks_timeout_or_unreadable_report_withholds(
    forge: Forge,
    make_builder: MakeBuilder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = forge.repo("a", {"README.md": b"hi\n"})
    # Exit 3, a finding, with no report written.
    monkeypatch.setenv("FAKE_GITLEAKS_EXIT", "3")
    with caplog.at_level(logging.ERROR):
        assert states(make_builder().build(manifest(repo))) == {repo: "withheld"}
    assert "its report is unreadable" in caplog.text
    monkeypatch.delenv("FAKE_GITLEAKS_EXIT")
    real = subprocess.run

    def hang(argv: list[str], **kw: Any) -> Any:
        if Path(argv[0]).name == "gitleaks":
            raise subprocess.TimeoutExpired(argv, corpus.GIT_TIMEOUT_S)
        return real(argv, **kw)

    monkeypatch.setattr(corpus.subprocess, "run", hang)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert states(make_builder().build(manifest(repo))) == {repo: "withheld"}
    assert "gitleaks failed: TimeoutExpired" in caplog.text


# -- the unit --------------------------------------------------------------------------------
PACKAGING = Path(__file__).resolve().parents[3] / "packaging"


def unit(name: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    here: dict[str, str] = {}
    for line in (PACKAGING / name).read_text().splitlines():
        if line.startswith("[") and line.endswith("]"):
            here = sections.setdefault(line[1:-1], {})
        elif "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            here[key] = value
    return sections


def test_the_unit_runs_as_crewcorpus_and_sees_no_chat_state() -> None:
    svc = unit("crewkiosk-corpus.service")["Service"]
    assert (svc["Type"], svc["User"], svc["Group"]) == ("oneshot", "crewcorpus", "crewcorpus")
    assert svc["ExecStart"] == "/opt/mattermost-crew/.venv/bin/crewkiosk-corpus"
    assert svc["NoNewPrivileges"] == "yes" and svc["ProtectSystem"] == "strict"
    assert svc["ReadWritePaths"] == "/var/lib/crewcorpus" and svc["UMask"] == "0027"
    assert svc["ProtectHome"] == "yes" and svc["PrivateTmp"] == "yes"
    hidden = {p.lstrip("-") for p in svc["InaccessiblePaths"].split()}
    assert {
        "/etc/crewchat",
        "/var/lib/crewchat",
        "/etc/crewbroker",
        "/var/lib/crewbroker",
        "/etc/crewkiosk/model.credential",
    } <= hidden
    timer = unit("crewkiosk-corpus.timer")["Timer"]
    assert (timer["OnBootSec"], timer["OnUnitActiveSec"]) == ("2min", "15min")


def test_the_tmpfiles_entries() -> None:
    lines = [
        line.split()
        for line in (PACKAGING / "crewcorpus.tmpfiles.conf").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert lines == [
        ["d", "/var/lib/crewcorpus", "2751", "crewcorpus", "crewkiosk-broker", "-"],
        ["d", "/var/lib/crewcorpus/status", "0755", "crewcorpus", "crewcorpus", "-"],
    ]
