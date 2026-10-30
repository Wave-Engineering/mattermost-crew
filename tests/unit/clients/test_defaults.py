"""Story 2.4: R-11's default-backend resolution (Dev Spec §5.10)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from crewchat_clients.defaults import (
    DefaultsError,
    NeedsHuman,
    forge_key,
    forge_repo,
    git_remote,
    repo_key,
    repo_ref,
    resolve_default,
    write_default,
)

REMOTE = "git@github.com:Wave-Engineering/mattermost-crew.git"


def no_remote(_root: Path) -> str | None:
    return None


def github_remote(_root: Path) -> str | None:
    return REMOTE


def test_resolution_order(tmp_path: Path) -> None:
    defaults = tmp_path / "chat-defaults.json"
    defaults.write_text(json.dumps({"github.com/Wave-Engineering": "mattermost:oaw-dev"}))

    # (1) the identity file's home_backend wins over the defaults file.
    got = resolve_default(
        {"home_backend": "discord"}, tmp_path, path=defaults, remote_for=github_remote
    )
    assert got == "discord"
    # (2) without it, the defaults file's entry for the forge org.
    got = resolve_default({}, tmp_path, path=defaults, remote_for=github_remote)
    assert got == "mattermost:oaw-dev"
    # (3) with neither, NeedsHuman, carrying the key the answer goes under.
    got = resolve_default({}, tmp_path, path=tmp_path / "absent.json", remote_for=github_remote)
    assert got == NeedsHuman(key="github.com/Wave-Engineering")
    # No remote: the key is the repo path.
    got = resolve_default({}, tmp_path, path=defaults, remote_for=no_remote)
    assert got == NeedsHuman(key=str(tmp_path.resolve()))
    defaults.write_text(json.dumps({str(tmp_path.resolve()): "discord"}))
    assert resolve_default({}, tmp_path, path=defaults, remote_for=no_remote) == "discord"
    # A malformed home_backend is an error, never a silent fallback.
    with pytest.raises(DefaultsError):
        resolve_default({"home_backend": "slack"}, tmp_path, path=defaults, remote_for=no_remote)


@pytest.mark.parametrize(
    ("remote", "key"),
    [
        ("git@github.com:Wave-Engineering/mattermost-crew.git", "github.com/Wave-Engineering"),
        ("ssh://git@gitlab.com:2222/example-group/sub/repo.git", "gitlab.com/example-group"),
        ("ssh://gitlab.com/example-group/repo", "gitlab.com/example-group"),
        ("https://github.com/Wave-Engineering/mattermost-crew.git", "github.com/Wave-Engineering"),
        ("https://user@GitLab.com/example-group/repo", "gitlab.com/example-group"),
        ("http://git.lan:8080/org/repo/", "git.lan/org"),
        ("/srv/git/repo.git", None),
        ("file:///srv/git/repo.git", None),
        ("../sibling", None),
        ("", None),
    ],
)
def test_forge_key_from_remote(remote: str, key: str | None) -> None:
    assert forge_key(remote) == key


@pytest.mark.parametrize(
    ("remote", "repo"),
    [
        (
            "git@github.com:Wave-Engineering/mattermost-crew.git",
            "github.com/Wave-Engineering/mattermost-crew",
        ),
        (
            "ssh://git@gitlab.com:2222/example-group/sub/repo.git",
            "gitlab.com/example-group/sub/repo",
        ),
        ("https://user:tok@GitLab.com/example-group/repo", "gitlab.com/example-group/repo"),
        ("http://git.lan:8080/org/repo/", "git.lan/org/repo"),
        ("/srv/git/repo.git", None),
        ("file:///srv/git/repo.git", None),
        ("", None),
    ],
)
def test_forge_repo_forms(remote: str, repo: str | None) -> None:
    """#139: the full repo, nested groups kept, `.git` and credentials dropped."""
    assert forge_repo(remote) == repo


def test_repo_ref(tmp_path: Path) -> None:
    assert repo_ref(tmp_path, lambda _root: None) == ""
    assert repo_ref(tmp_path, lambda _root: REMOTE) == "github.com/Wave-Engineering/mattermost-crew"


def test_repo_key_and_git_remote(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert git_remote(tmp_path) is None
    assert repo_key(tmp_path) == str(tmp_path.resolve())
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", REMOTE], check=True)
    assert git_remote(tmp_path) == REMOTE
    assert repo_key(tmp_path) == "github.com/Wave-Engineering"


def test_write_default_merges_and_validates(tmp_path: Path) -> None:
    path = tmp_path / "home" / ".claude" / "chat-defaults.json"
    write_default("github.com/a", "discord", path)
    write_default("/repo", "mattermost:oaw-dev", path)
    want = {"github.com/a": "discord", "/repo": "mattermost:oaw-dev"}
    assert json.loads(path.read_text()) == want
    with pytest.raises(DefaultsError):
        write_default("k", "telegram", path)
    path.write_text("{not json")
    with pytest.raises(DefaultsError):
        write_default("k", "discord", path)
    assert path.read_text() == "{not json"  # a broken file is never overwritten
