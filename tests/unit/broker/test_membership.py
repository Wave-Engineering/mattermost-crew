"""#167: the reconcile pass keeps the broker's bots in the team, the default channels and the
channels tagged `crew-<crew>`."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from crewbroker.membership import Membership, keep_channel, tag_pattern, tagged

BROKER_ID = "broker-user-id"  # the fake's broker account (conftest)
TEAM, TEAM_ID = "oaw", "tid-oaw"
DEFAULT, DEFAULT_ID = "town-square", "cid-town"
OPS, OPS_ID = "precheck", "cid-precheck"  # tagged, public
TAG = "crew-oaw"


@pytest.fixture
def server(fake: Any) -> Any:
    fake.add_team(TEAM_ID, TEAM, "Oak and Wave")
    fake.add_channel(DEFAULT_ID, TEAM_ID, DEFAULT, "Town Square")
    fake.add_channel(OPS_ID, TEAM_ID, OPS, "Precheck", header=f"gate results · {TAG}")
    return fake


def bots(fake: Any, *names: str) -> dict[str, str]:
    """Bots this broker owns, as bot id -> account name."""
    return {fake.add_user(n, is_bot=True, owner_id=BROKER_ID): n for n in names}


def membership(fake: Any, *channels: str) -> Membership:
    built = Membership(fake, TEAM, list(channels or (DEFAULT,)), TAG, BROKER_ID)
    built.resolve()
    return built


def adds(fake: Any) -> list[tuple[str, Any]]:
    return [arg for call, arg in fake.calls if call in ("add_team_member", "add_channel_member")]


def test_reconcile_adds_missing_members(server: Any) -> None:
    owned = bots(server, "babel.oaw", "kessel.oaw")
    result = membership(server).reconcile(list(owned), owned)
    assert result["failed"] == []
    for target in (TEAM_ID, DEFAULT_ID, OPS_ID):
        assert sorted(server.members[target]) == sorted(owned), target
    assert [a["target"] for a in result["added"]] == [TEAM, DEFAULT, OPS] * 2
    assert [a["account"] for a in result["added"][:3]] == ["babel.oaw"] * 3


def test_reconcile_is_idempotent(server: Any) -> None:
    owned = bots(server, "babel.oaw", "kessel.oaw")
    for target in (TEAM_ID, DEFAULT_ID, OPS_ID):
        server.members[target] = list(owned)
    server.exists_as_400 = True  # "already a member" as an error
    built = membership(server)
    first = built.reconcile(list(owned), owned)
    assert first == {"added": [], "failed": []}  # counted as joined, never as failed or added
    # A pair that succeeded once is not sent again while the broker runs.
    sent = len(adds(server))
    assert built.reconcile(list(owned), owned) == {"added": [], "failed": []}
    assert len(adds(server)) == sent


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("crew-oaw", True),
        ("see CREW-OAW here", True),
        ("(crew-oaw).", True),
        ("crew-oawx", False),
        ("xcrew-oaw", False),
        ("crew-oaw-old", False),
        ("crew_oaw", False),
        ("", False),
    ],
)
def test_tag_matches_whole_token_only(text: str, hit: bool) -> None:
    channel = {"id": "c", "type": "O", "delete_at": 0, "header": text}
    assert tagged(channel, tag_pattern(TAG)) is hit


def test_tag_in_purpose_or_header(server: Any) -> None:
    server.add_channel("cid-purpose", TEAM_ID, "ops", "Ops", purpose=f"ask here {TAG}")
    server.add_channel("cid-neither", TEAM_ID, "lunch", "Lunch", header="crew-oawx")
    built = membership(server)
    assert built.scan()
    assert {t.id for t in built.tagged} == {OPS_ID, "cid-purpose"}
    assert [t.name for t in built.channel_targets()] == [DEFAULT, "ops", OPS]


def test_reconcile_skips_archived_and_dm(server: Any) -> None:
    server.add_channel("cid-old", TEAM_ID, "old", "Old", header=TAG)
    server.channels["cid-old"]["delete_at"] = 1
    server.add_channel("cid-dm", TEAM_ID, "a__b", "a, b", "D", header=TAG)
    server.add_channel("cid-gm", TEAM_ID, "gm", "a, b, c", "G", header=TAG)
    owned = bots(server, "babel.oaw")
    membership(server).reconcile(list(owned), owned)
    targets = {target for target, _ in adds(server)}
    assert targets == {TEAM_ID, DEFAULT_ID, OPS_ID}
    assert not keep_channel(server.channels["cid-old"])


def test_reconcile_never_removes(server: Any) -> None:
    owned = bots(server, "babel.oaw")
    [bot] = owned
    server.add_channel("cid-untagged", TEAM_ID, "lunch", "Lunch")
    server.members["cid-untagged"] = [bot, "u-human"]
    membership(server).reconcile(list(owned), owned)
    assert server.members["cid-untagged"] == [bot, "u-human"]
    assert not [call for call, _ in server.calls if "remove" in call]


def test_reconcile_lists_channels_once(server: Any) -> None:
    owned = bots(server, *(f"agent{i}.oaw" for i in range(5)))
    built = membership(server)
    before = (server.count("list_team_channels"), server.count("list_my_team_channels"))
    built.reconcile(list(owned), owned)
    after = (server.count("list_team_channels"), server.count("list_my_team_channels"))
    assert (after[0] - before[0], after[1] - before[1]) == (1, 1)


def test_reconcile_failure_is_reported_not_raised(
    server: Any, caplog: pytest.LogCaptureFixture
) -> None:
    owned = bots(server, "babel.oaw", "kessel.oaw")
    server.add_fails.add(OPS_ID)
    with caplog.at_level(logging.INFO, logger="crewbroker.membership"):
        result = membership(server).reconcile(list(owned), owned)
    # The other adds still happen.
    assert [a["target"] for a in result["added"]] == [TEAM, DEFAULT] * 2
    assert [(f["account"], f["name"]) for f in result["failed"]] == [
        ("babel.oaw", OPS),
        ("kessel.oaw", OPS),
    ]
    # One refusal and the pass stops sending to that channel; one WARNING names it.
    assert [t for t, _ in adds(server)].count(OPS_ID) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and f"tagged channel {OPS} ({OPS_ID})" in warnings[0]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert "membership: reconcile: added babel.oaw to precheck" not in infos
    assert "membership: reconcile: added babel.oaw to town-square" in infos
    assert infos[-1] == "membership: reconcile: 2 bots, 2 channels (1 tagged), 4 added, 2 failed"
    # A default channel's refusal is an ERROR for each add, as on mint.
    server.add_fails = {DEFAULT_ID}
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="crewbroker.membership"):
        membership(server).reconcile(list(owned), owned)
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2


def test_broker_joins_a_tagged_public_channel_first(server: Any) -> None:
    """#132's probe: a `team_user` broker adds its bots only to a channel it is in. A tagged
    public channel it is not in, it joins itself; a private one it can't even see."""
    server.channel_needs_broker = True
    server.my_ids.discard(OPS_ID)
    server.add_channel("cid-secret", TEAM_ID, "secret", "Secret", "P", mine=False, header=TAG)
    owned = bots(server, "babel.oaw")
    [bot] = owned
    built = membership(server)
    result = built.reconcile(list(owned), owned)
    assert result["failed"] == []
    assert server.members[OPS_ID] == [BROKER_ID, bot]
    assert "cid-secret" not in server.members
    # Once in, the broker doesn't join again on the next pass.
    joins = [a for a in adds(server) if a == (OPS_ID, BROKER_ID)]
    built.reconcile(list(owned), owned)
    assert [a for a in adds(server) if a == (OPS_ID, BROKER_ID)] == joins


def test_unresolved_team_adds_nothing(fake: Any) -> None:
    owned = bots(fake, "babel.oaw")
    built = membership(fake)  # no team on the server
    result = built.reconcile(list(owned), owned)
    assert result["added"] == [] and len(result["failed"]) == 1
    assert adds(fake) == []
