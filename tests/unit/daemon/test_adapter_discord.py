"""Story 2.4c: the Discord adapter's parity with today's `disc_*` tools (R-15, D-106).

Channel names, one guild, today's guild ops, the single-message read, attachments and embeds,
and the scream-hole rule: every call goes through scream-hole; while it is unhealthy, reads are
refused and never reach Discord, and writes alone fall back. The fakes are
`tests/fakes/screamhole.py`; time is an injected clock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from crewchat.adapters.base import Attachment, BackendUnavailable, Embed, NotFound
from crewchat.adapters.discord import (
    HEALTH_BACKOFF_FIRST_S,
    HEALTH_BACKOFF_MAX_S,
    HEALTH_FIRST_TIMEOUT_S,
    HEALTH_TIMEOUT_S,
    SCREAMHOLE_UNHEALTHY,
    WHERE_MAX,
    DiscordBackend,
    is_snowflake,
)

from tests.fakes.screamhole import BOT_TOKEN, DISCORD_API, GUILD_ID, SCREAMHOLE_URL, FakeDiscord

ME = "hailing-frequencies"
HUMAN = {"id": "500000000000000001", "username": "bj", "global_name": "BJ", "bot": False}
OTHER_GUILD = "900000000000000999"


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def credential(tmp_path: Path) -> Path:
    path = tmp_path / "discord.credential"
    path.write_text(BOT_TOKEN + "\n")
    path.chmod(0o600)
    return path


def backend(discord: FakeDiscord, credential: Path, **kw: Any) -> DiscordBackend:
    b = DiscordBackend(
        SCREAMHOLE_URL,
        credential,
        discord_api=DISCORD_API,
        http_client=discord.http_client(),
        guild_id=GUILD_ID,
        **{
            "health_fail_threshold": 1,
            **kw,
        },  # one failed probe is an outage, unless a test says not
    )
    b.set_identity(ME, avatar="📡", team="oaw")
    return b


# -- channel names (R-15) --------------------------------------------------------------------
def test_channel_name_resolution(credential: Path, run: Any) -> None:
    discord = FakeDiscord()
    ops = discord.add_channel("agent-ops")
    discord.add_channel("agent-ops-voice", type=2)
    discord.add_channel("Lounge", type=2)  # a voice channel is never a name match for a post
    b = backend(discord, credential)

    for name in ("agent-ops", "#agent-ops", "#Agent-Ops"):
        sent = run(b.send(ME, name, f"to {name}"))
        assert sent.channel == ops
        assert discord.messages[ops][-1]["id"] == sent.post_id
    assert run(b.channel_id(ops)) == ops  # an ID is used as is
    assert [m.post_id for m in run(b.read(ME, "#agent-ops"))][-1] == sent.post_id

    # A channel made after the cache was filled is found on a miss.
    later = discord.add_channel("later")
    assert run(b.channel_id("later")) == later

    for unknown in ("nope", "#nope", "lounge"):
        with pytest.raises(ValueError, match="unknown channel") as exc:
            run(b.send(ME, unknown, "x"))
        assert unknown in str(exc.value)
    # Without a configured guild a name can't be resolved, and says so.
    no_guild = DiscordBackend(
        SCREAMHOLE_URL, credential, discord_api=DISCORD_API, http_client=discord.http_client()
    )
    with pytest.raises(ValueError, match="guild_id"):
        run(no_guild.send(ME, "agent-ops", "x"))
    assert discord.direct_reads == []


def test_foreign_guild_refused(credential: Path, run: Any) -> None:
    """One daemon serves one guild: another `guild_id` is refused, with no HTTP call."""
    discord = FakeDiscord()
    discord.add_channel("agent-ops")
    b = backend(discord, credential)
    before = list(discord.requests)
    calls = [
        lambda: b.list_channels(ME, "text", OTHER_GUILD),
        lambda: b.resolve(ME, "agent-ops", "channel", OTHER_GUILD),
        lambda: b.create_channel(ME, "new-one", "text", OTHER_GUILD),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="one daemon serves one guild"):
            run(call())
    assert discord.requests == before  # not even a health check
    # The configured guild, named or not, is fine.
    assert run(b.list_channels(ME, "text", GUILD_ID))
    assert run(b.list_channels(ME))


# -- today's guild ops -----------------------------------------------------------------------
def test_list_resolve_create(credential: Path, run: Any) -> None:
    discord = FakeDiscord()
    general = discord.add_channel("general")
    ops = discord.add_channel("agent-ops")
    voice = discord.add_channel("standup", type=2)
    cat = discord.add_channel("Projects", type=4)
    role = discord.add_role("Crew")
    user = discord.add_member("kessel", global_name="Kessel Run")
    b = backend(discord, credential)

    assert run(b.list_channels(ME)) == [
        {"id": general, "name": "general", "type": "text"},
        {"id": ops, "name": "agent-ops", "type": "text"},
    ]
    assert [c["id"] for c in run(b.list_channels(ME, "voice"))] == [voice]
    assert [c["id"] for c in run(b.list_channels(ME, "category"))] == [cat]
    with pytest.raises(ValueError, match="text, voice or category"):
        run(b.list_channels(ME, "stage"))

    assert run(b.resolve(ME, "#Agent-Ops"))["id"] == ops
    assert run(b.resolve(ME, "standup", "channel"))["id"] == voice
    assert run(b.resolve(ME, "@crew", "role")) == {"id": role, "name": "Crew", "type": "role"}
    assert run(b.resolve(ME, "kessel", "user"))["id"] == user
    assert run(b.resolve(ME, "Kessel Run", "user"))["id"] == user
    with pytest.raises(NotFound):
        run(b.resolve(ME, "nobody", "user"))
    with pytest.raises(NotFound):
        run(b.resolve(ME, "missing"))
    with pytest.raises(ValueError):
        run(b.resolve(ME, "x", "emoji"))

    made = run(b.create_channel(ME, "#New Room"))
    assert made["name"] == "new-room" and discord.channels[made["id"]]["type"] == 0
    assert run(b.channel_id("new-room")) == made["id"]  # the name cache was dropped
    made_voice = run(b.create_channel(ME, "huddle", "voice"))
    assert discord.channels[made_voice["id"]]["type"] == 2

    thread = run(b.create_thread(ME, "#agent-ops", "a thread"))
    assert thread["channel"] == ops
    assert discord.channels[thread["id"]]["type"] == 11
    assert discord.channels[thread["id"]]["parent_id"] == ops
    assert discord.direct_reads == []


def test_read_one_message(credential: Path, run: Any) -> None:
    discord = FakeDiscord()
    ops = discord.add_channel("agent-ops")
    first = discord.add_message(ops, "first", author=HUMAN)
    discord.add_message(ops, "second", author=HUMAN)
    b = backend(discord, credential)
    one = run(b.read_one(ME, "agent-ops", first["id"]))
    assert (one.post_id, one.text, one.channel) == (first["id"], "first", ops)
    with pytest.raises(NotFound):
        run(b.read_one(ME, ops, "123456789012345678"))
    with pytest.raises(ValueError):
        run(b.read_one(ME, ops, "not-a-snowflake"))
    assert any(f"/messages/{first['id']}" in p for p in discord.reads("scream-hole"))
    assert discord.direct_reads == []


# -- attachments and embeds (R-15) -----------------------------------------------------------
def test_send_carries_attachments_and_embed(credential: Path, tmp_path: Path, run: Any) -> None:
    discord = FakeDiscord()
    ops = discord.add_channel("agent-ops")
    b = backend(discord, credential)
    held = tmp_path / "held"
    held.write_bytes(b"\x00report bytes\xff")
    att = Attachment(name="report.bin", path=held, size=held.stat().st_size)
    long_text = " ".join(["word"] * 900)  # three chunks: only the last carries both

    run(b.send(ME, ops, long_text, attachments=[att], embed=Embed("Status", "all: green")))
    msgs = discord.messages[ops]
    assert len(msgs) == 3
    assert [m["embeds"] for m in msgs[:2]] == [[], []]
    assert [m["attachments"] for m in msgs[:2]] == [[], []]
    assert msgs[-1]["embeds"] == [{"title": "Status", "description": "all: green"}]
    assert [a["filename"] for a in msgs[-1]["attachments"]] == ["report.bin"]
    assert discord.files[(msgs[-1]["id"], "report.bin")] == b"\x00report bytes\xff"

    dm = run(b.dm(ME, HUMAN["id"], "psst", embed=Embed("Just", "")))
    assert discord.messages[dm.channel][-1]["embeds"] == [{"title": "Just", "description": ""}]


# -- scream-hole only, and its health (D-106) ------------------------------------------------
def test_discord_reads_only_screamhole(credential: Path, run: Any) -> None:
    clock = Clock()
    discord = FakeDiscord(now=clock)
    ops = discord.add_channel("agent-ops")
    msg = discord.add_message(ops, "hello", author=HUMAN)
    b = backend(discord, credential, now=clock)

    def reads() -> list[Any]:
        return [
            lambda: b.read(ME, ops),
            lambda: b.read(ME, "#agent-ops"),
            lambda: b.read_one(ME, ops, msg["id"]),
            lambda: b.thread(ME, ops),
            lambda: b.list_channels(ME),
            lambda: b.resolve(ME, "agent-ops"),
        ]

    # Healthy: every read and every write targets the scream-hole base, none Discord.
    for read in reads():
        run(read())
    run(b.send(ME, ops, "via scream-hole"))
    assert run(b.health())["state"] == "ok"
    services = {s for s, _m, _p in discord.requests}
    assert services == {"scream-hole"}
    assert ("scream-hole", "POST", f"/api/v10/channels/{ops}/messages") in discord.requests

    # Unhealthy: the next check (after the healthy recheck interval) sees it.
    discord.screamhole_healthy = False
    clock.t += b.healthy_recheck_s
    mark = len(discord.requests)
    for read in reads():
        with pytest.raises(BackendUnavailable, match=SCREAMHOLE_UNHEALTHY):
            run(read())
    # Name resolution that misses the cache is a read too (a cached hit makes no request).
    with pytest.raises(BackendUnavailable):
        run(b.channel_id("not-cached-yet"))
    health = run(b.health())
    assert health["state"] == "degraded" and SCREAMHOLE_UNHEALTHY in health["detail"]
    # A write falls back to the Discord REST API, with the bot token.
    sent = run(b.send(ME, ops, "direct while scream-hole is down"))
    assert discord.messages[ops][-1]["id"] == sent.post_id
    new = discord.requests[mark:]
    assert ("discord", "POST", f"/channels/{ops}/messages") in new
    assert [r for r in new if r[0] == "discord" and r[1] == "GET"] == []
    assert discord.direct_reads == []  # no read ever reached discord.com

    # The re-check backs off, 1 s doubling to the 60 s cap, not once at startup.
    waits = []
    for _ in range(9):
        clock.t = b._health_next
        assert run(b.check_health()) is False
        waits.append(b._health_next - clock.t)
    assert waits == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0]
    assert max(waits) == HEALTH_BACKOFF_MAX_S

    # Recovery: once the next check passes, reads and writes go through scream-hole again.
    discord.screamhole_healthy = True
    with pytest.raises(BackendUnavailable):
        run(b.read(ME, ops))  # not re-checked yet: still refused, still not Discord
    clock.t = b._health_next
    mark = len(discord.requests)
    assert run(b.read(ME, ops))[0].text == "hello"
    run(b.send(ME, ops, "via scream-hole again"))
    assert {s for s, _m, _p in discord.requests[mark:]} == {"scream-hole"}
    assert run(b.health())["state"] == "ok"
    assert discord.direct_reads == []


def test_channel_names_while_unhealthy(credential: Path, run: Any) -> None:
    """While scream-hole is unhealthy a name resolves from the last healthy listing, however
    old, so a name-addressed write still goes direct to Discord (#74). The listing is not
    re-read: no read reaches scream-hole or Discord. With no listing, or a name the listing
    lacks, the error says to use the channel ID."""
    clock = Clock()
    discord = FakeDiscord(now=clock)
    ops = discord.add_channel("agent-ops")
    b = backend(discord, credential, now=clock)
    assert run(b.channel_id("agent-ops")) == ops  # one healthy listing

    discord.screamhole_healthy = False
    clock.t += max(b.channel_refresh_s, b.healthy_recheck_s) * 10  # the listing is stale
    later = discord.add_channel("later")  # made after the listing: not in it
    mark = len(discord.requests)
    sent = run(b.send(ME, "#agent-ops", "by name while degraded"))
    assert sent.channel == ops and discord.messages[ops][-1]["id"] == sent.post_id
    thread = run(b.create_thread(ME, "agent-ops", "a thread while degraded"))
    assert thread["channel"] == ops
    new = discord.requests[mark:]
    assert [r for r in new if r[1] == "GET" and "/health" not in r[2]] == []  # no listing read
    assert ("discord", "POST", f"/channels/{ops}/messages") in new
    with pytest.raises(BackendUnavailable, match="address it by channel ID"):
        run(b.send(ME, "later", "x"))
    run(b.send(ME, later, "by ID still goes"))
    assert discord.messages[later][-1]["content"].startswith("by ID still goes")
    for read in (lambda: b.list_channels(ME), lambda: b.resolve(ME, "agent-ops")):
        with pytest.raises(BackendUnavailable, match=SCREAMHOLE_UNHEALTHY):
            run(read())
    assert "channel names resolve from the last listing" in run(b.health())["detail"]
    assert discord.direct_reads == []

    # Unhealthy from the start: no listing to fall back on.
    cold_discord = FakeDiscord(now=clock)
    cold_discord.screamhole_healthy = False
    cold = backend(cold_discord, credential, now=clock)
    with pytest.raises(BackendUnavailable, match="address it by channel ID"):
        run(cold.send(ME, "agent-ops", "x"))
    assert cold_discord.direct_reads == []


def test_stream_does_not_poll_while_unhealthy(credential: Path, run: Any) -> None:
    import asyncio

    clock = Clock()
    discord = FakeDiscord(now=clock)
    ops = discord.add_channel("agent-ops")
    discord.screamhole_healthy = False
    polls: list[float] = []
    down: list[tuple[str, str, str]] = []

    async def sleep(seconds: float) -> None:
        polls.append(seconds)
        clock.t += seconds
        if len(polls) == 3:
            down.extend(discord.requests)
            discord.screamhole_healthy = True
            discord.add_message(ops, "after recovery", author=HUMAN)
        await asyncio.sleep(0)

    b = backend(discord, credential, now=clock, sleep=sleep)

    async def scenario() -> str:
        gen = b.stream(ME)
        got = await asyncio.wait_for(anext(gen), 2)
        await b.close_agent(ME)
        await gen.aclose()
        return got.text

    assert run(scenario()) == "after recovery"
    # While down: health checks only, never a poll, and nothing to Discord.
    assert down and all(p == "/health" for _s, _m, p in down)
    assert discord.direct_reads == []


# -- caller IDs never steer the bot credential (R-08, #62) ----------------------------------
HOSTILE_IDS = [
    "..",
    "1/../../guilds/9/prune",
    "1?x=/m",
    "1%2F2",
    "123\n",
    "١٢٣",  # Arabic-Indic digits: `\d` matches them, a snowflake doesn't
    "",
    "1" * 21,
    "C",
]


def test_snowflake_strict() -> None:
    assert is_snowflake("123") and is_snowflake("1" * 20)
    for bad in [*HOSTILE_IDS, "1/2"]:
        assert not is_snowflake(bad), bad


def test_discord_hostile_ids_send_nothing(credential: Path, run: Any) -> None:
    """A hostile thread or post ID is a `ValueError` before any request, on every path that
    would put it in a URL: `send(thread)`, `react` (either part), `thread` and `read_one`."""
    discord = FakeDiscord()
    ops = discord.add_channel("agent-ops")
    good = discord.add_message(ops, "a message", author=HUMAN)["id"]
    b = backend(discord, credential)
    before = list(discord.requests)
    for bad in HOSTILE_IDS:
        calls = [
            lambda bad=bad: b.send(ME, ops, "hi", thread=bad),
            lambda bad=bad: b.react(ME, bad, "👍"),
            lambda bad=bad: b.react(ME, f"{ops}/{bad}", "👍"),
            lambda bad=bad: b.react(ME, f"{bad}/{good}", "👍"),
            lambda bad=bad: b.react(ME, f"{ops}/{good}/{good}", "👍"),
            lambda bad=bad: b.thread(ME, bad),
            lambda bad=bad: b.read_one(ME, ops, bad),
        ]
        for call in calls:
            with pytest.raises(ValueError):
                run(call())
    for emoji in (".", "..", "...", " .. "):  # a dot segment would walk the reactions path
        with pytest.raises(ValueError):
            run(b.react(ME, f"{ops}/{good}", emoji))
    assert discord.requests == before  # not one request, not even a health check


def test_react_channel_message_form_ok(credential: Path, run: Any) -> None:
    discord = FakeDiscord()
    ops = discord.add_channel("agent-ops")
    msg = discord.add_message(ops, "react to me", author=HUMAN)["id"]
    b = backend(discord, credential)
    run(b.react(ME, f"{ops}/{msg}", "👍"))
    puts = [p for s, m, p in discord.requests if m == "PUT"]
    assert len(puts) == 1 and f"/channels/{ops}/messages/{msg}/reactions/" in puts[0]


def test_where_is_bounded_lru(credential: Path) -> None:
    b = backend(FakeDiscord(), credential)
    for n in range(WHERE_MAX + 1):
        b.message({"id": str(1_000_000 + n), "channel_id": "42", "content": ""})
    assert len(b._where) == WHERE_MAX
    assert "1000000" not in b._where  # the least recent, evicted
    assert str(1_000_000 + WHERE_MAX) in b._where


# -- hysteresis (#202) -----------------------------------------------------------------------
def test_one_failed_probe_is_not_an_outage(credential: Path, run: Any) -> None:
    """#202: scream-hole's /health is trivial, so one failed probe means a stalled host or
    network. Health flips only after `health_fail_threshold` failures in a row, and a probe
    that passes in between starts the count again."""
    clock = Clock()
    discord = FakeDiscord(now=clock)
    b = backend(discord, credential, now=clock, health_fail_threshold=3)
    assert run(b.check_health()) is True

    discord.screamhole_healthy = False
    for want in (True, True, False):  # fails 1 and 2 are absorbed; the 3rd is an outage
        clock.t = b._health_next
        assert run(b.check_health()) is want
    assert run(b.health())["state"] == "degraded"

    discord.screamhole_healthy = True  # recovery, then a fresh run of failures
    clock.t = b._health_next
    assert run(b.check_health()) is True
    discord.screamhole_healthy = False
    for _ in range(2):
        clock.t = b._health_next
        assert run(b.check_health()) is True
    discord.screamhole_healthy = True
    clock.t = b._health_next
    assert run(b.check_health()) is True  # a pass resets the count ...
    discord.screamhole_healthy = False
    for _ in range(2):
        clock.t = b._health_next
        assert run(b.check_health()) is True  # ... so two more failures are still absorbed


def test_a_blip_is_reprobed_soon_not_a_minute_later(credential: Path, run: Any) -> None:
    clock = Clock()
    discord = FakeDiscord(now=clock)
    b = backend(discord, credential, now=clock, health_fail_threshold=3)
    assert run(b.check_health()) is True
    discord.screamhole_healthy = False
    clock.t = b._health_next  # the healthy recheck (60 s) comes due
    assert run(b.check_health()) is True
    assert b._health_next - clock.t == HEALTH_BACKOFF_FIRST_S  # then 1 s, not 60


def test_failing_for_the_window_is_an_outage(credential: Path, run: Any) -> None:
    """#202: the threshold or the window, whichever comes first."""
    clock = Clock()
    discord = FakeDiscord(now=clock)
    b = backend(
        discord, credential, now=clock, health_fail_threshold=100, health_fail_window_s=10.0
    )
    assert run(b.check_health()) is True
    discord.screamhole_healthy = False
    clock.t = b._health_next
    assert run(b.check_health()) is True  # the window opens here
    clock.t += 9.0
    assert run(b.check_health(force=True)) is True
    clock.t += 1.0
    assert run(b.check_health(force=True)) is False


def test_failures_far_apart_are_not_a_streak(credential: Path, run: Any) -> None:
    """#202: a failure long after the last one starts a new streak: two failures minutes
    apart must not read as an outage on the second."""
    clock = Clock()
    discord = FakeDiscord(now=clock)
    b = backend(
        discord, credential, now=clock, health_fail_threshold=100, health_fail_window_s=10.0
    )
    assert run(b.check_health()) is True
    discord.screamhole_healthy = False
    clock.t = b._health_next
    assert run(b.check_health()) is True  # failure 1
    clock.t += 300.0  # nobody asked for minutes
    assert run(b.check_health(force=True)) is True  # a new streak, not the window expiring
    clock.t += 9.0
    assert run(b.check_health(force=True)) is True
    clock.t += 1.0
    assert run(b.check_health(force=True)) is False  # 10 s of failing since the new streak


def test_the_first_probe_waits_longer(credential: Path, run: Any) -> None:
    clock = Clock()
    discord = FakeDiscord(now=clock)
    b = backend(discord, credential, now=clock)
    client = b._client()
    real_get = client.get
    seen: list[Any] = []

    async def spy(url: str, **kw: Any) -> Any:
        seen.append(kw.get("timeout"))
        return await real_get(url, **kw)

    client.get = spy  # type: ignore[method-assign]
    run(b.check_health())
    run(b.check_health(force=True))
    assert seen == [HEALTH_FIRST_TIMEOUT_S, HEALTH_TIMEOUT_S]
