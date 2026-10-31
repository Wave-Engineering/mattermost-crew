"""Story 1.3: the broker's ops and its socket (R-05, R-08, R-22; D-100)."""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from crewbroker import mattermost
from crewbroker.config import CrewConfig
from crewbroker.mattermost import DeadlineExceeded
from crewbroker.membership import DEADLINE_RESERVE_S, DEFERRED
from crewbroker.service import (
    Broker,
    BrokerServer,
    OpError,
    call,
    handle_frame,
    start_membership_reconciler,
)

ALL_OPS: dict[str, dict[str, Any]] = {
    "mint": {"dev_name": "babel", "crew": "oaw"},
    "remint": {"dev_name": "babel", "crew": "oaw"},
    "revoke": {"token_id": "t1"},
    "deactivate": {"dev_name": "babel", "crew": "oaw"},
    "crew_info": {},
    "switch_state": {},
}


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # A unix socket path is limited to 108 bytes; pytest's tmp_path can be longer.
    with tempfile.TemporaryDirectory(prefix="cb-", dir="/tmp") as d:
        yield Path(d)


def serving(broker: Broker, path: Path, allowed_uid: int) -> tuple[BrokerServer, threading.Thread]:
    server = BrokerServer(path, broker, allowed_uid)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_remint_keeps_old_token_until_revoke(broker: Broker, fake: Any) -> None:
    first = broker.mint({"dev_name": "babel", "crew": "oaw"})
    second = broker.remint({"dev_name": "babel", "crew": "oaw"})
    assert second["token_id"] != first["token_id"]
    assert set(second) == {"token", "token_id"}
    assert fake.token_works(first["token"]) and fake.token_works(second["token"])

    assert broker.revoke({"token_id": first["token_id"]}) == {}
    assert not fake.token_works(first["token"])
    assert fake.token_works(second["token"])
    assert broker.tokens.get(first["token_id"]) is None
    # Only tokens this broker minted can be revoked through it.
    with pytest.raises(OpError, match="not_found"):
        broker.revoke({"token_id": first["token_id"]})
    with pytest.raises(OpError, match="not_found"):
        broker.remint({"dev_name": "never", "crew": "oaw"})


CANARY = {"dev_name": "verify-canary", "crew": "oaw"}


def test_deactivate_disables_and_reads_back(broker: Broker, fake: Any) -> None:
    """#127: the bot is disabled, `pruned_at` recorded, and `delete_at` is the server's."""
    token = broker.mint(CANARY)["token"]
    acct = broker.accounts.get("verify-canary.oaw")
    assert acct is not None
    reads = fake.count("get_bot")

    reply = broker.deactivate(CANARY)

    assert reply == {"account": "verify-canary.oaw", "delete_at": 1}
    assert fake.bots[acct.bot_user_id]["delete_at"]
    assert fake.count("disable_bot") == 1
    assert fake.count("get_bot") == reads + 2  # the owner check, then the read-back
    pruned = broker.accounts.get("verify-canary.oaw")
    assert pruned is not None and pruned.pruned_at is not None
    assert not fake.token_works(token)
    # Again: disabled again, and the same answer.
    assert broker.deactivate(CANARY)["delete_at"] == 1


def test_deactivate_refusals(broker: Broker, fake: Any) -> None:
    with pytest.raises(OpError, match="not_found"):
        broker.deactivate(CANARY)
    with pytest.raises(OpError, match="bad_request"):
        broker.deactivate({"dev_name": "verify-canary", "crew": "otter"})
    with pytest.raises(OpError, match="bad_request"):
        broker.deactivate({"dev_name": "verify-canary"})
    broker.mint(CANARY)
    acct = broker.accounts.get("verify-canary.oaw")
    assert acct is not None
    for owner in ("someone-else", None):  # reassigned: the owner check, or a 404
        if owner is None:
            del fake.bots[acct.bot_user_id]
        else:
            fake.reassign(acct.bot_user_id, owner)
        with pytest.raises(OpError, match="squatted"):
            broker.deactivate(CANARY)
    assert fake.count("disable_bot") == 0


def test_deactivate_upstream_failure_is_reported(broker: Broker, fake: Any) -> None:
    broker.mint(CANARY)
    acct = broker.accounts.get("verify-canary.oaw")
    assert acct is not None
    fake.disable_fails.add(acct.bot_user_id)
    with pytest.raises(OpError, match="upstream"):
        broker.deactivate(CANARY)
    still = broker.accounts.get("verify-canary.oaw")
    assert still is not None and still.pruned_at is None


def test_mint_after_deactivate_reenables(broker: Broker, fake: Any) -> None:
    broker.mint(CANARY)
    broker.deactivate(CANARY)
    token = broker.mint(CANARY)["token"]
    acct = broker.accounts.get("verify-canary.oaw")
    assert acct is not None and acct.pruned_at is None
    assert not fake.bots[acct.bot_user_id]["delete_at"]
    assert fake.token_works(token)


# -- membership on mint (#132, D-109) ---------------------------------------------------------
TEAM, PUBLIC, PRIVATE = "oaw", "town-square", "crew-private"  # URL names, as configured
TEAM_ID, PUBLIC_ID, PRIVATE_ID = "tid-oaw", "cid-town", "cid-private"
BABEL = {"dev_name": "babel", "crew": "oaw"}
ADDS = ("add_team_member", "add_channel_member")
LOOKUPS = (
    "get_team_by_name",
    "get_channel_by_name",
    "list_my_teams",
    "list_team_channels",
    "list_my_team_channels",
)


@pytest.fixture
def server(fake: Any) -> Any:
    """The names exist on the server, and the broker account is in all three, as on oaw-dev."""
    fake.add_team(TEAM_ID, TEAM, "Oak and Wave")
    fake.add_channel(PUBLIC_ID, TEAM_ID, PUBLIC, "Town Square")
    fake.add_channel(PRIVATE_ID, TEAM_ID, PRIVATE, "Crew Private", "P")
    return fake


def joiner(fake: Any, crew_config: CrewConfig, path: Path, clock: Any, *channels: str) -> Broker:
    config = replace(crew_config, team=TEAM, default_channels=list(channels or (PUBLIC, PRIVATE)))
    return Broker(config, fake, path, clock=clock)


@pytest.fixture
def joining(server: Any, crew_config: CrewConfig, tmp_path: Path, clock: Any) -> Broker:
    return joiner(server, crew_config, tmp_path / "joining", clock)


def _adds(fake: Any) -> list[tuple[str, Any]]:
    return [(call, arg[0]) for call, arg in fake.calls if call in ADDS]


def _lookups(fake: Any) -> int:
    return sum(1 for call, _ in fake.calls if call in LOOKUPS)


def test_names_resolve_at_startup_and_are_logged(
    server: Any,
    crew_config: CrewConfig,
    tmp_path: Path,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="crewbroker.membership"):
        built = joiner(server, crew_config, tmp_path / "b", clock)
    assert built.membership is not None
    assert [t.id for t in built.membership.targets()] == [TEAM_ID, PUBLIC_ID, PRIVATE_ID]
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert logged == [
        f"membership: team {TEAM} → {TEAM_ID}",
        f"membership: channel {PUBLIC} → {PUBLIC_ID}",
        f"membership: channel {PRIVATE} → {PRIVATE_ID}",
    ]
    # Resolved once: a mint asks the server for no names again.
    before = _lookups(server)
    built.mint(BABEL)
    assert _lookups(server) == before


def _resolved(
    fake: Any, crew_config: CrewConfig, path: Path, clock: Any, team: str, *ch: str
) -> Any:
    config = replace(crew_config, team=team, default_channels=list(ch))
    built = Broker(config, fake, path, clock=clock)
    assert built.membership is not None
    return built.membership


def test_display_names_resolve_ignoring_case(
    server: Any, crew_config: CrewConfig, tmp_path: Path, clock: Any
) -> None:
    """What a person types: "Oak and Wave", "town square", a private channel the broker is in."""
    m = _resolved(
        server, crew_config, tmp_path, clock, "Oak and Wave", "town square", "CREW PRIVATE"
    )
    assert [t.id for t in m.targets()] == [TEAM_ID, PUBLIC_ID, PRIVATE_ID]
    # A display name that is also slug-shaped is tried as a URL name first, then matched.
    server.add_channel("cid-ops", TEAM_ID, "agent-ops-7", "ops")
    m = _resolved(server, crew_config, tmp_path / "b", clock, TEAM, "ops")
    assert m.channels[0].id == "cid-ops"
    # An entry that can't be a URL name is never sent to a `/name/` endpoint.
    assert not [a for c, a in server.calls if c == "get_team_by_name" and a == "Oak and Wave"]


def test_display_name_sees_only_what_the_broker_can(
    server: Any, crew_config: CrewConfig, tmp_path: Path, clock: Any
) -> None:
    """A private channel the broker isn't in, a team it isn't on, a DM: none resolves."""
    server.add_channel("cid-secret", TEAM_ID, "secret", "Secret", "P", mine=False)
    server.add_channel("cid-dm", TEAM_ID, "a__b", "Secret", "D")
    server.add_team("tid-other", "other", "Elsewhere", mine=False)
    m = _resolved(server, crew_config, tmp_path, clock, TEAM, "Secret")
    assert m.channels[0].id is None and m.channels[0].final
    assert "visible only if the broker is a member" in m.channels[0].error
    m = _resolved(server, crew_config, tmp_path / "b", clock, "Elsewhere")
    assert m.team.id is None and m.team.final


def test_ambiguous_display_name_names_its_candidates(
    server: Any,
    crew_config: CrewConfig,
    tmp_path: Path,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server.add_channel("cid-gen1", TEAM_ID, "general", "General")
    server.add_channel("cid-gen2", TEAM_ID, "general-2", "general")
    with caplog.at_level(logging.ERROR, logger="crewbroker.membership"):
        m = _resolved(server, crew_config, tmp_path, clock, TEAM, "GENERAL")
    channel = m.channels[0]
    assert channel.id is None and channel.final
    for part in ("matches 2 channels", "cid-gen1", "cid-gen2", "general-2"):
        assert part in channel.error
    assert channel.error in caplog.text
    # The same display name on a private channel the broker is in counts as a candidate too.
    server.add_channel("cid-gen3", TEAM_ID, "gen-priv", "General", "P")
    assert (
        "matches 3 channels"
        in _resolved(server, crew_config, tmp_path / "b", clock, TEAM, "General").channels[0].error
    )


def test_first_mint_joins_the_team_then_each_channel(joining: Broker, fake: Any) -> None:
    reply = joining.mint(BABEL)
    bot_id = joining.accounts.get("babel.oaw").bot_user_id  # type: ignore[union-attr]

    assert reply["membership"] == {"joined": [TEAM, PUBLIC, PRIVATE], "failed": []}
    assert _adds(fake) == [
        ("add_team_member", TEAM_ID),
        ("add_channel_member", PUBLIC_ID),
        ("add_channel_member", PRIVATE_ID),
    ]
    assert all(fake.members[t] == [bot_id] for t in (TEAM_ID, PUBLIC_ID, PRIVATE_ID))
    assert fake.token_works(reply["token"])


@pytest.mark.parametrize("exists_as_400", [False, True], ids=["201", "400-exists"])
def test_second_mint_counts_existing_membership_as_joined(
    joining: Broker, fake: Any, exists_as_400: bool
) -> None:
    joining.mint(BABEL)
    fake.exists_as_400 = exists_as_400
    again = joining.mint(BABEL)
    assert again["membership"] == {"joined": [TEAM, PUBLIC, PRIVATE], "failed": []}
    assert all(len(fake.members[t]) == 1 for t in (TEAM_ID, PUBLIC_ID, PRIVATE_ID))


def test_failed_channel_add_still_returns_the_token(
    joining: Broker, fake: Any, caplog: pytest.LogCaptureFixture
) -> None:
    fake.add_fails.add(PUBLIC_ID)
    with caplog.at_level(logging.ERROR, logger="crewbroker.membership"):
        reply = joining.mint(BABEL)
    bot_id = joining.accounts.get("babel.oaw").bot_user_id  # type: ignore[union-attr]

    assert fake.token_works(reply["token"])
    assert reply["membership"]["joined"] == [TEAM, PRIVATE]  # the next channel is still tried
    [failure] = reply["membership"]["failed"]
    assert failure["name"] == PUBLIC and failure["id"] == PUBLIC_ID
    assert "403" in failure["error"]
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    for part in ("babel.oaw", bot_id, PUBLIC, PUBLIC_ID, "403"):
        assert part in record.getMessage()


def test_failed_team_add_skips_the_channels(joining: Broker, fake: Any) -> None:
    fake.add_fails.add(TEAM_ID)
    reply = joining.mint(BABEL)
    assert fake.token_works(reply["token"])
    assert reply["membership"]["joined"] == []
    assert [f["name"] for f in reply["membership"]["failed"]] == [TEAM, PUBLIC, PRIVATE]
    assert "skipped" in reply["membership"]["failed"][1]["error"]
    assert _adds(fake) == [("add_team_member", TEAM_ID)]


def test_unknown_channel_name_never_stops_the_broker(
    server: Any,
    crew_config: CrewConfig,
    tmp_path: Path,
    clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo is ERROR at startup and in every mint's `failed`; the broker starts and mints."""
    with caplog.at_level(logging.ERROR, logger="crewbroker.membership"):
        built = joiner(server, crew_config, tmp_path / "b", clock, PUBLIC, "town-sqaure")
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "no channel has the URL name or display name 'town-sqaure'" in record.getMessage()

    for dev in ("babel", "kessel"):
        reply = built.mint({"dev_name": dev, "crew": "oaw"})
        assert server.token_works(reply["token"])
        assert reply["membership"]["joined"] == [TEAM, PUBLIC]
        [failure] = reply["membership"]["failed"]
        assert failure["name"] == "town-sqaure" and failure["id"] is None
    # A 404 is final until a restart: the name is not looked up on every mint.
    assert (
        sum(1 for c, a in server.calls if c == "get_channel_by_name" and a[1] == "town-sqaure") == 1
    )


def test_unknown_team_fails_every_target_and_adds_nothing(
    fake: Any, crew_config: CrewConfig, tmp_path: Path, clock: Any
) -> None:
    built = joiner(fake, crew_config, tmp_path / "b", clock)  # no team on the server at all
    reply = built.mint(BABEL)
    assert fake.token_works(reply["token"])
    assert reply["membership"]["joined"] == []
    failed = reply["membership"]["failed"]
    assert [f["name"] for f in failed] == [TEAM, PUBLIC, PRIVATE]
    assert "no team has" in failed[0]["error"] and "unresolved" in failed[1]["error"]
    assert _adds(fake) == []


@pytest.mark.parametrize("status", [503, 429], ids=["down", "rate-limited"])
def test_lookup_that_failed_in_passing_is_retried_on_the_next_mint(
    server: Any, crew_config: CrewConfig, tmp_path: Path, clock: Any, status: int
) -> None:
    """The server was down or rate-limiting when the broker started: not final, so a mint
    resolves again."""
    server.lookup_down = True
    server.lookup_status = status
    built = joiner(server, crew_config, tmp_path / "b", clock)
    first = built.mint(BABEL)
    assert first["membership"]["joined"] == []
    assert str(status) in first["membership"]["failed"][0]["error"]

    server.lookup_down = False
    second = built.mint(BABEL)
    assert second["membership"] == {"joined": [TEAM, PUBLIC, PRIVATE], "failed": []}


def test_no_membership_config_makes_no_calls(broker: Broker, fake: Any) -> None:
    reply = broker.mint(BABEL)
    assert "membership" not in reply
    assert _adds(fake) == [] and _lookups(fake) == 0
    assert broker.membership is None


def test_membership_only_for_a_bot_this_broker_owns(joining: Broker, fake: Any) -> None:
    joining.mint(BABEL)
    bot_id = joining.accounts.get("babel.oaw").bot_user_id  # type: ignore[union-attr]
    before = len(_adds(fake))
    # A reassigned bot: the mint itself refuses it, so nothing is added.
    fake.reassign(bot_id, "someone-else")
    with pytest.raises(OpError, match="squatted"):
        joining.mint(BABEL)
    assert len(_adds(fake)) == before


def test_deadline_near_defers_every_add(
    joining: Broker, fake: Any, clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The token is the point of the mint: with less than the reserve left, nothing is added,
    every target is reported as deferred, and the next register joins them."""
    deadline = clock.now.timestamp() + DEADLINE_RESERVE_S / 2
    with caplog.at_level(logging.WARNING, logger="crewbroker.membership"):
        reply = joining.mint({**BABEL, "deadline": deadline})

    assert fake.token_works(reply["token"])
    assert reply["membership"]["joined"] == []
    failed = reply["membership"]["failed"]
    assert [(f["name"], f["error"]) for f in failed] == [
        (TEAM, DEFERRED),
        (PUBLIC, DEFERRED),
        (PRIVATE, DEFERRED),
    ]
    assert _adds(fake) == []
    [record] = [r for r in caplog.records if r.name == "crewbroker.membership"]
    assert record.levelno == logging.WARNING
    for part in ("babel.oaw", TEAM, PUBLIC, PRIVATE):
        assert part in record.getMessage()

    again = joining.mint(BABEL)
    assert again["membership"] == {"joined": [TEAM, PUBLIC, PRIVATE], "failed": []}


def test_deadline_running_out_partway_defers_the_rest(
    joining: Broker, fake: Any, clock: Any
) -> None:
    deadline = clock.now.timestamp() + 2

    def slow(target: str) -> None:
        if target == PUBLIC_ID:
            clock.now += timedelta(seconds=1.8)

    fake.on_add = slow
    reply = joining.mint({**BABEL, "deadline": deadline})
    assert fake.token_works(reply["token"])
    assert reply["membership"] == {
        "joined": [TEAM, PUBLIC],
        "failed": [{"name": PRIVATE, "id": PRIVATE_ID, "error": DEFERRED}],
    }
    assert [target for _, target in _adds(fake)] == [TEAM_ID, PUBLIC_ID]


def test_deadline_near_skips_a_pending_lookup(
    server: Any, crew_config: CrewConfig, tmp_path: Path, clock: Any
) -> None:
    """A name still to resolve is not looked up late: it is deferred like the adds."""
    server.lookup_down = True
    built = joiner(server, crew_config, tmp_path / "b", clock)
    server.lookup_down = False
    lookups = _lookups(server)
    reply = built.mint({**BABEL, "deadline": clock.now.timestamp() + DEADLINE_RESERVE_S / 2})
    assert [f["error"] for f in reply["membership"]["failed"]] == [DEFERRED] * 3
    assert _lookups(server) == lookups


# -- tagged channels, kept by reconcile (#167) ------------------------------------------------
TAGGED, TAGGED_ID = "precheck", "cid-precheck"


def test_mint_joins_tagged_channels(joining: Broker, fake: Any) -> None:
    """A mint adds the new bot to the tagged channels the last pass found, as well as the
    default channels, so a new agent is in every crew channel at once."""
    fake.add_channel(TAGGED_ID, TEAM_ID, TAGGED, "Precheck", header="crew-oaw")
    assert joining.reconcile_membership() == {"added": [], "failed": []}  # no bots yet
    reply = joining.mint(BABEL)
    assert reply["membership"] == {"joined": [TEAM, PUBLIC, PRIVATE, TAGGED], "failed": []}
    acct = joining.accounts.get("babel.oaw")
    assert acct is not None and acct.bot_user_id in fake.members[TAGGED_ID]


def test_reconcile_covers_only_bots_this_broker_owns(joining: Broker, fake: Any) -> None:
    joining.mint(BABEL)
    joining.mint({"dev_name": "kessel", "crew": "oaw"})
    fake.add_channel(TAGGED_ID, TEAM_ID, TAGGED, "Precheck", purpose="CREW-OAW")
    kessel = joining.accounts.get("kessel.oaw")
    assert kessel is not None
    fake.reassign(kessel.bot_user_id, "someone-else")
    result = joining.reconcile_membership()
    assert result == {"added": [{"account": "babel.oaw", "target": TAGGED}], "failed": []}
    assert kessel.bot_user_id not in fake.members.get(TAGGED_ID, [])


def test_bot_minted_while_the_broker_joins_still_joins(joining: Broker, fake: Any) -> None:
    """The race the pass closes: a bot minted while the broker joins a tagged public channel
    is either in the pass's second read of its bots or minted after the channel was published."""
    fake.channel_needs_broker = True
    fake.add_channel(TAGGED_ID, TEAM_ID, TAGGED, "Precheck", mine=False, header="crew-oaw")
    joining.mint(BABEL)
    late: dict[str, Any] = {}

    def mint_during_the_join(target: str) -> None:
        if target == TAGGED_ID and not late:
            late["reply"] = joining.mint({"dev_name": "kessel", "crew": "oaw"})

    fake.on_add = mint_during_the_join
    joining.reconcile_membership()
    # Minted before the channel was published: its own mint couldn't add it there.
    assert TAGGED not in late["reply"]["membership"]["joined"]
    for name in ("babel.oaw", "kessel.oaw"):
        acct = joining.accounts.get(name)
        assert acct is not None and acct.bot_user_id in fake.members[TAGGED_ID], name


def test_reconcile_joins_nothing_without_bots(joining: Broker, fake: Any) -> None:
    fake.channel_needs_broker = True
    fake.add_channel(TAGGED_ID, TEAM_ID, TAGGED, "Precheck", mine=False, header="crew-oaw")
    assert joining.reconcile_membership() == {"added": [], "failed": []}
    assert TAGGED_ID not in fake.members
    fake.list_bots_fails = True
    joining.mint(BABEL)
    with pytest.raises(Exception, match="boom"):
        joining.reconcile_membership()  # the loop logs it; nothing is joined
    assert TAGGED_ID not in fake.members


def test_reconcile_loop_runs_at_start_and_interval(
    joining: Broker, broker: Broker, caplog: pytest.LogCaptureFixture
) -> None:
    passes: list[str] = []
    waits: list[float] = []

    def counted() -> None:
        passes.append("pass")
        if len(passes) == 2:
            raise RuntimeError("boom")  # a failing pass is logged, and the loop goes on

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        return len(waits) == 3  # stop after the third wait

    joining.reconcile_membership = counted  # type: ignore[method-assign]
    stop = threading.Event()
    with caplog.at_level(logging.ERROR, logger="crewbroker"):
        thread = start_membership_reconciler(joining, stop, 300, wait)
        assert thread is not None
        thread.join(5)
    assert passes == ["pass"] * 3  # at startup, then after each interval
    assert waits == [300] * 3
    assert "membership reconcile failed" in caplog.text
    # 0 turns the loop off, and so does a broker with no `team`: no thread, no pass at all.
    passes.clear()
    assert start_membership_reconciler(joining, stop, 0, wait) is None
    assert start_membership_reconciler(broker, threading.Event(), 300, wait) is None
    assert passes == []


def _args(held: str | None) -> dict[str, Any]:
    return {"dev_name": "babel", "crew": "oaw", "current_token_id": held}


def test_timed_out_remint_token_revoked_on_next_call(broker: Broker, fake: Any) -> None:
    """#41: the daemon keeps its token after a remint it timed out on; the orphan is revoked."""
    held = broker.mint(_args(None))
    orphan = broker.remint(_args(held["token_id"]))  # its reply never reached the daemon
    fresh = broker.remint(_args(held["token_id"]))  # the next rotation, still on `held`

    assert not fake.token_works(orphan["token"])
    assert broker.tokens.get(orphan["token_id"]) is None
    # The token the daemon is using is never touched; it revokes it itself after the swap.
    assert fake.token_works(held["token"]) and fake.token_works(fresh["token"])


def test_mint_with_nothing_held_revokes_every_other_token(broker: Broker, fake: Any) -> None:
    orphan = broker.mint(_args(None))  # a register whose reply timed out
    fresh = broker.mint(_args(None))  # the agent registers again, holding nothing
    assert not fake.token_works(orphan["token"])
    assert {r.token_id for r in broker.tokens.all()} == {fresh["token_id"]}


def test_supersede_without_current_token_id_revokes_nothing(broker: Broker, fake: Any) -> None:
    # An older daemon sends no current_token_id: the broker cannot tell what it holds.
    first = broker.mint({"dev_name": "babel", "crew": "oaw"})
    broker.mint({"dev_name": "babel", "crew": "oaw"})
    assert fake.token_works(first["token"])


def test_supersede_errors_never_fail_the_op(broker: Broker, fake: Any) -> None:
    held = broker.mint(_args(None))
    gone = broker.remint(_args(held["token_id"]))
    stuck = broker.remint(_args(held["token_id"]))
    del fake.tokens[gone["token_id"]]  # 404 upstream: cleared
    fake.revoke_fails.add(stuck["token_id"])  # 500 upstream: kept for the next call

    fresh = broker.remint(_args(held["token_id"]))
    assert fake.token_works(fresh["token"])
    assert broker.tokens.get(gone["token_id"]) is None
    assert broker.tokens.get(stuck["token_id"]) is not None
    fake.revoke_fails.clear()
    broker.remint(_args(held["token_id"]))
    assert broker.tokens.get(stuck["token_id"]) is None


def test_current_token_id_must_be_an_id_or_null(broker: Broker) -> None:
    for bad in ("", 7, ["x"]):
        with pytest.raises(OpError, match="bad_request"):
            broker.remint(_args(bad))  # type: ignore[arg-type]


def test_late_request_past_its_deadline_is_refused(
    broker: Broker, fake: Any, clock: Any
) -> None:
    """#41: a call the daemon gave up on, served after a newer one, mints and revokes nothing."""
    held = broker.mint(_args(None))
    sent = clock.now.timestamp()
    clock.now += timedelta(seconds=3)  # the broker was stalled past the 2 s timeout
    newer = broker.mint({**_args(held["token_id"]), "deadline": clock.now.timestamp() + 2})
    tokens_before = fake.count("create_token")

    with pytest.raises(OpError, match="expired"):
        broker.mint({**_args(held["token_id"]), "deadline": sent + 2})
    with pytest.raises(OpError, match="expired"):
        broker.remint({**_args(held["token_id"]), "deadline": sent + 2})
    assert fake.count("create_token") == tokens_before
    assert fake.token_works(newer["token"])


def test_mint_rate_limited_past_deadline_is_expired(
    broker: Broker, fake: Any, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#158: `mint` and `remint` run under `call_deadline(deadline - DEADLINE_RESERVE_S)`, and
    a `create_token` rate limited past it answers `expired` with no token recorded."""
    held = broker.mint(_args(None))
    deadline = clock.now.timestamp() + 2
    bounds: list[float | None] = []

    def rate_limited(user_id: str, description: str, expires_at_ms: int | None = None) -> Any:
        bounds.append(mattermost._local.deadline)
        raise DeadlineExceeded("POST", f"/users/{user_id}/tokens", 429, "rate limited")

    monkeypatch.setattr(fake, "create_token", rate_limited)
    tokens_before = len(broker.tokens.all())
    for op in (broker.mint, broker.remint):
        with pytest.raises(OpError, match="expired"):
            op({**_args(held["token_id"]), "deadline": deadline})
    assert bounds == [deadline - DEADLINE_RESERVE_S] * 2
    assert len(broker.tokens.all()) == tokens_before
    assert getattr(mattermost._local, "deadline", None) is None  # restored after the op


def test_deadline_past_the_mint_is_reported_not_raised(
    broker: Broker, fake: Any, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#158: once the token exists, a deadline that fires in the avatar step leaves the mint
    answered, the flag unset for the next mint."""

    def late(user_id: str, png: bytes) -> None:
        raise DeadlineExceeded("POST", f"/users/{user_id}/image", None, "deadline passed")

    monkeypatch.setattr(fake, "set_user_image", late)
    minted = broker.mint({**_args(None), "deadline": clock.now.timestamp() + 10})
    assert fake.token_works(minted["token"])
    assert not broker.accounts.avatar_set("babel.oaw")


def test_deadline_must_be_epoch_seconds(broker: Broker) -> None:
    for bad in ("soon", True, [1]):
        with pytest.raises(OpError, match="bad_request"):
            broker.mint({**_args(None), "deadline": bad})


def test_supersede_is_capped_per_call(broker: Broker, fake: Any) -> None:
    held = broker.mint(_args(None))
    orphans = [broker.remint({"dev_name": "babel", "crew": "oaw"}) for _ in range(7)]
    broker.remint(_args(held["token_id"]))
    left = [o for o in orphans if fake.token_works(o["token"])]
    assert len(left) == 2  # 5 revoked on this call; the rest wait for the next
    broker.remint(_args(held["token_id"]))
    assert not any(fake.token_works(o["token"]) for o in orphans)
    assert fake.token_works(held["token"])


def test_revoke_notfound_upstream_is_success(broker: Broker, fake: Any) -> None:
    minted = broker.mint({"dev_name": "babel", "crew": "oaw"})
    del fake.tokens[minted["token_id"]]  # already revoked on the server
    assert broker.revoke({"token_id": minted["token_id"]}) == {}
    assert broker.tokens.get(minted["token_id"]) is None


def test_every_op_refused_to_non_daemon_peer(broker: Broker, fake: Any, short_dir: Path) -> None:
    path = short_dir / "oaw-dev.sock"
    server, _ = serving(broker, path, allowed_uid=os.getuid() + 1)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o660
        for op, args in ALL_OPS.items():
            reply = call(str(path), op, args)
            assert reply["ok"] is False and reply["error"] == "forbidden", op
            assert "result" not in reply and "token" not in json.dumps(reply)
        assert fake.count("create_bot") == 0 and fake.tokens == {}
    finally:
        server.shutdown()
        server.server_close()

    # The control: the same socket, with this test's UID as the daemon's, does mint.
    server, _ = serving(broker, path, allowed_uid=os.getuid())
    try:
        reply = call(str(path), "mint", ALL_OPS["mint"])
        assert reply["ok"] is True and reply["result"]["account"] == "babel.oaw"
        assert reply["proto"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_crew_info_lists_minted_bots(broker: Broker, fake: Any) -> None:
    a = broker.mint({"dev_name": "a", "crew": "oaw"})["account"]
    b = broker.mint({"dev_name": "b", "crew": "oaw"})["account"]
    # A bot the broker owns but never minted through ensure_bot is not listed.
    fake.add_user("stray.oaw", is_bot=True, owner_id="broker-user-id")
    info = broker.crew_info({})
    ids = {u["username"]: u["id"] for u in fake.users.values()}
    assert info == {
        "broker_user_id": "broker-user-id",
        "human_user_ids": ["human-user-id"],
        "trusted": ["otter"],
        "minted_bot_ids": [ids[a], ids[b]],
    }


def test_crew_info_drops_reassigned_bot(broker: Broker, fake: Any) -> None:
    broker.mint({"dev_name": "a", "crew": "oaw"})
    broker.mint({"dev_name": "b", "crew": "oaw"})
    ids = {u["username"]: u["id"] for u in fake.users.values()}
    a, b = ids["a.oaw"], ids["b.oaw"]

    # Reassigned: missing from the broker's GET /bots, so dropped.
    fake.reassign(a, "someone-else")
    assert broker.crew_info({})["minted_bot_ids"] == [b]

    # With read_others_bots, GET /bots lists it again, but with another owner_id: dropped.
    fake.read_others_bots = True
    assert a in [bot["user_id"] for bot in fake.list_bots()]
    assert broker.crew_info({})["minted_bot_ids"] == [b]

    # Assigned back: listed again on the next GET /bots.
    fake.read_others_bots = False
    fake.reassign(a, "broker-user-id")
    assert broker.crew_info({})["minted_bot_ids"] == [a, b]

    # GET /bots fails: an error, never the unfiltered list.
    fake.list_bots_fails = True
    with pytest.raises(OpError, match="upstream"):
        broker.crew_info({})
    reply = handle_frame(broker, b'{"proto":1,"id":"x","op":"crew_info","args":{}}', True)
    assert reply["ok"] is False and "result" not in reply


def test_crew_info_leaves_out_pruned_bot(broker: Broker, fake: Any, clock: Any) -> None:
    from datetime import timedelta

    broker.mint({"dev_name": "a", "crew": "oaw"})
    clock.now += timedelta(days=31)
    broker.prune()
    assert broker.crew_info({})["minted_bot_ids"] == []
    broker.mint({"dev_name": "a", "crew": "oaw"})
    assert len(broker.crew_info({})["minted_bot_ids"]) == 1


def test_crew_info_own_fields_only(
    broker: Broker, fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker.mint({"dev_name": "a", "crew": "oaw"})
    connects: list[Any] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def spy_connect(self: socket.socket, address: Any) -> None:
        connects.append(address)
        real_connect(self, address)

    def spy_connect_ex(self: socket.socket, address: Any) -> int:
        connects.append(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", spy_connect_ex)
    info = broker.crew_info({})
    assert connects == []
    # Trusted crews by name only: no other crew's membership data.
    assert info["trusted"] == ["otter"]
    assert set(info) == {"broker_user_id", "human_user_ids", "trusted", "minted_bot_ids"}
    assert all(isinstance(name, str) for name in info["trusted"])


@pytest.mark.parametrize(
    ("line", "error"),
    [
        (b'{"proto":2,"id":"x","op":"crew_info"}', "unsupported"),
        (b'{"proto":1,"id":"x","op":"no_such_op"}', "unsupported"),
        (b"not json", "bad_request: not JSON"),
        (b'{"proto":1,"id":"x","op":"mint","args":[]}', "bad_request: args is an object"),
        (b'{"proto":1,"id":"x","op":"mint","args":{"dev_name":"a","crew":"other"}}', None),
    ],
)
def test_frame_errors(broker: Broker, line: bytes, error: str | None) -> None:
    reply = handle_frame(broker, line, True)
    assert reply["ok"] is False and reply["proto"] == 1
    if error is not None:
        assert reply["error"] == error
    else:
        assert reply["error"].startswith("bad_request")


def test_older_proto_accepted(broker: Broker) -> None:
    reply = handle_frame(broker, b'{"proto":0,"id":"x","op":"crew_info"}', True)
    assert reply["ok"] is True and reply["id"] == "x"
