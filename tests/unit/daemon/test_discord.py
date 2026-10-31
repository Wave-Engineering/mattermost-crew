"""Story 2.2: the Discord adapter. Reads through the scream-hole fake, writes through the
Discord REST fake (`tests/fakes/screamhole.py`). R-14, R-15, D-013, D-059.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from crewchat.adapters.base import ChatBackend, InboundMessage, StreamCursor
from crewchat.adapters.discord import (
    MAX_CHUNK,
    CredentialError,
    DiscordBackend,
    Identity,
    format_signature,
    parse_signature,
    snowflake_at,
    snowflake_time,
    split_message,
)

from tests.fakes.screamhole import BOT_TOKEN, DISCORD_API, GUILD_ID, SCREAMHOLE_URL, FakeDiscord

HUMAN = {"id": "500000000000000001", "username": "bj", "global_name": "BJ", "bot": False}
ME = "hailing-frequencies"
# Channel IDs, as snowflakes: since Story 2.4c anything else is a channel name to resolve.
C1 = "700000000000000901"
C2 = "700000000000000902"
NEVER_POLLED = "700000000000000904"


@pytest.fixture
def discord() -> FakeDiscord:
    return FakeDiscord()


@pytest.fixture
def credential(tmp_path: Path) -> Path:
    path = tmp_path / "discord.credential"
    path.write_text(BOT_TOKEN + "\n")
    path.chmod(0o600)
    return path


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        await asyncio.sleep(0)


def backend(discord: FakeDiscord, credential: Path, **kw: Any) -> DiscordBackend:
    b = DiscordBackend(
        SCREAMHOLE_URL,
        credential,
        discord_api=DISCORD_API,
        http_client=discord.http_client(),
        **kw,
    )
    b.set_identity(ME, avatar="📡", team="oaw")
    return b


async def take(gen: AsyncIterator[InboundMessage], n: int) -> list[InboundMessage]:
    out = []
    for _ in range(n):
        out.append(await asyncio.wait_for(anext(gen), 2))
    return out


def test_backend_satisfies_the_contract(discord: FakeDiscord, credential: Path) -> None:
    b: ChatBackend = backend(discord, credential)
    assert b is not None


# -- the signature (R-14, D-059) -------------------------------------------------------------
def test_signature_with_p1_suffix(discord: FakeDiscord, credential: Path, run: Any) -> None:
    b = backend(discord, credential)
    msg = run(b.send(ME, C1, "status update"))
    stored = discord.messages[C1][-1]["content"]
    assert stored == "status update\n\n— **hailing-frequencies** 📡 (oaw) ·p1"
    assert msg.proto == 1 and msg.author_name == ME and msg.backend == "discord"

    # Parse accepts the signature with or without the suffix.
    today = parse_signature("status update\n\n— **hailing-frequencies** 📡 (oaw)")
    assert today is not None
    assert (today.name, today.avatar, today.team, today.proto) == (ME, "📡", "oaw", 0)
    marked = parse_signature(stored)
    assert marked is not None
    assert (marked.name, marked.avatar, marked.team, marked.proto) == (ME, "📡", "oaw", 1)
    no_avatar = parse_signature("hi\n\n— **hailing-frequencies** (oaw) ·p1")
    assert no_avatar is not None and (no_avatar.avatar, no_avatar.proto) == ("", 1)

    # Inbound: no suffix gives proto 0, the suffix gives 1.
    discord.add_message(C1, "old\n\n— **kessel** 🚀 (oaw)")
    discord.add_message(C1, "new\n\n— **kessel** 🚀 (oaw) ·p1")
    discord.add_message(C1, "a human, unsigned", author=HUMAN)
    msgs = run(b.read(ME, C1))
    assert [(m.author_name, m.proto) for m in msgs[-3:]] == [
        ("kessel", 0),
        ("kessel", 1),
        ("BJ", 0),
    ]


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        ("no signature here", None),
        ("quoted `— **morpheus**` in code", None),
        ("```\n— **morpheus** (oaw) ·p1\n```", None),
        ("— **morpheus**", ("morpheus", "", "", 0)),
        ("— **morpheus** ·p1", ("morpheus", "", "", 1)),
        ("— **morpheus** 🕶 (oaw) ·p2", ("morpheus", "🕶", "oaw", 2)),
        ("— **a** (x)\nmore\n— **b** (y) ·p1", ("b", "", "y", 1)),
        ("— **morpheus** (oaw)·p1", ("morpheus", "", "oaw", 0)),
    ],
)
def test_parse_signature_forms(text: str, expect: tuple[str, str, str, int] | None) -> None:
    sig = parse_signature(text)
    got = None if sig is None else (sig.name, sig.avatar, sig.team, sig.proto)
    assert got == expect


def test_format_signature_leaves_out_empty_parts() -> None:
    assert format_signature("x") == "— **x** ·p1"
    assert format_signature("x", Identity(team="oaw")) == "— **x** (oaw) ·p1"
    assert format_signature("x", Identity(avatar="🦊", team="oaw")) == "— **x** 🦊 (oaw) ·p1"


def test_send_replaces_an_existing_own_signature(
    discord: FakeDiscord, credential: Path, run: Any
) -> None:
    b = backend(discord, credential)
    run(b.send(ME, C1, "hello\n\n— **hailing-frequencies** 📡 (oaw)"))
    assert (
        discord.messages[C1][-1]["content"] == "hello\n\n— **hailing-frequencies** 📡 (oaw) ·p1"
    )


def test_long_send_signs_every_chunk(discord: FakeDiscord, credential: Path, run: Any) -> None:
    b = backend(discord, credential)
    text = " ".join(["word"] * 900)  # about 4,500 characters
    run(b.send(ME, C1, text))
    chunks = [m["content"] for m in discord.messages[C1]]
    assert len(chunks) == 3
    assert all(len(c) <= MAX_CHUNK for c in chunks)
    assert all(c.endswith("— **hailing-frequencies** 📡 (oaw) ·p1") for c in chunks)


def test_split_message() -> None:
    assert split_message("", 10) == [""]
    assert split_message("a b c", 10) == ["a b c"]
    assert split_message("aaaa bbbb cccc", 9) == ["aaaa bbbb", "cccc"]
    assert split_message("x" * 12, 5) == ["xxxxx", "xxxxx", "xx"]
    with pytest.raises(ValueError):
        split_message("x", 0)


# -- reads through scream-hole (D-013) -------------------------------------------------------
def test_read_via_screamhole_fake(credential: Path, run: Any) -> None:
    clock = [1_800_000_000.0]
    discord = FakeDiscord(now=lambda: clock[0])
    b = backend(discord, credential)
    discord.add_message(C1, "one", author=HUMAN)
    clock[0] += 10
    discord.add_message(C1, "two", author=HUMAN)
    discord.add_message(C2, "elsewhere", author=HUMAN)

    msgs = run(b.read(ME, C1))
    assert [m.text for m in msgs] == ["one", "two"]  # oldest first
    assert all(m.channel == C1 and m.is_bot is False for m in msgs)
    assert msgs[1].ts == clock[0]
    assert [m.text for m in run(b.read(ME, C1, limit=1))] == ["two"]
    assert [m.text for m in run(b.read(ME, C1, since=clock[0] - 5))] == ["two"]
    assert run(b.read(ME, NEVER_POLLED)) == []  # scream-hole fails open (#16)

    # Every read went to scream-hole, without `limit` (watcher #92); none to Discord.
    reads = [r for r in discord.requests if r[1] == "GET"]
    assert reads and all(service == "scream-hole" for service, _, _ in reads)
    assert all("limit=" not in path for _, _, path in reads)
    assert discord.direct_reads == []


def test_reads_never_carry_the_bot_token(discord: FakeDiscord, credential: Path, run: Any) -> None:
    seen: list[httpx.Request] = []

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return discord.handle(request)

    b = DiscordBackend(
        SCREAMHOLE_URL,
        credential,
        discord_api=DISCORD_API,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(spy)),
    )
    run(b.read(ME, C1))
    assert seen and all("Authorization" not in r.headers for r in seen)


def test_writes_refuse_a_get() -> None:
    b = DiscordBackend(SCREAMHOLE_URL, "/nonexistent")
    with pytest.raises(ValueError, match="scream-hole"):
        asyncio.run(b._discord_write("GET", "/channels/1/messages"))


def test_thread_dm_and_react(discord: FakeDiscord, credential: Path, run: Any) -> None:
    b = backend(discord, credential)
    thread_id = discord.add_channel("a-thread", type=11)
    discord.add_message(thread_id, "starter", author=HUMAN)
    reply = run(b.send(ME, "parent", "in the thread", thread=thread_id))
    assert reply.channel == thread_id and reply.root_id == thread_id
    thread = run(b.thread(ME, thread_id))
    assert [m.text.split("\n")[0] for m in thread] == ["starter", "in the thread"]
    assert all(m.root_id == thread_id for m in thread)

    dm = run(b.dm(ME, "<@500000000000000001>", "psst"))
    assert dm.channel == discord.dm_channels["500000000000000001"]
    assert discord.messages[dm.channel][-1]["content"].endswith("·p1")
    again = run(b.dm(ME, "@500000000000000001", "again"))
    assert again.channel == dm.channel
    with pytest.raises(ValueError):
        run(b.dm(ME, "bj", "a name, not an ID"))

    run(b.react(ME, reply.post_id, ":thumbsup:"))
    run(b.react(ME, f"{thread_id}/{thread[0].post_id}", "👍"))
    assert discord.reactions == [
        (thread_id, reply.post_id, "thumbsup"),
        (thread_id, thread[0].post_id, "👍"),
    ]
    with pytest.raises(KeyError):
        run(b.react(ME, "123456", "👍"))
    assert discord.direct_reads == []


def test_reply_and_mentions_are_mapped(discord: FakeDiscord, credential: Path, run: Any) -> None:
    b = backend(discord, credential)
    root = discord.add_message(C1, "root", author=HUMAN)
    discord.add_message(
        C1, "a reply @hailing-frequencies", author=HUMAN, reply_to=root["id"], mentions=[HUMAN]
    )
    msgs = run(b.read(ME, C1))
    assert msgs[-1].root_id == root["id"] and msgs[0].root_id == ""
    assert msgs[-1].mentions == [HUMAN["id"]]
    assert msgs[-1].author_id == HUMAN["id"] and msgs[-1].ts > 0


def test_429_waits_and_retries(
    discord: FakeDiscord, credential: Path, run: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The retry says so: with httpx silenced (#180), nothing else would."""
    sleeps = Sleeps()
    b = backend(discord, credential, sleep=sleeps)
    discord.rate_limit_next = 1
    with caplog.at_level("WARNING", logger="crewchat.adapters.discord"):
        msg = run(b.send(ME, C1, "hello"))
    assert msg.text.startswith("hello") and sleeps.waits == [0.25]
    assert "429 on POST" in caplog.text and "retrying in 0.2 s" in caplog.text


# -- the credential --------------------------------------------------------------------------
def test_credential_must_be_0600(discord: FakeDiscord, credential: Path, run: Any) -> None:
    b = backend(discord, credential)
    credential.chmod(0o640)
    with pytest.raises(CredentialError, match="0640"):
        run(b.send(ME, C1, "hello"))
    credential.chmod(0o600)
    credential.write_text("  \n")
    with pytest.raises(CredentialError, match="empty"):
        run(b.send(ME, C1, "hello"))
    with pytest.raises(CredentialError, match="no Discord credential"):
        run(backend(discord, credential.parent / "missing").send(ME, C1, "x"))
    with pytest.raises(CredentialError, match="regular file"):
        run(backend(discord, credential.parent).send(ME, C1, "x"))
    assert discord.messages.get(C1, []) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can chown to anyone")
def test_credential_owner_is_checked(
    discord: FakeDiscord, credential: Path, run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: credential.stat().st_uid + 1)
    with pytest.raises(CredentialError, match="not owned"):
        run(backend(discord, credential).send(ME, C1, "x"))


# -- the stream and the echo filter (R-15) ---------------------------------------------------
def test_discord_echo_by_signature(credential: Path, run: Any) -> None:
    clock = [1_800_000_000.0]
    discord = FakeDiscord(now=lambda: clock[0])
    sleeps = Sleeps()
    c1 = discord.add_channel("general")
    discord.add_channel("voice", type=2)
    discord.add_message(c1, "from before the stream", author=HUMAN)
    clock[0] += 1
    b = backend(discord, credential, guild_id=GUILD_ID, sleep=sleeps, now=lambda: clock[0])

    async def scenario() -> list[InboundMessage]:
        gen = b.stream(ME)
        first = asyncio.ensure_future(take(gen, 2))
        await asyncio.sleep(0)
        clock[0] += 1
        # Our own signed post (with or without ·p1, any case) is dropped; others arrive.
        await b.send(ME, c1, "our own post")
        discord.add_message(c1, "old form\n\n— **Hailing-Frequencies** 📡 (oaw)")
        discord.add_message(c1, "from kessel\n\n— **kessel** 🚀 (oaw) ·p1")
        discord.add_message(c1, "quoting `— **hailing-frequencies**`", author=HUMAN)
        got = await first
        await b.close_agent(ME)
        await gen.aclose()
        return got

    got = run(scenario())
    assert [m.text.split("\n")[0] for m in got] == [
        "from kessel",
        "quoting `— **hailing-frequencies**`",
    ]
    assert got[0].author_name == "kessel" and got[0].proto == 1
    assert sleeps.waits and all(w == b.poll_interval_s for w in sleeps.waits)
    assert discord.direct_reads == []
    # Only text channels are polled.
    polled = {p for s, m, p in discord.requests if s == "scream-hole" and "/messages" in p}
    assert polled and all(c1 in p for p in polled)


def test_stream_skips_a_failing_channel_and_static_channels(
    discord: FakeDiscord, credential: Path, run: Any
) -> None:
    sleeps = Sleeps()
    b = backend(discord, credential, channels=[C1, "bad"], sleep=sleeps)
    real = discord.handle_screamhole

    def flaky(request: httpx.Request) -> httpx.Response:
        if "/channels/bad/" in request.url.path:
            return httpx.Response(500, json={})
        return real(request)

    discord.handle_screamhole = flaky  # type: ignore[method-assign]

    async def scenario() -> list[InboundMessage]:
        gen = b.stream(ME)
        task = asyncio.ensure_future(take(gen, 1))
        await asyncio.sleep(0)
        discord.add_message(C1, "hello", author=HUMAN)
        got = await task
        await b.aclose()
        return got

    assert [m.text for m in run(scenario())] == ["hello"]


def test_channel_list_is_cached_and_refreshed(
    discord: FakeDiscord, credential: Path, run: Any
) -> None:
    clock = [1_800_000_000.0]
    b = backend(discord, credential, guild_id=GUILD_ID, now=lambda: clock[0])
    discord.add_channel("one")
    assert len(run(b.poll_channels())) == 1
    discord.add_channel("two")
    assert len(run(b.poll_channels())) == 1  # cached
    clock[0] += b.channel_refresh_s
    assert len(run(b.poll_channels())) == 2
    # A failing list keeps the last good one.
    b.guild_id = "999"
    clock[0] += b.channel_refresh_s
    assert len(run(b.poll_channels())) == 2
    assert run(backend(discord, credential).poll_channels()) == []


def test_token_hooks_are_harmless(discord: FakeDiscord, credential: Path, run: Any) -> None:
    b = backend(discord, credential)
    run(b.on_token_rotated(ME))
    run(b.close_agent("nobody"))
    owned = DiscordBackend(SCREAMHOLE_URL, credential)
    owned._client()
    run(owned.aclose())
    assert owned._http is None


def test_snowflakes_round_trip() -> None:
    t = 1_800_000_000.5
    assert abs(snowflake_time(snowflake_at(t)) - t) < 0.001
    assert snowflake_at(0) == "0" and snowflake_time("x") == 0.0


def test_fake_serves_screamhole_over_http(discord: FakeDiscord) -> None:
    """The integration form of the fake: a real port, the same read API."""
    c1 = discord.add_channel("general")
    discord.add_message(c1, "hello", author=HUMAN)
    with discord.serve() as url:
        ok = httpx.get(f"{url}/api/v10/channels/{c1}/messages", params={"after": "0"})
        bad = httpx.get(f"{url}/api/v10/channels/{c1}/messages")
        health = httpx.get(f"{url}/health")
    assert ok.status_code == 200 and [m["content"] for m in ok.json()] == ["hello"]
    assert bad.status_code == 400 and health.json()["status"] == "ok"


def test_backend_reads_from_a_served_fake(discord: FakeDiscord, credential: Path, run: Any) -> None:
    c1 = discord.add_channel("general")
    discord.add_message(c1, "over the wire", author=HUMAN)
    with discord.serve() as url:
        b = DiscordBackend(url, credential)

        async def scenario() -> list[InboundMessage]:
            try:
                return await b.read(ME, c1)
            finally:
                await b.aclose()

        msgs = run(scenario())
    assert [m.text for m in msgs] == ["over the wire"]


# -- Story 3.2: cursor replay (R-28) and status (R-27) ----------------------------------------
def test_stream_resumes_from_cursor_and_reports_status(credential: Path, run: Any) -> None:
    """With a snowflake cursor, every channel is polled from it: the messages after it are
    replayed once, the ones in `cursor.seen` are not; scream-hole down is reported down."""
    clock = [1_800_000_000.0]
    discord = FakeDiscord(now=lambda: clock[0])
    c1 = discord.add_channel("general", channel_id=C1)
    c2 = discord.add_channel("random", channel_id=C2)
    discord.add_message(c1, "before the cursor", author=HUMAN)
    clock[0] += 1
    last = discord.add_message(c1, "the cursor's own", author=HUMAN)
    clock[0] += 1
    seen = discord.add_message(c2, "already handed over", author=HUMAN)
    missed = discord.add_message(c2, "missed", author=HUMAN)
    clock[0] += 1
    b = backend(discord, credential, channels=[C1, C2], sleep=Sleeps(), now=lambda: clock[0])
    cursor = StreamCursor(last["id"], 0.0, frozenset({last["id"], seen["id"]}))
    status: list[tuple[bool, str]] = []

    async def scenario() -> list[InboundMessage]:
        gen = b.stream(ME, cursor=cursor, on_status=lambda up, why: status.append((up, why)))
        got = await take(gen, 1)
        discord.screamhole_healthy = False
        b._health_next = 0.0  # re-check now
        task = asyncio.ensure_future(take(gen, 1))
        while not any(not up for up, _ in status):
            await asyncio.sleep(0)
        discord.screamhole_healthy = True
        b._health_next = 0.0
        discord.add_message(c1, "after recovery", author=HUMAN)
        got += await task
        await b.aclose()
        await gen.aclose()
        return got

    got = run(scenario())
    assert [m.text for m in got] == ["missed", "after recovery"]
    assert got[0].post_id == missed["id"]
    assert status[0] == (True, "")
    down = [s for s in status if not s[0]]
    assert down and all(why == "scream-hole unhealthy" for _, why in down)
    assert status[-1] == (True, "")
