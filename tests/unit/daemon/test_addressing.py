"""Story 2.3: addressing (R-16 to R-19) and the `MyThreads` store."""

from __future__ import annotations

import asyncio
import dataclasses
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from crewchat.adapters.base import InboundMessage
from crewchat.adapters.mattermost import MattermostBackend, event_dm
from crewchat.addressing import (
    addressed_to,
    group_tokens,
    is_broadcast,
    is_direct_address,
    team_scope,
    tokens,
)
from crewchat.registry import Registration
from crewchat.threads import MyThreads

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts" / "ci" / "no-user-groups.sh"

MM = "mattermost:dev"
DISCORD = "discord"


def reg(dev: str = "kessel", crew: str = "oaw", dev_team: str = "waveteam") -> Registration:
    return Registration(
        dev_name=dev,
        crew=crew,
        session_id="s",
        account=f"{dev}.{crew}",
        backends=["dev"],
        needs_list="bj:needs",
        registered_at=datetime.now(UTC),
        dev_team=dev_team,
    )


def msg(
    text: str = "",
    *,
    backend: str = MM,
    root_id: str = "",
    post_id: str = "p1",
    dm: bool = False,
) -> InboundMessage:
    return InboundMessage(
        backend=backend,
        channel="c1",
        post_id=post_id,
        root_id=root_id,
        author_id="u-bj",
        author_name="bj",
        text=text,
        dm=dm,
    )


def test_agents_broadcast_both_backends() -> None:
    for backend in (MM, "mattermost:other", DISCORD):
        assert is_broadcast(tokens("heads up @agents"), backend)
        assert addressed_to(msg("@agents stand up", backend=backend), reg(), set())


def test_all_only_on_discord() -> None:
    assert is_broadcast(tokens("@all please"), DISCORD)
    assert not is_broadcast(tokens("@all please"), MM)
    assert addressed_to(msg("@all please", backend=DISCORD), reg(), set())
    assert not addressed_to(msg("@all please", backend=MM), reg(), set())


def test_dot_kept_in_name_crew() -> None:
    assert tokens("over to @kessel.oaw now") == {"kessel.oaw"}
    assert "kesseloaw" not in tokens("@kessel.oaw")
    assert addressed_to(msg("@kessel.oaw ping"), reg(), set())
    # The dot-joined name of another crew's kessel is not ours.
    assert not addressed_to(msg("@kessel.frogstar ping"), reg(), set())
    # Sentence punctuation after a name is not part of it.
    assert tokens("ask @kessel.oaw. Then @babel, ok?") == {"kessel.oaw", "babel"}


def test_tokens_skip_email_and_ignore_case() -> None:
    assert tokens("mail bob@example.com") == set()
    assert tokens("@Kessel.OAW") == {"kessel.oaw"}
    assert tokens("no tokens here") == set()
    assert tokens("@a_b-c.d") == {"a_b-c.d"}


def test_crew_and_team_tokens() -> None:
    r = reg()
    assert addressed_to(msg("@oaw anyone?"), r, set())
    assert addressed_to(msg("@waveteam anyone?"), r, set(), author_crew="oaw")
    assert addressed_to(msg("@WaveTeam anyone?"), r, set(), author_crew="oaw")
    assert addressed_to(msg("@WaveTeam anyone?", backend=DISCORD), r, set())
    assert not addressed_to(msg("@frogstar anyone?"), r, set())
    # No Dev-Team: only the crew token is ours.
    assert not addressed_to(msg("@waveteam anyone?"), reg(dev_team=""), set(), author_crew="oaw")
    assert not addressed_to(msg("@waveteam anyone?", backend=DISCORD), reg(dev_team=""), set())
    assert addressed_to(msg("@oaw anyone?"), reg(dev_team=""), set())


def test_team_scope_policy() -> None:
    """D-103: Discord's team trust is the backend; every Mattermost server's is the crew."""
    assert team_scope("discord") == "backend"
    assert team_scope("discord:x") == "backend"
    assert team_scope(MM) == "crew"
    assert team_scope("mattermost:other") == "crew"


def test_team_token_discord_no_author_check() -> None:
    """On Discord a non-agent author's `@<dev-team>` reaches the team, with no crew known."""
    r = reg(dev_team="oaw", crew="frogstar")
    m = msg("@oaw who's around?", backend=DISCORD)
    assert addressed_to(m, r, set(), author_crew=None)
    assert "oaw" in group_tokens(r, DISCORD, None)


def test_team_token_mattermost_fails_closed() -> None:
    """On Mattermost the team token needs an author of the agent's own crew (D-103)."""
    r = reg(dev_team="waveteam", crew="oaw")
    m = msg("@waveteam who's around?", backend=MM)
    assert not addressed_to(m, r, set(), author_crew=None)
    assert not addressed_to(m, r, set())  # the Phase 2 default
    assert addressed_to(m, r, set(), author_crew="oaw")
    assert not addressed_to(m, r, set(), author_crew="frogstar")
    assert group_tokens(r, MM, None) == {"oaw"}
    # The crew token itself is never gated.
    assert addressed_to(msg("@oaw ping", backend=MM), r, set(), author_crew=None)


def test_avatar_never_addresses() -> None:
    r = dataclasses.replace(reg(), dev_avatar="lighthouse")
    for backend in (MM, DISCORD):
        m = msg("@lighthouse hello", backend=backend)
        assert not addressed_to(m, r, set(), author_crew="oaw")
        assert not is_direct_address(m, r, set())


def test_reply_to_me_by_thread_root() -> None:
    threads = MyThreads()
    # kessel started a thread (a root post) and posted in another (a reply).
    assert threads.record("kessel", msg(post_id="root-a")) == "root-a"
    assert threads.record("kessel", msg(post_id="p9", root_id="root-b")) == "root-b"
    mine = threads.roots("kessel")
    assert mine == {"root-a", "root-b"}
    for root in ("root-a", "root-b"):
        assert addressed_to(msg("sounds good", root_id=root, post_id="p10"), reg(), mine)
    assert not addressed_to(msg("sounds good", root_id="root-c"), reg(), mine)
    # A root post (no thread) with an empty root ID never matches.
    assert not addressed_to(msg("sounds good"), reg(), mine | {""})
    # Another agent's threads are not kessel's.
    assert threads.roots("babel") == set()


def test_dm_is_addressed() -> None:
    plain = msg("hey, got a minute?", dm=True)
    assert tokens(plain.text) == set()
    assert addressed_to(plain, reg(), set())
    assert not addressed_to(msg("hey, got a minute?"), reg(), set())


def test_direct_address_excludes_broadcast_and_crew_tokens() -> None:
    r = reg()
    mine = {"root-a"}
    assert is_direct_address(msg("@kessel look"), r, mine)
    assert is_direct_address(msg("@kessel.oaw look"), r, mine)
    assert is_direct_address(msg("look", dm=True), r, mine)
    assert is_direct_address(msg("look", root_id="root-a"), r, mine)
    for text, backend in [
        ("@agents look", MM),
        ("@agents look", DISCORD),
        ("@all look", DISCORD),
        ("@oaw look", MM),
        ("@waveteam look", MM),
        ("@waveteam look", DISCORD),
    ]:
        m = msg(text, backend=backend)
        assert addressed_to(m, r, mine, author_crew="oaw"), text
        assert not is_direct_address(m, r, mine), text


def test_unaddressed_message() -> None:
    assert not addressed_to(msg("general chatter @babel"), reg(), {"root-a"})


def test_my_threads_bounded_and_forget() -> None:
    threads = MyThreads(limit=2)
    for root in ("a", "b", "a", "c"):
        threads.add("kessel", root)
    assert threads.roots("kessel") == {"a", "c"}  # "a" was refreshed, so "b" aged out
    threads.add("kessel", "")
    assert threads.roots("kessel") == {"a", "c"}
    threads.forget("kessel")
    assert threads.roots("kessel") == set()
    with pytest.raises(ValueError):
        MyThreads(limit=0)


def test_event_dm_needs_both_members() -> None:
    data = {"channel_type": "D", "channel_name": "u-me__u-bj"}
    assert event_dm(data, "u-bj", "u-me")
    assert event_dm({**data, "channel_name": "u-bj__u-me"}, "u-bj", "u-me")
    assert not event_dm(data, "u-other", "u-me")
    assert not event_dm(data, "u-bj", None)
    assert not event_dm({"channel_type": "O", "channel_name": "u-me__u-bj"}, "u-bj", "u-me")
    assert not event_dm({"channel_type": "G", "channel_name": "x"}, "u-bj", "u-me")
    assert not event_dm({"channel_type": "D"}, "u-bj", "u-me")


def test_stream_marks_dm(mm: Any, recording_sleep: Any, run: Any) -> None:
    """A `posted` event in a DM between the author and the agent arrives with `dm` set."""
    me = mm.add_user("kessel.oaw", is_bot=True)
    token = mm.add_token(me)
    bj = mm.add_user("bj")
    b = MattermostBackend(
        "dev",
        mm.URL,
        lambda dev, sid: token,
        http_client=mm.http_client(),
        connect=mm.connect,
        sleep=recording_sleep,
        max_retries=0,
    )

    async def scenario() -> list[InboundMessage]:
        gen = b.stream("kessel")
        got: list[InboundMessage] = []
        first = asyncio.ensure_future(asyncio.wait_for(anext(gen), 2))
        for _ in range(2000):
            if mm.open_sockets(me):
                break
            await asyncio.sleep(0)
        mm.broadcast_posted(
            mm.add_post(bj, "dm-chan", "hi"), channel_type="D", channel_name=f"{bj}__{me}"
        )
        got.append(await first)
        mm.broadcast_posted(mm.add_post(bj, "chan", "in the open"))
        got.append(await asyncio.wait_for(anext(gen), 2))
        await b.close_agent("kessel")
        return got

    dm_msg, open_msg = run(scenario())
    assert dm_msg.dm is True and dm_msg.text == "hi"
    assert open_msg.dm is False
    assert addressed_to(dm_msg, reg(), set())
    assert not addressed_to(open_msg, reg(), set())


def test_no_user_groups_gate_passes_on_packages() -> None:
    """R-17, CT-01: no code path calls the Mattermost user-groups API."""
    result = subprocess.run([str(GATE)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "line",
    [
        'await client.get("/api/v4/groups")',
        'path = f"/users/{uid}/groups"',
        "await client.get_groups()",
        "members = await client.get_group_members(gid)",
    ],
)
def test_no_user_groups_gate_catches_a_call(tmp_path: Path, line: str) -> None:
    (tmp_path / "bad.py").write_text(line + "\n")
    result = subprocess.run([str(GATE), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "bad.py" in result.stderr
