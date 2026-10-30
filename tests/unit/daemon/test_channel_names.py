"""#145: Mattermost channel names in `send`, `read`, `read_one` and `resolve`."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from crewchat.adapters.base import NotFound
from crewchat.adapters.mattermost import CHANNEL_CACHE_S, match_channel
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.server import DaemonServer, build_handlers
from crewchat.threads import MyThreads
from mcp_server_mattermost.exceptions import NotFoundError

from tests.unit.daemon.test_mattermost import Tokens, backend

DEV = "babel"
TEAM = "t" * 26
PRECHECK = "p" * 26
PRIVATE = "q" * 26
ROLL = "r" * 26


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def world(mm: Any, *teams: str) -> tuple[Any, Clock, Tokens]:
    """One agent on one team holding `precheck` and a display-named `Roll Call`."""
    tokens = Tokens(mm)
    tokens.add(DEV)
    for team in teams or (TEAM,):
        mm.teams[team] = [
            {"id": PRECHECK, "type": "O", "name": "precheck", "display_name": "precheck"},
            {"id": ROLL, "type": "O", "name": "roll-call-2", "display_name": "Roll Call"},
        ]
    clock = Clock()
    return backend(mm, tokens, monotonic=clock), clock, tokens


def lookups(mm: Any) -> list[str]:
    return [p for (_m, p, _t) in mm.requests if "/teams" in p]


def test_channel_id_passes_an_id_through(mm: Any, run: Any) -> None:
    b, _, _ = world(mm)
    run(b.read(DEV, PRECHECK))
    assert lookups(mm) == []


def test_channel_id_resolves_url_name_and_hash_name(mm: Any, run: Any) -> None:
    b, clock, _ = world(mm)
    sent = run(b.send(DEV, "precheck", "one"))
    assert mm.posts[sent.post_id]["channel_id"] == PRECHECK
    clock.now += CHANNEL_CACHE_S + 1  # past the cache: `#precheck` looks it up again
    sent = run(b.send(DEV, "#precheck", "two"))
    assert mm.posts[sent.post_id]["channel_id"] == PRECHECK
    assert lookups(mm).count(f"/teams/{TEAM}/channels/name/precheck") == 2
    assert [m.text for m in run(b.read(DEV, "precheck"))] == ["one", "two"]


def test_channel_id_falls_back_to_display_name(mm: Any, run: Any) -> None:
    b, _, _ = world(mm)
    sent = run(b.send(DEV, "Roll Call", "hi"))  # not a URL name: never sent to `/name/`
    assert mm.posts[sent.post_id]["channel_id"] == ROLL
    sent = run(b.send(DEV, "#roll call", "hi"))  # casefolded
    assert mm.posts[sent.post_id]["channel_id"] == ROLL
    assert not any("/name/Roll" in p for p in lookups(mm))


def test_channel_id_ambiguous_display_name_is_bad_request(mm: Any, run: Any) -> None:
    b, _, _ = world(mm)
    mm.teams[TEAM].append({"id": "s" * 26, "type": "P", "name": "rc", "display_name": "roll call"})
    before = len(mm.posts)
    with pytest.raises(ValueError, match=r"matches 2 channels: 'Roll Call' \(roll-call-2, r+\)"):
        run(b.send(DEV, "Roll Call", "hi"))
    assert len(mm.posts) == before


def test_channel_id_unknown_is_not_found(mm: Any, run: Any) -> None:
    """Also a private channel the agent isn't in: the server shows it no such channel."""
    b, _, _ = world(mm)
    before = len(mm.posts)
    for name in ("nowhere", "Private Room"):
        with pytest.raises(NotFound, match="no channel has the URL name or display name"):
            run(b.send(DEV, name, "hi"))
    assert len(mm.posts) == before
    # A DM or group channel never matches a display name.
    mm.teams[TEAM].append({"id": PRIVATE, "type": "D", "name": "x__y", "display_name": "bj"})
    with pytest.raises(NotFound):
        run(b.read(DEV, "bj"))


def test_channel_id_caches_and_evicts_on_404(mm: Any, run: Any) -> None:
    b, _, _ = world(mm)
    run(b.read(DEV, "precheck"))
    first = len(lookups(mm))
    run(b.read(DEV, "precheck"))
    assert len(lookups(mm)) == first  # cached
    # The channel is gone: its ID 404s, the entry is dropped, and the next call resolves again.
    mm.errors[f"/channels/{PRECHECK}/posts"] = 1
    mm.error_status[f"/channels/{PRECHECK}/posts"] = 404
    with pytest.raises(NotFoundError):
        run(b.read(DEV, "precheck"))
    run(b.read(DEV, "precheck"))
    assert len(lookups(mm)) > first


def test_cache_is_per_agent(mm: Any, run: Any) -> None:
    """One agent's lookup never answers another's: each resolves as itself."""
    b, _, tokens = world(mm)
    tokens.add("kessel")
    run(b.read(DEV, "precheck"))
    n = len(lookups(mm))
    run(b.read("kessel", "precheck"))
    assert len(lookups(mm)) > n


def test_team_choice_single_several_and_none(mm: Any, run: Any) -> None:
    b, _, _ = world(mm, TEAM, "u" * 26)
    with pytest.raises(ValueError, match=r"on 2 teams on .*; pass the channel ID"):
        run(b.read(DEV, "precheck"))
    run(b.read(DEV, PRECHECK))  # an ID still works
    mm.teams.clear()
    with pytest.raises(NotFound, match="on no team"):
        run(b.read(DEV, "precheck"))


@pytest.mark.parametrize("bad", ["", "#", " ", "a\n", "a\x00b", "a\x7f", "x" * 65])
def test_malformed_channel_names_send_nothing(mm: Any, run: Any, bad: str) -> None:
    b, _, _ = world(mm)
    before = list(mm.requests)
    for call in (
        lambda: b.read(DEV, bad),
        lambda: b.send(DEV, bad, "x"),
        lambda: b.read_one(DEV, bad, "p" * 26) if bad else b.read(DEV, bad),
        lambda: b.resolve(DEV, bad),
    ):
        with pytest.raises(ValueError):
            run(call())
    assert [r for r in mm.requests if r not in before] == []


@pytest.mark.parametrize(
    "odd", ["..", "#..", "a/b", "../users/me", "a?x=1", "a%2Fb", "a\\b", "A" * 26]
)
def test_unslug_names_never_reach_a_path(mm: Any, run: Any, odd: str) -> None:
    """Only a URL-name-shaped name is put in a REST path. Any other is compared with display
    names only, so it reaches no path built from it, and an unmatched one is `not_found`."""
    b, _, _ = world(mm)
    before = list(mm.requests)
    with pytest.raises(NotFound):
        run(b.send(DEV, odd, "x"))
    new = [p for (_m, p, _t) in mm.requests if (_m, p, _t) not in before]
    assert all("/name/" not in p for p in new), new
    listings = {"/users/me/teams", f"/users/me/teams/{TEAM}/channels"}
    assert {p for p in new if "/teams" in p} <= listings


def test_display_name_with_path_characters(mm: Any, run: Any) -> None:
    b, _, _ = world(mm)
    qa = "s" * 26
    mm.teams[TEAM].append({"id": qa, "type": "O", "name": "qa", "display_name": "Q&A / Support"})
    for name in ("Q&A / Support", "#q&a / support"):
        sent = run(b.send(DEV, name, "hi"))
        assert mm.posts[sent.post_id]["channel_id"] == qa


def test_cache_keeps_url_name_and_display_name_apart(mm: Any, run: Any) -> None:
    """`standup` is A's URL name and `Standup` is B's display name: two channels, in either
    order, while both are cached."""
    b, _, _ = world(mm)
    a, b_id = "a" * 26, "b" * 26
    mm.teams[TEAM] += [
        {"id": a, "type": "O", "name": "standup", "display_name": "Morning"},
        {"id": b_id, "type": "O", "name": "daily", "display_name": "Standup"},
    ]
    assert mm.posts[run(b.send(DEV, "Standup", "1")).post_id]["channel_id"] == b_id
    assert mm.posts[run(b.send(DEV, "standup", "2")).post_id]["channel_id"] == a
    assert mm.posts[run(b.send(DEV, "Standup", "3")).post_id]["channel_id"] == b_id


def test_read_one_drops_a_mapping_whose_channel_lacks_the_post(mm: Any, run: Any) -> None:
    b, _, _ = world(mm)
    elsewhere = mm.add_post(mm.add_user("bj", is_bot=False), ROLL, "in roll call")
    run(b.read(DEV, "precheck"))
    n = len(lookups(mm))
    with pytest.raises(NotFound):
        run(b.read_one(DEV, "precheck", elsewhere["id"]))
    run(b.read(DEV, "precheck"))
    assert len(lookups(mm)) > n  # resolved again


def test_resolve_channel_on_mattermost(mm: Any, run: Any, daemon: Any) -> None:
    """`resolve(type=channel)` answers the ID with no guild; `user` stays unsupported."""
    b, _, _ = world(mm)
    run(daemon.registry.register(DEV, None, "s1"))
    handlers = build_handlers(daemon.config, daemon.registry)
    handlers.update(build_chat_handlers(daemon.registry, {b.name: b}, MyThreads(), AgentContext()))
    server = DaemonServer(handlers)

    def call(**args: Any) -> dict[str, Any]:
        frame = {"proto": 1, "id": "x", "op": "resolve", "args": {"dev_name": DEV, **args}}
        return run(server.handle_line(json.dumps(frame).encode()))

    assert call(backend=b.name, name="#precheck")["result"] == {
        "backend": b.name,
        "id": PRECHECK,
        "name": "precheck",
        "type": "channel",
    }
    unsupported = call(backend=b.name, name="bj", type="user")
    assert unsupported["ok"] is False and unsupported["error"].startswith("unsupported")
    missing = call(backend=b.name, name="nowhere")
    assert missing["ok"] is False and missing["error"].startswith("not_found")
    with_guild = call(backend=b.name, name="precheck", guild_id="1" * 18)
    assert with_guild["ok"] is False and with_guild["error"].startswith("bad_request")


# -- the broker's copy and the daemon's agree ---------------------------------------------------
CASES: list[tuple[str, list[dict[str, Any]], str | None]] = [
    ("precheck", [{"id": "A", "name": "precheck", "display_name": "x", "type": "O"}], "A"),
    ("Roll Call", [{"id": "B", "name": "rc", "display_name": "roll call", "type": "P"}], "B"),
    ("nope", [{"id": "C", "name": "other", "display_name": "Other", "type": "O"}], None),
    (
        "Dup",
        [
            {"id": "D", "name": "d1", "display_name": "dup", "type": "O"},
            {"id": "E", "name": "d2", "display_name": "DUP", "type": "O"},
        ],
        "ambiguous",
    ),
    ("dm", [{"id": "F", "name": "x__y", "display_name": "dm", "type": "D"}], None),
]


@pytest.mark.parametrize(("name", "channels", "want"), CASES)
def test_daemon_and_broker_match_agree(
    name: str, channels: list[dict[str, Any]], want: str | None
) -> None:
    """`match_channel` is a copy of `crewbroker.membership._match`: both give the same
    answer, or both refuse, on the same cases."""
    from crewbroker.mattermost import NotFound as BrokerNotFound
    from crewbroker.membership import Target, Unresolved, _match

    by_name = {c["name"]: c for c in channels}

    def broker_slug() -> dict[str, Any]:
        if name in by_name:
            return by_name[name]
        raise BrokerNotFound("GET", "/channels/name", 404, "no such channel")

    async def daemon_slug() -> dict[str, Any]:
        if name in by_name:
            return by_name[name]
        raise NotFoundError("no such channel")

    async def daemon_listing() -> list[dict[str, Any]]:
        return channels

    def kept(c: dict[str, Any]) -> bool:
        return c.get("type") in {"O", "P"}

    def outcome(fn: Any) -> tuple[str | None, str]:
        try:
            return fn(), ""
        except (Unresolved, ValueError) as exc:
            return ("ambiguous" if "matches" in str(exc) else None), str(exc)
        except NotFound as exc:
            return None, str(exc)

    broker = outcome(lambda: _match(Target("channel", name), broker_slug, [lambda: channels], kept))
    ours = outcome(lambda: asyncio.run(match_channel(name, daemon_slug, daemon_listing)))
    assert broker == ours
    assert broker[0] == want
