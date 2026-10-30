"""IT-29 (#140): crewkiosk-corpus against real git and the real gitleaks.

Two local bare repos stand in for the forge, served over `file://` through the builder's
clone-URL hook. The forge lookup is the test's: it answers each repo's own path, except one
that "was renamed" into an excluded repo. Skipped without gitleaks, unless
`CREWKIOSK_GITLEAKS_REQUIRED=1` (CI sets it, `scripts/ci/gitleaks-install.sh`), which makes a
missing gitleaks a failure.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import shutil
import stat
import string
from pathlib import Path

import pytest
from crewkiosk.corpus import Builder, run

from tests.fakes.forge import SERVER, TOKEN, Clock, Forge, write_manifest

MB = 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64
HIDDEN = "forge.test/hidden/secret-repo"


@pytest.fixture
def gitleaks() -> str:
    path = shutil.which("gitleaks")
    if path is None:
        if os.environ.get("CREWKIOSK_GITLEAKS_REQUIRED") == "1":
            pytest.fail("CREWKIOSK_GITLEAKS_REQUIRED=1, and there is no gitleaks on PATH")
        pytest.skip("no gitleaks on PATH (CI installs it: scripts/ci/gitleaks-install.sh)")
    return path


def github_token() -> str:
    """A token gitleaks' `github-pat` rule flags, made here so no secret is ever committed."""
    alphabet = string.ascii_letters + string.digits
    return "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))


def test_it29_builds_filters_and_withholds(
    tmp_path: Path, gitleaks: str, caplog: pytest.LogCaptureFixture
) -> None:
    forge, clock = Forge(tmp_path / "forge"), Clock()
    app = forge.repo(
        "app",
        {
            "README.md": b"# app\n",
            "src/main.py": b"print('hi')\n",
            "big.txt": b"x" * (6 * MB),
            "logo.png": PNG,
            ".env": b"API_KEY=whatever\n",
            ".gitleaks.toml": b"[allowlist]\npaths = ['.*']\n",
        },
        symlinks={"passwd": "/etc/passwd"},
    )
    lib = forge.repo("lib", {"README.md": b"lib v1\n"})
    moved = forge.repo("moved", {"README.md": b"was public\n"})
    # gitleaks' default config never scans these paths: the builder must scan them anyway.
    vendored = github_token()
    vend = forge.repo(
        "vend", {"node_modules/pkg/index.js": f'module.exports = "{vendored}"\n'.encode()}
    )
    art = forge.repo("art", {"logo.svg": f"<svg><desc>{vendored}</desc></svg>\n".encode()})
    # kubernetes-secret-yaml fires only on a `.ya?ml` path: the flat name must keep it. (A
    # `*secret*` path never reaches the scan at all: the denylist drops it.)
    sealed = base64.b64encode(secrets.token_bytes(24)).decode()
    k8s = forge.repo(
        "k8s", {"deploy/db.yaml": f"kind: Secret\ndata:\n  password: {sealed}\n".encode()}
    )
    renamed: set[str] = set()
    credential = tmp_path / "corpus-dev.credential"
    credential.write_text(TOKEN + "\n")
    credential.chmod(0o600)
    root = tmp_path / "corpus"
    root.mkdir()
    builder = Builder(
        root,
        gitleaks=gitleaks,
        credential=lambda server, host: credential,
        lookup=lambda repo, token: HIDDEN if repo in renamed else repo,
        clone_url=forge.url,
        clock=clock,
    )
    pool = tmp_path / "pool"
    write_manifest(pool, repos=[app, lib, moved, vend, art, k8s], exclude=[HIDDEN])

    with caplog.at_level(logging.INFO):
        assert run(pool, builder) == 0
    current = root / SERVER / "current"
    first = json.loads((root / "status" / f"{SERVER}.json").read_text())
    assert {r: s["state"] for r, s in first["repos"].items()} == {
        app: "ok",
        lib: "ok",
        moved: "ok",
        vend: "withheld",
        art: "withheld",
        k8s: "withheld",
    }
    assert "kubernetes-secret-yaml" in caplog.text and "deploy/db.yaml" in caplog.text
    assert sealed not in caplog.text
    caplog.clear()
    assert not (current / vend).exists() and not (current / art).exists()
    assert not (current / k8s).exists()
    top = current / app
    assert {str(p.relative_to(top)) for p in top.rglob("*")} == {"README.md", "src", "src/main.py"}
    for p in [current.resolve(), *current.resolve().rglob("*")]:
        assert stat.S_IMODE(p.lstat().st_mode) == (0o555 if p.is_dir() else 0o444), p

    # lib gains a real-looking secret, hidden behind `gitleaks:allow`; moved is renamed into an
    # excluded repo whose old path still redirects.
    value = github_token()
    forge.repo(
        "lib",
        {"README.md": b"lib v2\n", "conf.py": f'token = "{value}"  # gitleaks:allow\n'.encode()},
    )
    renamed.add(moved)
    clock.tick()
    forge.cloned.clear()
    with caplog.at_level(logging.INFO):
        assert run(pool, builder) == 0
    status = json.loads((root / "status" / f"{SERVER}.json").read_text())
    assert {r: s["state"] for r, s in status["repos"].items()} == {
        app: "ok",
        lib: "withheld",
        moved: "excluded",
        vend: "withheld",
        art: "withheld",
        k8s: "withheld",
    }
    assert (current / lib / "README.md").read_text() == "lib v1\n"  # its last good copy
    assert status["repos"][lib]["commit"] == first["repos"][lib]["commit"]
    assert not (current / moved).exists() and moved not in forge.cloned
    assert "github-pat" in caplog.text and "conf.py" in caplog.text
    assert value not in caplog.text and vendored not in caplog.text
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            data = p.read_bytes()
            assert value.encode() not in data and vendored.encode() not in data, p
