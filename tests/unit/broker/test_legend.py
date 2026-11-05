"""The agent legend (#231): render, `Legend.sync`, and the broker's mint and reconcile pass."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from crewbroker.config import CrewConfig
from crewbroker.legend import MARKER_PROP, NOTE, Legend, render
from crewbroker.service import Broker
from crewbroker.state import Account

TEAM, OAW = "oaw", "oaw"
TEAM_ID, OAW_ID, TOWN_ID = "tid.oaw", "cid.oaw", "cid-town"
NOW = datetime(2026, 11, 4, tzinfo=UTC)


def acct(name: str, project_dir: str | None = None, *, in_scope: bool = True, **kw: Any) -> Account:
    return Account(
        name=name,
        bot_user_id=f"b-{name}",
        last_register=NOW,
        pruned_at=kw.get("pruned_at"),
        project_dir=project_dir,
        in_scope=in_scope,
    )


def rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("| `")]


# -- render ---------------------------------------------------------------------------------
def test_render_sorted_table() -> None:
    text = render([acct("zed.oaw", "zeta"), acct("abe.oaw", "alpha")])
    assert rows(text) == ["| `abe.oaw` | `alpha` |", "| `zed.oaw` | `zeta` |"]
    assert NOTE in text and "| Agent | Directory |" in text
    assert render([acct("abe.oaw", "alpha"), acct("zed.oaw", "zeta")]) == text  # stable


def test_render_out_of_scope_name_only() -> None:
    text = render([acct("out.oaw", "secret-project", in_scope=False), acct("old.oaw", None)])
    assert rows(text) == ["| `old.oaw` |  |", "| `out.oaw` |  |"]
    assert "secret-project" not in text


def test_render_skips_deactivated() -> None:
    text = render([acct("live.oaw", "a"), acct("gone.oaw", "b", pruned_at=NOW)])
    assert "gone.oaw" not in text and rows(text) == ["| `live.oaw` | `a` |"]


def test_render_never_shows_full_path() -> None:
    text = render([acct("abe.oaw", "/home/someone/src/alpha/")])
    (row,) = rows(text)
    assert row == "| `abe.oaw` | `alpha` |"
    assert "/" not in row.split("|")[2]


def test_render_cells_cannot_break_out() -> None:
    """A directory is the agent's word: no pipe, backtick or line break leaves its code span,
    so it can't add a column, render markdown or mention anyone."""
    text = render([acct("abe.oaw", "a|b`@channel\nx")])
    (row,) = rows(text)
    assert row == "| `abe.oaw` | `a/b'@channelx` |"


def test_render_with_no_agents() -> None:
    assert "(none yet)" in render([])


# -- Legend.sync ----------------------------------------------------------------------------
@pytest.fixture
def legend(fake: Any, tmp_path: Path) -> Legend:
    return Legend(fake, tmp_path)


def legend_calls(fake: Any) -> list[str]:
    names = {"create_post", "get_post", "patch_post", "pin_post", "delete_post"}
    return [c for c, _ in fake.calls if c in names]


def stored(tmp_path: Path) -> dict[str, Any]:
    return dict(json.loads((tmp_path / "legend.json").read_text()))


def test_sync_creates_and_pins_once(fake: Any, legend: Legend, tmp_path: Path) -> None:
    assert legend.sync(OAW_ID, [acct("abe.oaw", "alpha")]) == "created"
    post_id = stored(tmp_path)["post_id"]
    assert post_id in fake.pinned
    assert fake.by_id[post_id]["props"] == {MARKER_PROP: "v1"}
    assert legend_calls(fake) == ["create_post", "pin_post"]


def test_sync_noop_when_unchanged(fake: Any, legend: Legend) -> None:
    legend.sync(OAW_ID, [acct("abe.oaw", "alpha")])
    fake.calls.clear()
    assert legend.sync(OAW_ID, [acct("abe.oaw", "alpha")]) == "unchanged"
    assert fake.calls == []


def test_sync_edits_in_place(fake: Any, legend: Legend, tmp_path: Path) -> None:
    legend.sync(OAW_ID, [acct("abe.oaw", "alpha")])
    post_id = stored(tmp_path)["post_id"]
    fake.calls.clear()
    assert legend.sync(OAW_ID, [acct("abe.oaw", "alpha"), acct("bo.oaw", "beta")]) == "edited"
    assert legend_calls(fake) == ["patch_post"]
    assert "`bo.oaw`" in fake.by_id[post_id]["message"]
    assert stored(tmp_path)["post_id"] == post_id


def test_sync_after_restart_edits_the_same_post(fake: Any, tmp_path: Path) -> None:
    Legend(fake, tmp_path).sync(OAW_ID, [acct("abe.oaw", "alpha")])
    post_id = stored(tmp_path)["post_id"]
    fake.calls.clear()
    again = Legend(fake, tmp_path)  # a restart: the stored post is read back once
    assert again.sync(OAW_ID, [acct("abe.oaw", "alpha")]) == "unchanged"
    assert legend_calls(fake) == ["get_post"]
    assert again.sync(OAW_ID, [acct("abe.oaw", "alpha"), acct("bo.oaw", "beta")]) == "edited"
    assert stored(tmp_path)["post_id"] == post_id
    assert legend_calls(fake) == ["get_post", "patch_post"]


def test_sync_recreates_after_deleted_post(fake: Any, legend: Legend, tmp_path: Path) -> None:
    legend.sync(OAW_ID, [acct("abe.oaw", "alpha")])
    old = stored(tmp_path)["post_id"]
    fake.deleted.add(old)
    assert legend.sync(OAW_ID, [acct("bo.oaw", "beta")]) == "created"
    new = stored(tmp_path)["post_id"]
    assert new != old and new in fake.pinned


def test_sync_recreates_on_restart_when_post_deleted(fake: Any, tmp_path: Path) -> None:
    Legend(fake, tmp_path).sync(OAW_ID, [acct("abe.oaw", "alpha")])
    old = stored(tmp_path)["post_id"]
    fake.deleted.add(old)
    assert Legend(fake, tmp_path).sync(OAW_ID, [acct("abe.oaw", "alpha")]) == "created"
    assert stored(tmp_path)["post_id"] != old


def test_sync_repins_an_unpinned_post_after_restart(fake: Any, tmp_path: Path) -> None:
    Legend(fake, tmp_path).sync(OAW_ID, [acct("abe.oaw", "alpha")])
    post_id = stored(tmp_path)["post_id"]
    fake.pinned.discard(post_id)  # someone unpinned it
    fake.calls.clear()
    assert Legend(fake, tmp_path).sync(OAW_ID, [acct("abe.oaw", "alpha")]) == "pinned"
    assert legend_calls(fake) == ["get_post", "pin_post"] and post_id in fake.pinned


def test_sync_recreates_when_edit_is_refused(fake: Any, legend: Legend, tmp_path: Path) -> None:
    """A 403 on the edit, as under a server's post-edit time limit: a new post is pinned, and
    the old one, still showing the old table, is deleted."""
    legend.sync(OAW_ID, [acct("abe.oaw", "alpha")])
    old = stored(tmp_path)["post_id"]
    fake.patch_status = 403
    assert legend.sync(OAW_ID, [acct("abe.oaw", "alpha", in_scope=False)]) == "created"
    new = stored(tmp_path)["post_id"]
    assert new != old and fake.pinned == {new}
    assert old in fake.deleted and stored(tmp_path)["stale"] == []


def test_a_replaced_post_the_server_wont_delete_is_retried(
    fake: Any, legend: Legend, tmp_path: Path
) -> None:
    legend.sync(OAW_ID, [acct("abe.oaw", "alpha")])
    old = stored(tmp_path)["post_id"]
    fake.patch_status, fake.delete_fails = 403, True
    assert legend.sync(OAW_ID, [acct("bo.oaw", "beta")]) == "created"
    assert stored(tmp_path)["stale"] == [old]
    fake.patch_status, fake.delete_fails = None, False
    fake.calls.clear()
    assert legend.sync(OAW_ID, [acct("bo.oaw", "beta")]) == "unchanged"
    assert legend_calls(fake) == ["delete_post"]
    assert old in fake.deleted and stored(tmp_path)["stale"] == []


def test_an_unpin_found_at_start_survives_a_failed_edit(fake: Any, tmp_path: Path) -> None:
    Legend(fake, tmp_path).sync(OAW_ID, [acct("abe.oaw", "alpha")])
    post_id = stored(tmp_path)["post_id"]
    fake.pinned.discard(post_id)
    fake.patch_status = 500
    restarted = Legend(fake, tmp_path)
    assert restarted.sync(OAW_ID, [acct("bo.oaw", "beta")]) == "failed"
    fake.patch_status = None
    assert restarted.sync(OAW_ID, [acct("bo.oaw", "beta")]) == "edited"
    assert post_id in fake.pinned


def test_sync_survives_429(fake: Any, legend: Legend, tmp_path: Path) -> None:
    """A 429 the client could not wait out fails the pass, never raises, and the next pass
    sends the edit."""
    legend.sync(OAW_ID, [acct("abe.oaw", "alpha")])
    fake.patch_status = 429
    assert legend.sync(OAW_ID, [acct("bo.oaw", "beta")]) == "failed"
    assert "bo.oaw" not in stored(tmp_path)["text"]
    fake.patch_status = None
    assert legend.sync(OAW_ID, [acct("bo.oaw", "beta")]) == "edited"


def test_sync_retries_a_failed_pin_on_the_same_post(
    fake: Any, legend: Legend, tmp_path: Path
) -> None:
    fake.pin_fails = True
    assert legend.sync(OAW_ID, [acct("abe.oaw", "alpha")]) == "failed"
    post_id = stored(tmp_path)["post_id"]
    fake.pin_fails = False
    assert legend.sync(OAW_ID, [acct("abe.oaw", "alpha")]) == "pinned"
    assert stored(tmp_path)["post_id"] == post_id and post_id in fake.pinned
    assert sum(1 for c in legend_calls(fake) if c == "create_post") == 1


def test_sync_in_a_new_channel_posts_there(fake: Any, legend: Legend, tmp_path: Path) -> None:
    legend.sync(OAW_ID, [acct("abe.oaw", "alpha")])
    old = stored(tmp_path)["post_id"]
    assert legend.sync("cid-other", [acct("abe.oaw", "alpha")]) == "created"
    assert stored(tmp_path)["channel_id"] == "cid-other"
    assert old in fake.deleted and fake.pinned == {stored(tmp_path)["post_id"]}


# -- the broker: mint records, the reconcile pass edits --------------------------------------
@pytest.fixture
def crew(fake: Any, crew_config: CrewConfig, tmp_path: Path, clock: Any) -> Broker:
    fake.add_team(TEAM_ID, TEAM, "Oak and Wave")
    fake.add_channel(TOWN_ID, TEAM_ID, "town-square", "Town Square")
    fake.add_channel(OAW_ID, TEAM_ID, OAW, "oaw")
    config = replace(crew_config, team=TEAM, default_channels=["town-square", OAW])
    return Broker(
        config, fake, tmp_path / "state", clock=clock, epoch=lambda: clock.now.timestamp()
    )


def mint(broker: Broker, dev: str, project_dir: str | None, in_scope: bool = True) -> None:
    args: dict[str, Any] = {"dev_name": dev, "crew": "oaw", "in_scope": in_scope}
    if project_dir is not None:
        args["project_dir"] = project_dir
    broker.mint(args)


def legend_post(fake: Any, tmp_path: Path) -> tuple[str, str]:
    state = json.loads((tmp_path / "state" / "legend.json").read_text())
    return state["post_id"], fake.by_id[state["post_id"]]["message"]


def test_mint_records_the_legend_fields_and_makes_no_legend_call(fake: Any, crew: Broker) -> None:
    mint(crew, "abe", "alpha")
    recorded = crew.accounts.get("abe.oaw")
    assert recorded is not None and (recorded.project_dir, recorded.in_scope) == ("alpha", True)
    mint(crew, "abe", "alpha", in_scope=False)  # the latest mint's word wins
    recorded = crew.accounts.get("abe.oaw")
    assert recorded is not None and not recorded.in_scope
    assert legend_calls(fake) == []


def test_register_without_project_dir(fake: Any, crew: Broker, tmp_path: Path) -> None:
    """An older shim, or an older daemon: the mint still works, and the agent is listed by
    name only."""
    crew.mint({"dev_name": "old", "crew": "oaw"})
    acct_ = crew.accounts.get("old.oaw")
    assert acct_ is not None and acct_.project_dir is None and not acct_.in_scope
    crew.reconcile_membership()
    _, text = legend_post(fake, tmp_path)
    assert "| `old.oaw` |  |" in text


def test_reconcile_pass_lists_three_agents_then_one_edit_per_burst(
    fake: Any, crew: Broker, crew_config: CrewConfig, tmp_path: Path, clock: Any
) -> None:
    """The issue's three agents: one in scope, one admitted by `name:` alone and one out of
    scope, both of the last listed by name only, as the daemon computes it (bj:needs #201).
    One pinned post; a burst of re-registers is one edit; a restarted broker edits the same
    post."""
    mint(crew, "inside", "alpha")
    mint(crew, "byname", "beta", in_scope=False)
    mint(crew, "outside", "gamma", in_scope=False)
    assert crew.reconcile_membership() is not None
    post_id, text = legend_post(fake, tmp_path)
    assert post_id in fake.pinned
    assert rows(text) == [
        "| `byname.oaw` |  |",
        "| `inside.oaw` | `alpha` |",
        "| `outside.oaw` |  |",
    ]
    assert "beta" not in text and "gamma" not in text

    fake.calls.clear()
    for dev, d in (("inside", "alpha2"), ("byname", "beta2"), ("outside", "gamma2")):
        mint(crew, dev, d, in_scope=dev == "inside")
    crew.reconcile_membership()
    assert legend_calls(fake) == ["patch_post"]
    assert legend_post(fake, tmp_path) == (post_id, fake.by_id[post_id]["message"])

    restarted = Broker(
        crew.config, fake, tmp_path / "state", clock=clock, epoch=lambda: clock.now.timestamp()
    )
    mint(restarted, "late", "delta")
    fake.calls.clear()
    restarted.reconcile_membership()
    assert legend_calls(fake) == ["get_post", "patch_post"]
    assert legend_post(fake, tmp_path)[0] == post_id


def test_an_out_of_scope_revoke_drops_the_row_to_the_name(
    fake: Any, crew: Broker, tmp_path: Path
) -> None:
    """bj:needs #201: the daemon retiring an agent from this server's allowlist revokes with
    `out_of_scope`, and the row drops to the name. A rotation's revoke changes nothing."""
    minted = crew.mint({"dev_name": "abe", "crew": "oaw", "project_dir": "alpha", "in_scope": True})
    crew.reconcile_membership()
    fresh = crew.remint({"dev_name": "abe", "crew": "oaw", "current_token_id": minted["token_id"]})
    assert crew.revoke({"token_id": minted["token_id"]}) == {}  # the daily rotation
    recorded = crew.accounts.get("abe.oaw")
    assert recorded is not None and recorded.in_scope

    assert crew.revoke({"token_id": fresh["token_id"], "out_of_scope": True}) == {}
    recorded = crew.accounts.get("abe.oaw")
    assert recorded is not None and not recorded.in_scope
    assert recorded.project_dir == "alpha"  # kept for a later in-scope mint
    crew.reconcile_membership()
    assert rows(legend_post(fake, tmp_path)[1]) == ["| `abe.oaw` |  |"]

    # Back on the allowlist: the next mint shows the directory again.
    mint(crew, "abe", "alpha")
    crew.reconcile_membership()
    assert rows(legend_post(fake, tmp_path)[1]) == ["| `abe.oaw` | `alpha` |"]


def test_a_deactivated_agent_leaves_the_legend(fake: Any, crew: Broker, tmp_path: Path) -> None:
    mint(crew, "abe", "alpha")
    mint(crew, "bo", "beta")
    crew.reconcile_membership()
    crew.deactivate({"dev_name": "bo", "crew": "oaw"})
    crew.reconcile_membership()
    assert "bo.oaw" not in legend_post(fake, tmp_path)[1]


def test_no_oaw_channel_posts_nothing(
    fake: Any, crew_config: CrewConfig, tmp_path: Path, clock: Any, caplog: Any
) -> None:
    fake.add_team(TEAM_ID, TEAM, "Oak and Wave")
    fake.add_channel(TOWN_ID, TEAM_ID, "town-square", "Town Square")
    config = replace(crew_config, team=TEAM, default_channels=["town-square"])
    broker = Broker(config, fake, tmp_path / "state", clock=clock)
    with caplog.at_level(logging.WARNING, logger="crewbroker.service"):
        assert broker.sync_legend() is None
        assert broker.sync_legend() is None
    assert caplog.text.count("no 'oaw' channel") == 1  # once per process, not every pass
    assert legend_calls(fake) == []


def test_no_team_no_legend(broker: Broker) -> None:
    assert broker.sync_legend() is None
