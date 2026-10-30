"""Fixtures for the corpus builder (#140): a local forge, the fake gitleaks and a builder."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from crewkiosk.corpus import Builder

from tests.fakes.forge import TOKEN, Clock, Forge, install_fake_gitleaks


@pytest.fixture
def forge(tmp_path: Path) -> Forge:
    return Forge(tmp_path / "forge")


@pytest.fixture
def fake_gitleaks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fake on disk, its calls logged to `<tmp>/gitleaks.log`, one JSON line each."""
    monkeypatch.setenv("FAKE_GITLEAKS_LOG", str(tmp_path / "gitleaks.log"))
    return install_fake_gitleaks(tmp_path / "bin")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def credential(tmp_path: Path) -> Path:
    path = tmp_path / "corpus-dev.credential"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def make_builder(
    tmp_path: Path, forge: Forge, fake_gitleaks: Path, credential: Path, clock: Clock
) -> Callable[..., Builder]:
    """`make_builder(lookup=..., gitleaks=...)`: a builder over the local forge, rooted at
    `<tmp>/corpus`, whose lookup answers each repo's own path unless given another."""

    def make(
        lookup: Callable[[str, str], str] | None = None, gitleaks: str | None = None
    ) -> Builder:
        (tmp_path / "corpus").mkdir(exist_ok=True)  # tmpfiles' job on a host
        return Builder(
            tmp_path / "corpus",
            gitleaks=gitleaks or str(fake_gitleaks),
            credential=lambda server: credential,
            lookup=lookup or (lambda repo, token: repo),
            clone_url=forge.url,
            clock=clock,
        )

    return make
