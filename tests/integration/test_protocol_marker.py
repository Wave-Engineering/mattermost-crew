"""IT-06 (R-14, D-036): the protocol marker, written and read, on both backends.

Every message the daemon writes carries the protocol version marker, and a message without one
parses as `proto` 0, without error:

- Mattermost, written: a post made through the adapter's `send` carries
  `props.crew_proto == PROTO`, read back through `GET /api/v4/posts/{id}`.
- Mattermost, proto 0: a post the admin makes directly, with no `crew_proto` prop, reads back
  through the adapter's `read` with `proto == 0`, its text and author intact.
- Discord, written: a `chat_send` with `backend="discord"` stores content ending in the
  ` ·p<PROTO>` suffix, and reads back with `proto == PROTO`.
- Discord, proto 0: two messages without the suffix, one with today's signature form
  `— **name** avatar (team)` and one with no signature at all, both read back with `proto == 0`.

The fixtures are the existing ones: the Mattermost daemon of `test_mattermost_adapter`, and the
scream-hole and Discord REST fake behind the daemon socket of `test_chat_contract`. The
Mattermost cases skip without `MM_URL`, like every other live IT; the Discord cases need only
the fakes, so they run locally too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from crewchat.protocol import PROTO

from tests.fakes.screamhole import FakeDiscord
from tests.integration.test_chat_contract import (  # noqa: F401 (`sock_dir` is a fixture)
    HUMAN,
    Host,
    make_repo,
    make_shim,
    sock_dir,
)
from tests.integration.test_mattermost_adapter import (  # noqa: F401 (`setup` is a fixture)
    Daemon,
    admin,
    as_admin_post,
    setup,
)


# -- Mattermost --------------------------------------------------------------------------------
def test_it06_mattermost_send_writes_crew_proto(
    mattermost: Any, setup: dict[str, str], tmp_path: Path  # noqa: F811
) -> None:
    """IT-06, Mattermost written (R-14): `send` marks the post `props.crew_proto = PROTO`."""
    channel = setup["channel_id"]

    async def scenario() -> None:
        d = Daemon(mattermost, setup, tmp_path)
        await d.register("proto-writer")
        try:
            sent = await d.backend.send("proto-writer", channel, "it06 marked post")
            assert sent.proto == PROTO
        finally:
            await d.backend.aclose()
        stored = admin(mattermost, "GET", f"/posts/{sent.post_id}")
        assert stored.status_code == 200, stored.text
        assert stored.json()["props"]["crew_proto"] == PROTO

    asyncio.run(scenario())


def test_it06_mattermost_unmarked_post_reads_as_proto_0(
    mattermost: Any, setup: dict[str, str], tmp_path: Path  # noqa: F811
) -> None:
    """IT-06, Mattermost proto 0 (R-14): a post with no `crew_proto` reads back as 0."""
    channel = setup["channel_id"]
    raw = as_admin_post(mattermost, channel, "it06 unmarked admin post")
    assert "crew_proto" not in (raw.get("props") or {})
    admin_name = admin(mattermost, "GET", "/users/me").json()["username"]

    async def scenario() -> None:
        d = Daemon(mattermost, setup, tmp_path)
        await d.register("proto-reader")
        try:
            got = await d.backend.read("proto-reader", channel, limit=20)
        finally:
            await d.backend.aclose()
        [msg] = [m for m in got if m.post_id == raw["id"]]
        assert msg.proto == 0
        assert msg.text == "it06 unmarked admin post"
        assert msg.author_id == setup["admin_id"]
        assert msg.author_name == admin_name

    asyncio.run(scenario())


# -- Discord -----------------------------------------------------------------------------------
def test_it06_discord_send_writes_proto_suffix(tmp_path: Path, sock_dir: Path) -> None:  # noqa: F811
    """IT-06, Discord written (R-14): the stored content ends with ` ·p<PROTO>`, and reads
    back with `proto == PROTO`."""
    discord = FakeDiscord()
    chan = discord.add_channel("agent-ops")
    sock = sock_dir / "crewchat.sock"
    host = Host(tmp_path, discord)

    async def scenario() -> None:
        async with host.serving(sock):
            shim, _ = make_shim(make_repo(tmp_path, "proto-dc-w"), sock, tmp_path)
            await shim.start()
            sent = await shim.chat_send(chan, "it06 discord marked", backend="discord")
            assert sent["backend"] == "discord"
            stored = discord.messages[chan][-1]
            assert stored["id"] == sent["post_id"]
            assert stored["content"].endswith(f" ·p{PROTO}")
            got = await shim.chat_read(chan, backend="discord")
            [msg] = [m for m in got["messages"] if m["post_id"] == sent["post_id"]]
            assert msg["proto"] == PROTO

    asyncio.run(scenario())


def test_it06_discord_unmarked_messages_read_as_proto_0(
    tmp_path: Path, sock_dir: Path  # noqa: F811
) -> None:
    """IT-06, Discord proto 0 (R-14): no ` ·p<N>` suffix means `proto` 0, and no error,
    whether the message has today's signature or none at all."""
    discord = FakeDiscord()
    chan = discord.add_channel("agent-ops")
    human = {"id": HUMAN, "username": "bj", "bot": False}
    signed = discord.add_message(chan, "it06 today's signature\n\n— **kessel** 🛰️ (oaw)")
    bare = discord.add_message(chan, "it06 no signature at all", author=human)
    for raw in (signed, bare):
        assert "·p" not in raw["content"]
    sock = sock_dir / "crewchat.sock"
    host = Host(tmp_path, discord)

    async def scenario() -> None:
        async with host.serving(sock):
            shim, _ = make_shim(make_repo(tmp_path, "proto-dc-r"), sock, tmp_path)
            await shim.start()
            got = await shim.chat_read(chan, backend="discord")
            by_id = {m["post_id"]: m for m in got["messages"]}
            assert by_id[signed["id"]]["proto"] == 0
            assert by_id[signed["id"]]["author_name"] == "kessel"  # the signature still parses
            assert by_id[signed["id"]]["text"] == signed["content"]
            assert by_id[bare["id"]]["proto"] == 0
            assert by_id[bare["id"]]["author_name"] == "bj"
            assert by_id[bare["id"]]["text"] == "it06 no signature at all"

    asyncio.run(scenario())
