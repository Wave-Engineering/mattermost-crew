"""#117: which agents `register` mints an account for on a server (`crewchat.allowlist`)."""

from __future__ import annotations

from pathlib import Path

from crewchat.allowlist import allowed, matches
from crewchat.config import BrokerEntry

FORGE = "gitlab.com/partner-org"


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
    assert not allowed(allow, "gitlab.com/partner-org2", "babel")
    assert not allowed(allow, "github.com/partner-org", "babel")
    assert allowed(allow, "/home/agent/work/repo", "babel")
    assert allowed(allow, "/home/agent/work", "babel")
    assert allowed(allow, "github.com/elsewhere", "helper")
    assert not allowed(allow, "github.com/elsewhere", "babel")
    # Deny beats allow, by any form.
    assert not allowed(entry(allow=(FORGE,), deny=("name:babel",)), FORGE, "babel")
    assert not allowed(entry(deny=(FORGE,)), FORGE, "babel")
    assert allowed(entry(deny=(FORGE,)), "github.com/other", "babel")
    # An empty allowlist admits nobody.
    assert not allowed(entry(allow=()), FORGE, "babel")


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
    assert not matches("/a/b", "gitlab.com/a", "x")  # a forge key is never under a path
    assert not matches(FORGE, "/gitlab.com/partner-org", "x")
