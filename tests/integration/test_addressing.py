"""Story 2.3 against the CI leg's Mattermost: IT-07 (R-16, R-17, R-18, R-19).

Real posts reach a registered agent's websocket through the Story 2.1 adapter, and
`addressed_to` / `is_direct_address` judge them: `@agents` is a broadcast, `@all` is not on
Mattermost, `@<dev>.<crew>` stays whole, crew and team tokens match by our own filter (the
team token only for an author of the agent's own crew, D-103), a reply
in a thread the agent posted in is addressed to it, and so is a plain DM to it.

The harness (broker, registry, adapter, inbox) is Story 2.1's, from `test_mattermost_adapter`.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

from crewchat.adapters.base import InboundMessage
from crewchat.addressing import addressed_to, is_broadcast, is_direct_address, tokens
from crewchat.threads import MyThreads

from tests.integration.mmhttp import mm_request
from tests.integration.test_mattermost_adapter import (  # noqa: F401 (`setup` is a fixture)
    Daemon,
    Inbox,
    as_admin_post,
    setup,
)

TEAM = "it07team"


def admin_reply(mattermost: Any, channel_id: str, root_id: str, message: str) -> str:
    resp = mm_request(
        mattermost,
        "POST",
        "/posts",
        json={"channel_id": channel_id, "root_id": root_id, "message": message},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["message"])


def test_it07_addressing(mattermost: Any, setup: dict[str, str], tmp_path: Path) -> None:  # noqa: F811
    """IT-07."""
    channel = setup["channel_id"]
    crew = setup["crew"]

    async def scenario() -> None:
        d = Daemon(mattermost, setup, tmp_path)
        babel_id = await d.register("babel")
        await d.register("kessel")
        registered = d.registry.get("babel")
        assert registered is not None
        reg = dataclasses.replace(registered, dev_team=TEAM)
        threads = MyThreads()
        inbox = Inbox(d.backend, "babel")

        async def arrives(text: str) -> InboundMessage:
            got = await inbox.until(text)
            return got[-1]

        def judge(m: InboundMessage) -> tuple[bool, bool]:
            mine = threads.roots("babel")
            return addressed_to(m, reg, mine), is_direct_address(m, reg, mine)

        try:
            await inbox.connected()

            # R-16: `@agents` is the broadcast; `@all` is not ours on Mattermost.
            m = await arrives(as_admin_post(mattermost, channel, "@agents roll call")["message"])
            assert judge(m) == (True, False)
            m = await arrives(as_admin_post(mattermost, channel, "@all humans only")["message"])
            assert judge(m) == (False, False)
            assert is_broadcast(tokens(m.text), "discord")  # the transition's Discord `@all`

            # R-18: `@babel.<crew>` is one token, and it is direct address.
            text = as_admin_post(mattermost, channel, f"over to @babel.{crew} now")["message"]
            m = await arrives(text)
            assert f"babel.{crew}" in tokens(m.text)
            assert judge(m) == (True, True)
            assert m.mentions == [babel_id]  # the server's own marker agrees (R-29)

            # R-17: crew and team tokens, by our filter; never direct address. On Mattermost
            # the team token needs an author of babel's own crew (D-103): Phase 2 has no
            # membership step, so with `author_crew=None` it fails closed.
            m = await arrives(as_admin_post(mattermost, channel, f"@{crew} crew check")["message"])
            assert judge(m) == (True, False)
            m = await arrives(as_admin_post(mattermost, channel, f"@{TEAM} team check")["message"])
            assert judge(m) == (False, False)
            mine = threads.roots("babel")
            assert addressed_to(m, reg, mine, author_crew=crew)
            assert not addressed_to(m, reg, mine, author_crew="othercrew")
            assert not is_direct_address(m, reg, mine)
            m = await arrives(as_admin_post(mattermost, channel, "@otherteam check")["message"])
            assert judge(m) == (False, False)
            assert not addressed_to(m, reg, threads.roots("babel"), author_crew=crew)

            # R-19: a reply in a thread babel posted in is addressed to it, with no mention.
            root = await d.backend.send("kessel", channel, "kessel starts a thread")
            await arrives(root.text)
            m = await arrives(admin_reply(mattermost, channel, root.post_id, "early"))
            assert m.root_id == root.post_id and judge(m) == (False, False)
            own = await d.backend.send("babel", channel, "babel joins in", thread=root.post_id)
            assert threads.record("babel", own) == root.post_id
            m = await arrives(admin_reply(mattermost, channel, root.post_id, "thanks"))
            assert judge(m) == (True, True)

            # R-19 (D-080): a plain DM to babel, no mention, outside its threads.
            dm = mm_request(
                mattermost, "POST", "/channels/direct", json=[setup["admin_id"], babel_id]
            )
            assert dm.status_code in (200, 201), dm.text
            dm_channel = dm.json()["id"]
            m = await arrives(as_admin_post(mattermost, dm_channel, "got a minute?")["message"])
            assert m.dm is True and tokens(m.text) == set()
            assert judge(m) == (True, True)

            # And channel chatter that names nobody of ours is not addressed.
            m = await arrives(as_admin_post(mattermost, channel, "just chatter")["message"])
            assert m.dm is False and judge(m) == (False, False)
        finally:
            await inbox.close()
            await d.backend.aclose()

    asyncio.run(scenario())
