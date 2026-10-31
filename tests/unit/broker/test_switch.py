"""Story 3.1: the social switch and kiosk flag the broker holds (R-20, R-23, R-24, R-34)."""

from __future__ import annotations

import json
import stat
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from crewbroker.config import CrewConfig
from crewbroker.mattermost import MattermostError, NotFound
from crewbroker.service import (
    BROKER_PROP,
    Broker,
    DMListener,
    dm_from_event,
    handle_frame,
)
from crewbroker.switch import (
    REFUSAL,
    SwitchState,
    SwitchStore,
    apply_command,
    effective_mode,
    from_json,
    parse_duration,
    settle,
)

BROKER_ID = "broker-user-id"  # the conftest fake's broker and human partner
HUMAN_ID = "human-user-id"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
OUTSIDER = "outsider-user-id"


@pytest.fixture
def cfg() -> CrewConfig:
    return CrewConfig(
        crew="oaw",
        server_id="dev",
        server_url="http://mm.invalid",
        human_user_ids=[HUMAN_ID],
        broker_user_id=BROKER_ID,
    )


def run(state: SwitchState, cfg: CrewConfig, *texts: str, now: datetime = NOW) -> SwitchState:
    for text in texts:
        state, _ = apply_command(state, HUMAN_ID, text, cfg, now)
    return state


# -- the named tests (Dev Spec Story 3.1) ---------------------------------------------------
def test_default_crew_only_kiosk_on(tmp_path: Path) -> None:
    state = SwitchState()
    assert (state.mode, state.expires_at, state.revert_to, state.kiosk_on) == (
        "crew",
        None,
        "crew",
        True,
    )
    assert effective_mode(state, NOW) == ("crew", False)
    # No switch.json yet: the same defaults.
    assert SwitchStore(tmp_path).load() == SwitchState()


def test_only_partner_can_flip(cfg: CrewConfig) -> None:
    for start in (SwitchState(), SwitchState(mode="trusted", revert_to="trusted")):
        for text in ("social 2h", "trusted", "crew", "kiosk off", "kiosk on"):
            new, reply = apply_command(start, OUTSIDER, text, cfg, NOW)
            assert new is start
            assert reply == REFUSAL
    # The broker's own account and a display-name lookalike are not partners either.
    assert apply_command(SwitchState(), BROKER_ID, "social 1h", cfg, NOW)[1] == REFUSAL
    assert apply_command(SwitchState(), "bj", "social 1h", cfg, NOW)[1] == REFUSAL


def test_social_lapses_and_reverts(cfg: CrewConfig) -> None:
    state = run(SwitchState(), cfg, "trusted", "social 2h")
    assert state.mode == "social" and state.revert_to == "trusted"
    assert state.expires_at == NOW + timedelta(hours=2)
    assert effective_mode(state, NOW + timedelta(hours=1)) == ("social", False)
    assert effective_mode(state, NOW + timedelta(hours=2)) == ("trusted", True)
    assert effective_mode(state, NOW + timedelta(days=9)) == ("trusted", True)


def test_unreadable_revert_falls_back_crew() -> None:
    later = NOW + timedelta(hours=3)
    for bad in ("garbage", "social", ""):
        state = SwitchState(mode="social", expires_at=NOW + timedelta(hours=1), revert_to=bad)
        assert effective_mode(state, later) == ("crew", True)
        assert settle(state, later).mode == "crew"
    # From storage: a revert_to that isn't a string, and social with an unreadable expiry.
    stored = from_json({"mode": "social", "expires_at": "not a time", "revert_to": 7})
    assert effective_mode(stored, NOW) == ("crew", True)


def test_social_extend_keeps_revert(cfg: CrewConfig) -> None:
    state = run(SwitchState(), cfg, "trusted", "social 1h")
    first = state.expires_at
    later = NOW + timedelta(minutes=30)
    state, _ = apply_command(state, HUMAN_ID, "social 2h", cfg, later)
    assert state.mode == "social" and state.revert_to == "trusted"
    assert state.expires_at == later + timedelta(hours=2) != first
    assert effective_mode(state, later + timedelta(hours=2)) == ("trusted", True)


def test_social_reengage_can_shorten(cfg: CrewConfig) -> None:
    """#154: `social <dur>` sets the expiry to `dur` from now, so a shorter one shortens it."""
    state = run(SwitchState(), cfg, "trusted", "social 2h")
    later = NOW + timedelta(minutes=10)
    state, reply = apply_command(state, HUMAN_ID, "social 30m", cfg, later)
    assert state.mode == "social" and state.revert_to == "trusted"
    assert state.expires_at == later + timedelta(minutes=30) < NOW + timedelta(hours=2)
    assert "social until" in reply


def test_social_cap_is_inclusive(cfg: CrewConfig) -> None:
    """#154: 30 days is accepted; anything past it is refused, and changes nothing."""
    state = run(SwitchState(), cfg, "social 30d")
    assert state.mode == "social" and state.expires_at == NOW + timedelta(days=30)
    start = SwitchState(mode="trusted", revert_to="trusted")
    for text in ("social 30d1s", "social 31d"):
        new, reply = apply_command(start, HUMAN_ID, text, cfg, NOW)
        assert new == start, text
        assert reply.startswith("not a duration (up to 30d)"), reply


def test_social_multiword_duration_is_usage(cfg: CrewConfig) -> None:
    """#154: durations take no spaces. `social 1h 30m` is three words: the usage, no change."""
    start = SwitchState(mode="trusted", revert_to="trusted")
    new, reply = apply_command(start, HUMAN_ID, "social 1h 30m", cfg, NOW)
    assert new == start
    assert "commands:" in reply


def test_social_without_expiry_lapses() -> None:
    """#154: social with no expiry, or a naive (offset-less) one, lapses: it fails closed."""
    stored = from_json({"mode": "social", "expires_at": None, "revert_to": "trusted"})
    assert effective_mode(stored, NOW) == ("trusted", True)
    naive = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    stored = from_json({"mode": "social", "expires_at": naive, "revert_to": "trusted"})
    assert effective_mode(stored, NOW) == ("trusted", True)


def test_reengage_after_lapse(cfg: CrewConfig) -> None:
    state = run(SwitchState(), cfg, "trusted", "social 1h")
    after = NOW + timedelta(hours=2)  # lapsed, but not yet settled
    state, _ = apply_command(state, HUMAN_ID, "social 1h", cfg, after)
    assert state.mode == "social"
    assert state.revert_to == "trusted"  # the lapsed-to mode, never social
    assert state.expires_at == after + timedelta(hours=1)
    assert effective_mode(state, after + timedelta(hours=1)) == ("trusted", True)
    assert settle(state, after + timedelta(hours=1)).mode == "trusted"


def test_lapse_persisted(tmp_path: Path, cfg: CrewConfig) -> None:
    store = SwitchStore(tmp_path)
    store.save(run(SwitchState(), cfg, "trusted", "social 1h"))
    settled = settle(store.load(), NOW + timedelta(hours=2))
    store.save(settled)
    data = json.loads((tmp_path / "switch.json").read_text())
    assert data["mode"] == "trusted" and data["expires_at"] is None
    assert data["revert_to"] != "social"
    assert stat.S_IMODE((tmp_path / "switch.json").stat().st_mode) == 0o600
    # Not lapsed: settle leaves it as it was.
    social = run(SwitchState(), cfg, "social 1h")
    assert settle(social, NOW + timedelta(minutes=5)) is social


def test_kiosk_off_on(cfg: CrewConfig) -> None:
    state = run(SwitchState(), cfg, "kiosk off")
    assert state.kiosk_on is False and state.mode == "crew"
    state = run(state, cfg, "social 1h")
    assert state.kiosk_on is False  # the kiosk flag is independent of the mode
    assert run(state, cfg, "kiosk on").kiosk_on is True


# -- more of the command surface ------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2h", timedelta(hours=2)),
        ("90m", timedelta(minutes=90)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("1d", timedelta(days=1)),
        ("45s", timedelta(seconds=45)),
        ("0h", None),
        ("", None),
        ("2", None),
        ("h", None),
        ("-1h", None),
        ("2x", None),
        ("9" * 5000 + "h", None),
        ("999999999999d", None),
    ],
)
def test_parse_duration(text: str, expected: timedelta | None) -> None:
    assert parse_duration(text) == expected


def test_bad_commands_change_nothing(cfg: CrewConfig) -> None:
    start = SwitchState(mode="trusted", revert_to="trusted")
    for text in ("social", "social forever", "social 31d", "kiosk", "go social", "", "help"):
        new, reply = apply_command(start, HUMAN_ID, text, cfg, NOW)
        assert new == start, text
        assert "commands:" in reply
    new, reply = apply_command(start, HUMAN_ID, "  SOCIAL   2H ", cfg, NOW)
    assert new.mode == "social" and "social until" in reply


def test_crew_and_trusted_clear_social(cfg: CrewConfig) -> None:
    state = run(SwitchState(), cfg, "social 1h", "crew")
    assert (state.mode, state.expires_at, state.revert_to) == ("crew", None, "crew")
    state = run(state, cfg, "social 1h", "trusted")
    assert (state.mode, state.expires_at, state.revert_to) == ("trusted", None, "trusted")


def test_revert_to_never_social_through_any_sequence(cfg: CrewConfig) -> None:
    state = SwitchState()
    now = NOW
    for text in ["social 1h", "social 2h", "trusted", "social 1h", "social 1h", "crew"] * 3:
        state, _ = apply_command(state, HUMAN_ID, text, cfg, now)
        assert state.revert_to in ("crew", "trusted")
        now += timedelta(minutes=50)


def test_corrupt_switch_file_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "switch.json").write_text("[1, 2]")
    assert SwitchStore(tmp_path).load() == SwitchState()
    (tmp_path / "switch.json").write_text('{"mode": "party", "kiosk_on": "no"}')
    assert SwitchStore(tmp_path).load() == SwitchState()


# -- the broker: switch_state and DMs -------------------------------------------------------
def test_switch_state_op_settles_and_notifies(broker: Broker, fake: Any, clock: Any) -> None:
    assert broker.switch_state({}) == {
        "mode": "crew",
        "expires_at": None,
        "revert_to": "crew",
        "kiosk_on": True,
        "effective_mode": "crew",
    }
    assert "social until" in broker.handle_dm(HUMAN_ID, "social 2h")
    state = broker.switch_state({})
    assert state["mode"] == state["effective_mode"] == "social"
    assert state["expires_at"] == (clock.now + timedelta(hours=2)).isoformat()
    assert fake.posts == []

    clock.now += timedelta(hours=3)
    state = broker.switch_state({})
    assert state["mode"] == state["effective_mode"] == "crew"
    assert broker.switch.load().mode == "crew"  # persisted, not just reported
    assert fake.posts == [
        {
            "channel_id": "__".join(sorted([BROKER_ID, HUMAN_ID])),
            "message": "social lapsed → crew",
            "props": {BROKER_PROP: 1},
        }
    ]
    broker.switch_state({})
    assert len(fake.posts) == 1  # the lapse is announced once


def test_switch_state_over_the_frame(broker: Broker) -> None:
    reply = handle_frame(broker, b'{"proto":1,"id":"s","op":"switch_state","args":{}}', True)
    assert reply["ok"] is True and reply["result"]["effective_mode"] == "crew"


def test_lapse_found_by_a_command_is_announced(broker: Broker, fake: Any, clock: Any) -> None:
    broker.handle_dm(HUMAN_ID, "trusted")
    broker.handle_dm(HUMAN_ID, "social 1h")
    clock.now += timedelta(hours=2)
    reply = broker.handle_dm(HUMAN_ID, "kiosk off")
    assert [p["message"] for p in fake.posts] == ["social lapsed → trusted"]
    stored = broker.switch.load()
    assert (stored.mode, stored.kiosk_on) == ("trusted", False)
    assert "trusted crews; kiosk off" in reply


def test_outsider_dm_refused_and_storage_untouched(broker: Broker, fake: Any, clock: Any) -> None:
    broker.handle_dm(HUMAN_ID, "social 1h")
    before = broker.switch.path.read_bytes()
    clock.now += timedelta(hours=2)
    assert broker.handle_dm(OUTSIDER, "crew") == REFUSAL
    assert broker.handle_dm(OUTSIDER, "kiosk off") == REFUSAL
    assert broker.switch.path.read_bytes() == before
    assert fake.posts == []


def test_lapse_notice_failure_does_not_fail_the_op(broker: Broker, fake: Any, clock: Any) -> None:
    broker.handle_dm(HUMAN_ID, "social 1h")
    clock.now += timedelta(hours=2)
    fake.post_fails = True
    assert broker.switch_state({})["mode"] == "crew"


# -- the websocket listener -----------------------------------------------------------------
def posted(
    sender: str,
    message: str,
    *,
    channel_type: str = "D",
    members: tuple[str, str] = (BROKER_ID, HUMAN_ID),
    props: dict[str, Any] | None = None,
    post_type: str = "",
    seq: int = 1,
) -> dict[str, Any]:
    post = {
        "id": f"p{seq}",  # one id per post: the listener handles each id once (#159)
        "user_id": sender,
        "channel_id": "dm-channel",
        "message": message,
        "type": post_type,
        "props": props or {},
    }
    return {
        "event": "posted",
        "seq": seq,
        "data": {
            "channel_type": channel_type,
            "channel_name": "__".join(sorted(members)),
            "post": json.dumps(post),
        },
    }


def test_dm_from_event_filters() -> None:
    dm = dm_from_event(posted(HUMAN_ID, "social 2h"), BROKER_ID)
    assert dm is not None and (dm.sender_user_id, dm.message) == (HUMAN_ID, "social 2h")
    assert dm.channel_id == "dm-channel"
    assert dm_from_event(posted(BROKER_ID, "x"), BROKER_ID) is None  # our own post
    assert dm_from_event(posted(HUMAN_ID, "x", channel_type="O"), BROKER_ID) is None
    assert dm_from_event(posted(HUMAN_ID, "x", members=(HUMAN_ID, "z")), BROKER_ID) is None
    assert dm_from_event(posted(HUMAN_ID, "x", props={BROKER_PROP: 1}), BROKER_ID) is None
    assert dm_from_event(posted(HUMAN_ID, "x", post_type="system_join"), BROKER_ID) is None
    assert dm_from_event({"event": "typing", "data": {}}, BROKER_ID) is None
    assert dm_from_event({"event": "posted", "data": {"post": "{"}}, BROKER_ID) is None
    assert dm_from_event("nope", BROKER_ID) is None


def test_listener_replies_in_the_dm(broker: Broker, fake: Any) -> None:
    listener = DMListener(broker, "https://mm.example", "tok")
    listener.handle(json.dumps({"event": "hello", "seq": 0, "data": {"connection_id": "c1"}}))
    listener.handle(json.dumps(posted(HUMAN_ID, "social 2h", seq=1)))
    listener.handle(json.dumps(posted(OUTSIDER, "crew", seq=2)))
    listener.handle(b"not json")
    assert broker.switch.load().mode == "social"
    assert [(p["channel_id"], p["props"]) for p in fake.posts] == [
        ("dm-channel", {BROKER_PROP: 1}),
        ("dm-channel", {BROKER_PROP: 1}),
    ]
    assert "social until" in fake.posts[0]["message"]
    assert fake.posts[1]["message"] == REFUSAL
    assert listener.uri() == (
        "wss://mm.example/api/v4/websocket?connection_id=c1&sequence_number=3"
    )


def test_listener_survives_a_failed_reply(broker: Broker, fake: Any) -> None:
    fake.post_fails = True
    DMListener(broker, "http://mm", "tok").handle(json.dumps(posted(HUMAN_ID, "kiosk off")))
    assert broker.switch.load().kiosk_on is False


class FakeWS:
    def __init__(self, frames: list[str], stop: threading.Event) -> None:
        self.frames = frames
        self.stop = stop
        self.closed = False

    def recv(self, timeout: float | None = None) -> str:
        if self.frames:
            return self.frames.pop(0)
        self.stop.set()
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def test_listener_run_connects_with_the_token_and_reconnects(broker: Broker, fake: Any) -> None:
    stop = threading.Event()
    seen: list[tuple[str, dict[str, str]]] = []
    attempts = iter([OSError("refused"), "ws"])
    sleeps: list[float] = []

    def connect(uri: str, headers: dict[str, str]) -> FakeWS:
        seen.append((uri, headers))
        nxt = next(attempts)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeWS([json.dumps(posted(HUMAN_ID, "trusted"))], stop)

    def sleep(delay: float) -> bool:
        sleeps.append(delay)
        return stop.is_set()

    DMListener(broker, "http://mm", "tok", connect=connect, sleep=sleep).run(stop)
    assert [h for _, h in seen] == [{"Authorization": "Bearer tok"}] * 2
    assert seen[0][0] == "ws://mm/api/v4/websocket"
    assert sleeps[0] == 1.0
    assert broker.switch.load().mode == "trusted"
    assert len(fake.posts) == 1


def test_switch_file_lives_in_the_instance_state_dir(
    fake: Any, crew_config: CrewConfig, tmp_path: Path
) -> None:
    b = Broker(crew_config, fake, tmp_path / "oaw-dev")
    b.handle_dm(HUMAN_ID, "kiosk off")
    assert (tmp_path / "oaw-dev" / "switch.json").exists()


# -- the DM backfill (#159) -----------------------------------------------------------------
DM_CHANNEL = "__".join(sorted([BROKER_ID, HUMAN_ID]))  # the fake's direct_channel id
HELLO = json.dumps({"event": "hello", "seq": 0, "data": {"connection_id": "c1"}})


def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def dm_post(post_id: str, message: str, at: datetime, **extra: Any) -> dict[str, Any]:
    """A DM to the broker, as `GET /channels/{id}/posts` gives it."""
    return {
        "id": post_id,
        "user_id": HUMAN_ID,
        "channel_id": DM_CHANNEL,
        "message": message,
        "type": "",
        "props": {},
        "create_at": ms(at),
        "update_at": ms(at),
        "delete_at": 0,
        **extra,
    }


def posted_event(post: dict[str, Any], seq: int = 1) -> str:
    """The websocket's `posted` frame for `post`, a DM to the broker."""
    data = {"channel_type": "D", "channel_name": DM_CHANNEL, "post": json.dumps(post)}
    return json.dumps({"event": "posted", "seq": seq, "data": data})


def replies(fake: Any) -> list[str]:
    assert all(p["props"] == {BROKER_PROP: 1} for p in fake.posts)
    return [p["message"] for p in fake.posts]


def test_backfill_applies_recent_dm(broker: Broker, fake: Any, clock: Any) -> None:
    broker.dm_cursor.save(ms(clock.now - timedelta(minutes=5)))
    sent = clock.now - timedelta(seconds=60)
    fake.history[DM_CHANNEL] = [
        dm_post("p1", "social 2h", sent),
        dm_post("r1", "an earlier reply", sent, user_id=BROKER_ID, props={BROKER_PROP: 1}),
    ]
    DMListener(broker, "http://mm", "tok").handle(HELLO)
    assert broker.switch.load().mode == "social"
    assert [p["channel_id"] for p in fake.posts] == [DM_CHANNEL]
    assert "social until" in replies(fake)[0]
    assert broker.dm_cursor.load() == ms(sent)


def test_backfill_refuses_stale_widening(broker: Broker, fake: Any, clock: Any) -> None:
    broker.dm_cursor.save(ms(clock.now - timedelta(hours=1)))
    sent = clock.now - timedelta(minutes=20)
    fake.history[DM_CHANNEL] = [dm_post("p1", "social 2h", sent)]
    DMListener(broker, "http://mm", "tok").handle(HELLO)
    assert broker.switch.load().mode == "crew"
    assert replies(fake) == [
        "not applied: sent 2026-10-01T11:40:00+00:00 while the broker was offline; send it again"
    ]
    assert broker.dm_cursor.load() == ms(sent)  # answered: never refused twice


def test_backfill_applies_stale_narrowing(broker: Broker, fake: Any, clock: Any) -> None:
    broker.handle_dm(HUMAN_ID, "social 1d")
    broker.dm_cursor.save(ms(clock.now - timedelta(hours=3)))
    fake.history[DM_CHANNEL] = [
        dm_post("p1", " Kiosk  OFF ", clock.now - timedelta(hours=2, minutes=1)),
        dm_post("p2", "crew", clock.now - timedelta(hours=2)),
    ]
    DMListener(broker, "http://mm", "tok").handle(HELLO)
    state = broker.switch.load()
    assert (state.mode, state.kiosk_on) == ("crew", False)
    assert not any(r.startswith("not applied") for r in replies(fake))
    assert len(fake.posts) == 2


def test_backfill_dedupes_with_websocket(broker: Broker, fake: Any, clock: Any) -> None:
    broker.dm_cursor.save(ms(clock.now - timedelta(minutes=5)))
    post = dm_post("p1", "kiosk off", clock.now - timedelta(seconds=30))
    fake.history[DM_CHANNEL] = [post]
    listener = DMListener(broker, "http://mm", "tok")
    listener.handle(HELLO)  # the backfill applies it
    listener.handle(posted_event(post, seq=1))  # the resumed websocket replays it
    assert len(fake.posts) == 1

    # The other order: the websocket first, then a reconnect's backfill.
    fake.posts.clear()
    post2 = dm_post("p2", "kiosk on", clock.now - timedelta(seconds=10))
    fake.history[DM_CHANNEL].append(post2)
    listener.handle(posted_event(post2, seq=2))
    listener.handle(HELLO)
    assert len(fake.posts) == 1
    assert broker.switch.load().kiosk_on is True


def test_backfill_first_start_no_replay(broker: Broker, fake: Any, clock: Any) -> None:
    fake.history[DM_CHANNEL] = [dm_post("p1", "kiosk off", clock.now - timedelta(seconds=30))]
    DMListener(broker, "http://mm", "tok").handle(HELLO)
    assert broker.switch.load().kiosk_on is True
    assert fake.posts == [] and not any(c[0] == "posts_since" for c in fake.calls)
    assert broker.dm_cursor.load() == ms(clock.now)


def test_backfill_unreadable_cursor_is_a_first_start(broker: Broker, fake: Any, clock: Any) -> None:
    """A corrupt cursor never replays history: that would answer every old DM."""
    broker.dm_cursor.path.parent.mkdir(parents=True, exist_ok=True)
    broker.dm_cursor.path.write_text("{", encoding="utf-8")
    fake.history[DM_CHANNEL] = [dm_post("p1", "crew", clock.now - timedelta(days=30))]
    DMListener(broker, "http://mm", "tok").handle(HELLO)
    assert fake.posts == []
    assert broker.dm_cursor.load() == ms(clock.now)


def test_backfill_skips_edited_deleted_and_failed_reads(
    broker: Broker, fake: Any, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's `since` returns old posts edited since: those are not new DMs."""
    cursor = clock.now - timedelta(minutes=5)
    broker.dm_cursor.save(ms(cursor))
    fake.history[DM_CHANNEL] = [
        dm_post("old", "social 2h", cursor - timedelta(minutes=1), update_at=ms(clock.now)),
        dm_post("gone", "social 2h", clock.now - timedelta(seconds=5), delete_at=ms(clock.now)),
    ]
    listener = DMListener(broker, "http://mm", "tok")
    listener.handle(HELLO)
    assert fake.posts == [] and broker.switch.load().mode == "crew"

    def down(channel_id: str, since_ms: int) -> list[dict[str, Any]]:
        raise OSError("connection refused")

    monkeypatch.setattr(fake, "posts_since", down)
    listener.handle(HELLO)  # logged, never raised
    assert broker.dm_cursor.load() == ms(cursor)


def test_live_dm_advances_the_cursor(broker: Broker, fake: Any, clock: Any) -> None:
    broker.dm_cursor.save(ms(clock.now - timedelta(minutes=5)))
    DMListener(broker, "http://mm", "tok").handle(posted_event(dm_post("p1", "trusted", clock.now)))
    assert broker.switch.load().mode == "trusted"
    assert broker.dm_cursor.load() == ms(clock.now)


PARTNER_B = "partner-b-user-id"
B_CHANNEL = "__".join(sorted([BROKER_ID, PARTNER_B]))


@pytest.fixture
def two_partners(fake: Any, crew_config: CrewConfig, tmp_path: Path, clock: Any) -> Broker:
    config = replace(crew_config, human_user_ids=[HUMAN_ID, PARTNER_B])
    return Broker(config, fake, tmp_path / "state2", clock=clock)


def test_backfill_applies_all_partners_in_create_at_order(
    two_partners: Broker, fake: Any, clock: Any
) -> None:
    """The partners share one switch: B's earlier `social 1h` must not land after A's later
    `crew`, though A's channel is read first."""
    two_partners.dm_cursor.save(ms(clock.now - timedelta(minutes=5)))
    fake.history[B_CHANNEL] = [
        dm_post(
            "b1",
            "social 1h",
            clock.now - timedelta(seconds=60),
            user_id=PARTNER_B,
            channel_id=B_CHANNEL,
        )
    ]
    fake.history[DM_CHANNEL] = [dm_post("a1", "crew", clock.now - timedelta(seconds=30))]
    DMListener(two_partners, "http://mm", "tok").handle(HELLO)
    assert two_partners.switch.load().mode == "crew"
    assert [p["channel_id"] for p in fake.posts] == [B_CHANNEL, DM_CHANNEL]


def test_failed_read_keeps_the_backfill_owed(
    two_partners: Broker, fake: Any, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read applies nothing and moves no cursor, not even for a live DM in the
    meantime. The retry still applies what was missed, and an older widening command that
    a newer live one has overtaken is refused."""
    cursor = ms(clock.now - timedelta(minutes=5))
    two_partners.dm_cursor.save(cursor)
    fake.history[DM_CHANNEL] = [
        dm_post("a1", "kiosk off", clock.now - timedelta(seconds=90)),
        dm_post("a2", "social 1h", clock.now - timedelta(seconds=60)),
    ]
    reads = fake.posts_since
    down = {"on": True}

    def flaky(channel_id: str, since_ms: int) -> list[dict[str, Any]]:
        if down["on"] and channel_id == B_CHANNEL:
            raise OSError("connection refused")
        posts: list[dict[str, Any]] = reads(channel_id, since_ms)
        return posts

    monkeypatch.setattr(fake, "posts_since", flaky)
    listener = DMListener(two_partners, "http://mm", "tok")
    listener.handle(HELLO)
    assert fake.posts == [] and two_partners.dm_cursor.load() == cursor  # A's not applied alone

    live = dm_post("a3", "crew", clock.now - timedelta(seconds=10))
    listener.handle(posted_event(live, seq=1))  # the retry fails again; the live DM applies
    assert two_partners.switch.load().mode == "crew"
    assert two_partners.dm_cursor.load() == cursor  # still owed: the cursor holds

    down["on"] = False
    listener.handle(HELLO)
    state = two_partners.switch.load()
    assert (state.mode, state.kiosk_on) == ("crew", False)  # narrowing applied, social refused
    assert [m for m in replies(fake) if m.startswith("not applied")] == [
        "not applied: sent 2026-10-01T11:59:00+00:00 while the broker was offline; send it again"
    ]
    assert two_partners.dm_cursor.load() == live["create_at"]


def test_refused_partner_does_not_hold_the_backfill(
    two_partners: Broker, fake: Any, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partner the server refuses (a deleted or mistyped ID) never reads on a retry either:
    they are skipped, and the other partner's missed DMs apply. A 5xx still holds."""
    cursor = ms(clock.now - timedelta(minutes=5))
    two_partners.dm_cursor.save(cursor)
    missed = dm_post("a1", "crew", clock.now - timedelta(seconds=30))
    fake.history[DM_CHANNEL] = [missed]
    opens = fake.direct_channel
    refusal: dict[str, MattermostError] = {
        "exc": MattermostError("POST", "/channels/direct", 503, "unavailable")
    }

    def refuse_b(user_id: str, other_id: str) -> str:
        if other_id == PARTNER_B:
            raise refusal["exc"]
        channel: str = opens(user_id, other_id)
        return channel

    monkeypatch.setattr(fake, "direct_channel", refuse_b)
    two_partners.handle_dm(HUMAN_ID, "social 1d")
    listener = DMListener(two_partners, "http://mm", "tok")
    listener.handle(HELLO)
    assert fake.posts == [] and two_partners.dm_cursor.load() == cursor  # 503: held

    refusal["exc"] = NotFound("POST", "/channels/direct", 404, "user not found")
    listener.handle(HELLO)
    assert two_partners.switch.load().mode == "crew"
    assert two_partners.dm_cursor.load() == missed["create_at"]
