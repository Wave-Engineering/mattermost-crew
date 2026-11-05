"""#117: which agents `register` mints an account for on a server (`crewchat.allowlist`)."""

from __future__ import annotations

from pathlib import Path

from crewchat.allowlist import admits_repo, allowed, in_legend_scope, matches
from crewchat.config import BrokerEntry

FORGE = "forge.example.lan/partner-org"


def entry(allow: tuple[str, ...] | None = None, deny: tuple[str, ...] = ()) -> BrokerEntry:
    return BrokerEntry(
        crew="oaw",
        server_id="partner",
        broker_socket=Path("/run/crewbroker/oaw-partner.sock"),
        broker_user_id="b-oaw",
        agents_allow=allow,
        agents_deny=deny,
    )


def test_allowed_rules() -> None:
    # No allowlist: every agent, as before #117.
    assert allowed(entry(), FORGE, "babel")
    assert allowed(entry(), "", "babel")
    # Allowlisted by forge key (exact), by path or by name.
    allow = entry(allow=(FORGE, "/home/agent/work", "name:helper"))
    assert allowed(allow, FORGE, "babel")
    assert not allowed(allow, "forge.example.lan/partner-org2", "babel")
    assert not allowed(allow, "other.example.lan/partner-org", "babel")
    assert allowed(allow, "/home/agent/work/repo", "babel")
    assert allowed(allow, "/home/agent/work", "babel")
    assert allowed(allow, "forge.example.lan/elsewhere", "helper")
    assert not allowed(allow, "forge.example.lan/elsewhere", "babel")
    # Deny beats allow, by any form.
    assert not allowed(entry(allow=(FORGE,), deny=("name:babel",)), FORGE, "babel")
    assert not allowed(entry(deny=(FORGE,)), FORGE, "babel")
    assert allowed(entry(deny=(FORGE,)), "forge.example.lan/other", "babel")
    # An empty allowlist admits nobody.
    assert not allowed(entry(allow=()), FORGE, "babel")


def test_in_legend_scope() -> None:
    """#231, bj:needs #201: the legend shows a directory with no allowlist, or for an agent a
    forge or path entry admits. One admitted by `name:` alone shows by name only."""
    assert in_legend_scope(entry(), "forge.example.lan/team-a", "babel")
    assert in_legend_scope(entry(), "", "babel")  # an older shim, no allowlist
    allow = entry(allow=(FORGE, "/home/agent/work", "name:helper"))
    assert in_legend_scope(allow, FORGE, "babel")
    assert in_legend_scope(allow, "/home/agent/work/repo", "babel")
    assert in_legend_scope(allow, FORGE, "helper")  # by name and by forge: the forge decides
    assert not in_legend_scope(allow, "forge.example.lan/team-a", "helper")  # by name alone
    assert not in_legend_scope(allow, "", "helper")
    assert not in_legend_scope(allow, "forge.example.lan/team-a", "babel")
    assert not in_legend_scope(allow, "", "babel")
    assert not in_legend_scope(entry(deny=("name:babel",)), FORGE, "babel")  # deny wins


def test_admits_repo() -> None:
    """#139, D-112: a repo enters a pool only by its host/org on a forge allow entry."""
    repo = f"{FORGE}/group/service"
    assert admits_repo(entry(allow=(FORGE,)), repo)
    assert admits_repo(entry(allow=(FORGE,)), f"{FORGE}/site")
    assert not admits_repo(entry(allow=(FORGE,)), "forge.example.lan/partner-org2/x")
    assert not admits_repo(entry(allow=(FORGE,), deny=(FORGE,)), repo)  # deny wins
    # Path and name entries never admit a repo, and no allowlist admits none.
    assert not admits_repo(entry(allow=("/", "name:babel")), repo)
    assert not admits_repo(entry(), repo)
    assert not admits_repo(entry(allow=()), repo)


def test_empty_project_key_fails_closed() -> None:
    """An old shim sends no `project_key`: it matches no forge or path entry."""
    allow = entry(allow=(FORGE, "/"))
    assert not allowed(allow, "", "babel")
    assert allowed(entry(allow=("name:babel",)), "", "babel")  # a name entry still matches


def test_path_matches_by_component() -> None:
    assert matches("/a/b", "/a/b", "x")
    assert matches("/a/b", "/a/b/c", "x")
    assert matches("/a/b/", "/a/b/c", "x")
    assert not matches("/a/b", "/a/bc", "x")
    assert not matches("/a/b", "/a", "x")
    assert not matches("/a/b", "forge.example.lan/a", "x")  # a forge key is never under a path
    assert not matches(FORGE, "/forge.example.lan/partner-org", "x")
