"""#139: the kiosk's corpus pool manifest, per research server (D-112)."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from crewchat.corpus_pool import PoolManifest, pool_repos

ORG = "forge.example.lan/partner-org"
REPO_A = f"{ORG}/site-manifests"
REPO_B = f"{ORG}/group/service"
FOREIGN = "github.com/elsewhere/repo"


def research(raw: dict[str, Any], **server: Any) -> dict[str, Any]:
    """Research on for `dev`, with an allowlist that admits ORG's agents and repos."""
    raw["mattermost"][0].update(kiosk=True, kiosk_research=True, **server)
    raw["crews"]["oaw"]["dev"]["agents_allow"] = [ORG, "name:helper", "/home/agent/work"]
    return raw


def wired(make_daemon: Callable[[dict[str, Any]], Any], raw: dict[str, Any], root: Path) -> Any:
    daemon = make_daemon(raw)
    daemon.pool = PoolManifest(
        daemon.config,
        daemon.store.agents,
        root,
        clock=lambda: datetime(2026, 10, 30, tzinfo=UTC),
        corpus_status=root.parent / "corpus-status",  # never a host's real corpus
    )
    daemon.registry.pool = daemon.pool
    return daemon


def manifest(root: Path, server: str = "dev") -> dict[str, Any]:
    return json.loads((root / f"{server}.json").read_text())


def register(daemon: Any, dev: str, key: str = ORG, repo: str = "") -> Any:
    return asyncio.run(daemon.registry.register(dev, "oaw", "s1", project_key=key, repo=repo))


def test_pool_membership(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    """admitted(agent repos + corpus_extra) - corpus_exclude, admitted by host/org only."""
    raw = research(
        servers_toml,
        corpus_extra=[f"{ORG}/docs", FOREIGN],
        corpus_exclude=[f"{ORG}/secret-infra"],
    )
    daemon = wired(make_daemon, raw, tmp_path)
    register(daemon, "babel", repo=REPO_A)
    register(daemon, "kessel", repo=REPO_B)
    register(daemon, "infra", repo=f"{ORG}/secret-infra")
    # Admitted by name and by path, but their repos' orgs are on no forge allow entry.
    register(daemon, "helper", key="", repo=FOREIGN)
    register(daemon, "local", key="/home/agent/work/x", repo="other.lan/org/repo")
    register(daemon, "nore", repo="")  # a path-keyed repo: no forge remote, nothing pooled

    got = manifest(tmp_path)
    assert got["repos"] == [f"{ORG}/docs", REPO_B, REPO_A]
    assert got["server"] == "dev" and got["refresh_minutes"] == 15
    assert got["max_turns"] == 8  # kiosk_max_turns, for #141's model proxy
    assert got["exclude"] == [f"{ORG}/secret-infra"]  # for #140's check on canonical paths
    assert got["generated_at"] == "2026-10-30T00:00:00+00:00"
    assert daemon.pool.health() == {"dev": {"pool_repos": 3}}


def test_deny_wins_for_repos(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    raw = research(servers_toml, corpus_extra=[REPO_A])
    raw["crews"]["oaw"]["dev"]["agents_deny"] = [ORG]
    daemon = make_daemon(raw)
    assert pool_repos(daemon.config, daemon.store.agents(), "dev") == []


def test_pool_follows_retire(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    """An agent that no longer holds a token on the server leaves its pool."""
    raw = research(servers_toml)
    daemon = wired(make_daemon, raw, tmp_path)
    register(daemon, "babel", repo=REPO_A)
    assert manifest(tmp_path)["repos"] == [REPO_A]
    # Its project_key falls off the allowlist: register retires its token on dev.
    register(daemon, "babel", key="forge.example.lan/other", repo=REPO_A)
    assert daemon.store.token_for("babel", "dev") is None
    assert manifest(tmp_path)["repos"] == []


def test_repo_persists_with_tokens(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    """The repo is stored beside the tokens, so a restarted daemon pools the same repos."""
    raw = research(servers_toml)
    daemon = wired(make_daemon, raw, tmp_path / "pool")
    register(daemon, "babel", repo=REPO_A)
    path = daemon.store.path_for("babel")
    assert json.loads(path.read_text())["repo"] == REPO_A
    again = make_daemon(raw)
    again.store.load()
    assert pool_repos(again.config, again.store.agents(), "dev") == [REPO_A]
    # A token file from before #139 has no `repo`, and loads with none.
    data = json.loads(path.read_text())
    del data["repo"]
    path.write_text(json.dumps(data))
    again = make_daemon(raw)
    again.store.load()
    assert again.store.agent("babel").repo == ""


def test_manifest_atomic_and_unchanged(
    make_daemon: Callable[[dict[str, Any]], Any],
    servers_toml: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = research(servers_toml)
    daemon = wired(make_daemon, raw, tmp_path)
    register(daemon, "babel", repo=REPO_A)
    path = tmp_path / "dev.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no temp file left
    before = path.stat().st_mtime_ns
    path.touch()
    touched = path.stat().st_mtime_ns
    daemon.pool.clock = lambda: datetime(2026, 10, 31, tzinfo=UTC)
    register(daemon, "babel", repo=REPO_A)  # same list: not rewritten
    assert path.stat().st_mtime_ns == touched and touched >= before
    assert manifest(tmp_path)["generated_at"].startswith("2026-10-30")

    # A write that fails is logged; register still succeeds.
    daemon.pool.root = tmp_path / "missing"
    with caplog.at_level(logging.ERROR, logger="crewchat.corpus_pool"):
        reg = register(daemon, "kessel", repo=REPO_B)
    assert "mattermost:dev" in reg.backends
    assert "cannot write" in caplog.text


def test_research_off_has_no_manifest(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    """Research off: no manifest, an old one is removed, and health adds nothing."""
    (tmp_path / "dev.json").write_text("{}")
    daemon = wired(make_daemon, servers_toml, tmp_path)
    register(daemon, "babel", repo=REPO_A)
    assert not (tmp_path / "dev.json").exists()
    assert daemon.pool.health() == {}


def test_health_reports_pool(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    from crewchat.server import build_handlers

    daemon = wired(make_daemon, research(servers_toml), tmp_path)
    register(daemon, "babel", repo=REPO_A)
    health = build_handlers(daemon.config, daemon.registry)["health"]
    assert asyncio.run(health({}))["kiosk_research"] == {"dev": {"pool_repos": 1}}
    plain = make_daemon(servers_toml)
    assert "kiosk_research" not in asyncio.run(
        build_handlers(plain.config, plain.registry)["health"]({})
    )


def test_register_repo_over_the_socket(
    make_daemon: Callable[[dict[str, Any]], Any],
    servers_toml: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`repo` is passed through; a non-string is bad_request; a malformed one is dropped."""
    from crewchat.server import DaemonServer, build_handlers

    daemon = wired(make_daemon, research(servers_toml), tmp_path)
    server = DaemonServer(build_handlers(daemon.config, daemon.registry))

    def call(**args: Any) -> dict[str, Any]:
        frame = {"proto": 1, "id": "x", "op": "register", "args": args}
        return asyncio.run(server.handle_line(json.dumps(frame).encode()))

    assert call(dev_name="babel", session_id="s1", project_key=ORG, repo=REPO_A)["ok"]
    assert daemon.store.agent("babel").repo == REPO_A
    bad = call(dev_name="babel", session_id="s1", project_key=ORG, repo=[REPO_A])
    assert bad["ok"] is False and bad["error"].startswith("bad_request")
    with caplog.at_level(logging.WARNING, logger="crewchat.server"):
        got = call(dev_name="babel", session_id="s1", project_key=ORG, repo=f"{ORG}/../x")
    assert got["ok"] and daemon.store.agent("babel").repo == ""
    assert "dropping a malformed repo" in caplog.text
    assert "/../x" not in caplog.text  # never the value: it may hold part of a credential


def test_exclude_and_dedupe_ignore_case(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    """Forges resolve paths without regard to case: an exclude wins however the repo is
    spelled, and one repo spelled two ways is pooled once."""
    raw = research(servers_toml, corpus_exclude=[f"{ORG}/secret-infra"], corpus_extra=[REPO_A])
    daemon = wired(make_daemon, raw, tmp_path)
    register(daemon, "infra", repo=f"{ORG}/Secret-Infra")
    register(daemon, "babel", repo=f"{ORG}/Site-Manifests")
    assert manifest(tmp_path)["repos"] == [f"{ORG}/Site-Manifests"]
    # A forge clones `repo.git` as `repo`: the suffix dodges no exclude, and pools no twin.
    register(daemon, "infra", repo=f"{ORG}/secret-infra.git")
    register(daemon, "other", repo=f"{ORG}/secret-infra.git.git")
    register(daemon, "caps", repo=f"{ORG}/Secret-Infra.GIT")
    register(daemon, "bare", repo=f"{ORG}/.git")  # names the org, not a repo: never pooled
    register(daemon, "babel", repo=f"{REPO_A}.git")  # babel's repo is now REPO_A's spelling
    assert manifest(tmp_path)["repos"] == [REPO_A]


def test_exclude_written_with_git_suffix_still_wins(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    raw = research(servers_toml, corpus_exclude=[f"{ORG}/Secret-Infra.git"])
    daemon = wired(make_daemon, raw, tmp_path)
    register(daemon, "infra", repo=f"{ORG}/secret-infra")
    register(daemon, "babel", repo=f"{REPO_B}.git")
    assert manifest(tmp_path)["repos"] == [REPO_B]
    assert manifest(tmp_path)["exclude"] == [f"{ORG}/secret-infra"]  # canonical


@pytest.mark.parametrize("wiki", [".wiki", ".WIKI", ".wiki.git"])
def test_exclude_covers_the_repos_wiki(
    make_daemon: Callable[[dict[str, Any]], Any],
    servers_toml: dict[str, Any],
    tmp_path: Path,
    wiki: str,
) -> None:
    """`<repo>.wiki` is the excluded repo's wiki, a repo of its own on GitHub and GitLab."""
    raw = research(servers_toml, corpus_exclude=[f"{ORG}/secret-infra"])
    daemon = wired(make_daemon, raw, tmp_path)
    register(daemon, "infra", repo=f"{ORG}/Secret-Infra{wiki}")
    register(daemon, "babel", repo=f"{REPO_A}.wiki")  # a wiki nobody excluded is pooled
    assert manifest(tmp_path)["repos"] == [f"{REPO_A}.wiki"]


def test_agent_repo_needs_its_own_crews_allow(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    """Another crew's allowlist never vouches for an agent: its repo must pass its own crew's
    entry. `corpus_extra`, the operator's, may pass any crew's."""
    other = "forge.example.lan/other-org"
    raw = research(servers_toml, corpus_extra=[f"{other}/handbook"])
    raw["crews"]["partner"] = {
        "needs_list": "bj:needs",
        "dev": {
            "broker_socket": "/run/crewbroker/partner-dev.sock",
            "broker_user_id": "b-partner",
            "agents_allow": [other],
        },
    }
    daemon = wired(make_daemon, raw, tmp_path)
    register(daemon, "babel", repo=f"{other}/private")  # an oaw agent, an org only partner allows
    asyncio.run(
        daemon.registry.register("pal", "partner", "s2", project_key=other, repo=f"{other}/tool")
    )
    assert manifest(tmp_path)["repos"] == [f"{other}/handbook", f"{other}/tool"]


def test_unwritable_pool_reported_in_health(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    """`health` counts the manifest on disk: a write that fails reports the error, not N."""
    pool = tmp_path / "pool"
    pool.mkdir()
    daemon = wired(make_daemon, research(servers_toml), pool)
    pool.chmod(0o555)
    try:
        register(daemon, "babel", repo=REPO_A)
        health = daemon.pool.health()["dev"]
        assert health["pool_repos"] is None and health["error"].startswith("cannot write")
    finally:
        pool.chmod(0o755)
    register(daemon, "babel", repo=REPO_A)
    assert daemon.pool.health() == {"dev": {"pool_repos": 1}}


def test_health_reports_the_corpus_build(
    make_daemon: Callable[[dict[str, Any]], Any], servers_toml: dict[str, Any], tmp_path: Path
) -> None:
    """#140: once crewkiosk-corpus has built a server, `health` shows its last build, naming
    withheld and failed repos, and only counting excluded ones."""
    from crewchat.server import build_handlers

    pool = tmp_path / "pool"
    pool.mkdir()
    daemon = wired(make_daemon, research(servers_toml), pool)
    register(daemon, "babel", repo=REPO_A)
    assert daemon.pool.health() == {"dev": {"pool_repos": 1}}  # not built yet

    status_dir = tmp_path / "corpus-status"
    status_dir.mkdir()
    repos = {
        REPO_A: {"state": "ok"},
        REPO_B: {"state": "withheld"},
        f"{ORG}/c": {"state": "clone_failed"},
        f"{ORG}/d": {"state": "excluded"},
        f"{ORG}/e": {"state": "withheld"},
    }
    (status_dir / "dev.json").write_text(
        json.dumps({"server": "dev", "built_at": "2026-10-30T12:00:00+00:00", "repos": repos})
    )
    health = build_handlers(daemon.config, daemon.registry)["health"]
    assert asyncio.run(health({}))["kiosk_research"]["dev"]["corpus"] == {
        "built_at": "2026-10-30T12:00:00+00:00",
        "repos": 5,
        "ok": 1,
        "withheld": sorted([REPO_B, f"{ORG}/e"]),
        "clone_failed": [f"{ORG}/c"],
        "excluded": 1,
    }
    assert f"{ORG}/d" not in json.dumps(daemon.pool.health())

    (status_dir / "dev.json").write_text("{")
    assert daemon.pool.health()["dev"]["corpus"]["error"].startswith("cannot read")
    (status_dir / "dev.json").write_text("[]")
    assert daemon.pool.health()["dev"]["corpus"] == {
        "error": f"{status_dir / 'dev.json'} has no repos"
    }


def test_unremovable_old_manifest_never_fails_register(
    make_daemon: Callable[[dict[str, Any]], Any],
    servers_toml: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Research off with an old manifest the daemon can't remove: logged, and `register`
    still succeeds."""
    pool = tmp_path / "pool"
    pool.mkdir()
    (pool / "dev.json").write_text("{}")
    daemon = wired(make_daemon, servers_toml, pool)
    pool.chmod(0o555)
    try:
        assert register(daemon, "babel", repo=REPO_A) is not None
    finally:
        pool.chmod(0o755)
    assert (pool / "dev.json").exists()
    assert any("cannot remove" in r.getMessage() for r in caplog.records)


def test_pool_dir_is_setgid_crewcorpus() -> None:
    """Each manifest must get the crewcorpus group, which the daemon isn't in: the directory
    is setgid, so a file created there takes its group."""
    conf = Path(__file__).parents[3] / "packaging" / "crewchat-pool.tmpfiles.conf"
    (line,) = [ln for ln in conf.read_text().splitlines() if ln.startswith("d ")]
    assert line.split() == ["d", "/var/lib/crewchat-pool", "2750", "crewchat", "crewcorpus", "-"]


def test_manifest_takes_the_directory_group(tmp_path: Path) -> None:
    """In a setgid directory owned by another of our groups, the manifest gets that group."""
    import os

    from crewchat.corpus_pool import _write

    others = [g for g in os.getgroups() if g != os.getegid()]
    if not others:
        pytest.skip("this user has no second group to test setgid with")
    os.chown(tmp_path, -1, others[0])
    os.chmod(tmp_path, 0o2750)
    _write(tmp_path / "dev.json", {"repos": []})
    assert (tmp_path / "dev.json").stat().st_gid == others[0]
