"""Story 3.6: the kiosk review on social engage (R-31, R-33).

The switch cache is the real `SwitchCache` over in-process brokers whose `switch_state` the
test sets; the review is the real `Kiosk.review_since_cursor` over a transcript on disk; the
working agent's queue is the real `Doorbell`'s.
"""

from __future__ import annotations

import asyncio
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import crewchat.kiosk as kiosk_mod
import pytest
from crewchat.adapters.base import InboundMessage
from crewchat.doorbell import CursorStore, Doorbell, DoorbellEvent
from crewchat.filters import KIOSK, KioskQueue, SwitchCache
from crewchat.kiosk import REVIEW_TEXT, REVIEW_TYPE, BriefStore, Kiosk
from crewchat.tokens import BrokerClient
from crewchat_clients.doorbell import run as doorbell_run
from crewchat_clients.mcp_shim import OUTSIDER_PREFIX, render_event

DEV = "babel"
A, B = "srv-a", "srv-b"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
RENDERED = (
    "[from outside the crew] Kiosk review: review these exchanges and raise anything "
    "notable to your human partner."
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class SwitchBroker(BrokerClient):
    """A broker whose `switch_state` the test sets."""

    def __init__(self, crew: str, server_id: str) -> None:
        super().__init__(f"/run/crewbroker/{crew}-{server_id}.sock", 1.0, name=crew)
        self.state: dict[str, Any] = {"mode": "crew", "revert_to": "crew", "kiosk_on": True}

    def set(
        self, mode: str, *, expires_at: datetime | None = None, revert_to: str = "crew"
    ) -> None:
        self.state = {
            "mode": mode,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "revert_to": revert_to,
            "kiosk_on": True,
        }

    async def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "switch_state":
            return dict(self.state)
        if op == "crew_info":
            return {"human_user_ids": [], "trusted": [], "minted_bot_ids": []}
        raise AssertionError(op)


class NoRunner:
    async def answer(self, prompt: str) -> str:
        raise AssertionError("the review never runs the model")


def make_kiosk(tmp_path: Path, delivered: list[tuple[str, DoorbellEvent]] | None = None) -> Kiosk:
    out = delivered if delivered is not None else []

    def deliver(dev: str, event: DoorbellEvent) -> bool:
        out.append((dev, event))
        return True

    return Kiosk(KioskQueue(), BriefStore(), {}, NoRunner(), state_dir=tmp_path, deliver=deliver)


def record(kiosk: Kiosk, server_id: str, n: int, *, dev: str = DEV) -> dict[str, Any]:
    """Append one Q&A to `dev`'s transcript on `server_id`, as the kiosk writes it."""
    transcript, _ = kiosk.review_paths(dev, server_id)
    rec = {
        "ts": 1000.0 + n,
        "thread": f"root-{n}",
        "asker_id": "outsider-1",
        "question": f"question {n} on {server_id}",
        "answer": f"answer {n}",
    }
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def cursor_of(kiosk: Kiosk, server_id: str) -> Path:
    return kiosk.review_paths(DEV, server_id)[1]


# -- the transition -------------------------------------------------------------------------
def test_review_on_social_transition_only() -> None:
    async def scenario() -> None:
        clock = Clock()
        broker = SwitchBroker("oaw", A)
        cache = SwitchCache({("oaw", A): broker}, clock=clock)
        fired: list[tuple[str, str]] = []
        cache.social_listeners.append(lambda crew, sid: fired.append((crew, sid)))

        async def step(mode: str, **kw: Any) -> int:
            broker.set(mode, **kw)
            await cache.refresh("oaw", A)
            return len(fired)

        later = T0 + timedelta(hours=1)
        assert await step("crew") == 0
        assert await step("trusted") == 0  # crew → trusted: no review
        assert await step("crew") == 0
        assert await step("social", expires_at=later) == 1  # crew → social
        assert fired == [("oaw", A)]
        assert await step("social", expires_at=later) == 1  # still social: no second review
        assert await step("crew") == 1  # social → crew: none
        assert await step("trusted") == 1
        assert await step("social", expires_at=later, revert_to="trusted") == 2  # trusted → social
        # Social lapsing by the clock is a move away from social, never a review.
        clock.now = later + timedelta(seconds=1)
        assert await step("social", expires_at=later, revert_to="trusted") == 2
        # Social with no expiry is not in force (R-23): no review.
        assert await step("social") == 2
        # Engaged again, on time.
        assert await step("social", expires_at=clock.now + timedelta(minutes=5)) == 3

    asyncio.run(scenario())


def test_review_on_first_fetch_finding_social() -> None:
    """A daemon that starts while social is engaged reviews once: the cursor keeps that safe."""

    async def scenario() -> None:
        broker = SwitchBroker("oaw", A)
        broker.set("social", expires_at=T0 + timedelta(hours=1))
        cache = SwitchCache({("oaw", A): broker}, clock=Clock())
        fired: list[tuple[str, str]] = []
        cache.social_listeners.append(lambda crew, sid: fired.append((crew, sid)))
        await cache.refresh_all()
        await cache.refresh_all()
        assert fired == [("oaw", A)]

    asyncio.run(scenario())


def test_social_listener_failure_is_contained() -> None:
    async def scenario() -> None:
        broker = SwitchBroker("oaw", A)
        broker.set("social", expires_at=T0 + timedelta(hours=1))
        cache = SwitchCache({("oaw", A): broker}, clock=Clock())
        fired: list[str] = []

        def boom(crew: str, sid: str) -> None:
            raise RuntimeError("listener")

        cache.social_listeners.extend([boom, lambda crew, sid: fired.append(sid)])
        await cache.refresh("oaw", A)
        assert fired == [A]
        assert cache.switch_state("oaw", A) is not None

    asyncio.run(scenario())


# -- the review -----------------------------------------------------------------------------
def test_review_since_cursor_and_advance(tmp_path: Path) -> None:
    delivered: list[tuple[str, DoorbellEvent]] = []
    kiosk = make_kiosk(tmp_path, delivered)
    first = [record(kiosk, A, 1), record(kiosk, A, 2)]

    assert kiosk.review_since_cursor(DEV, A) == 2
    [(dev, event)] = delivered
    assert dev == DEV
    assert event.payload["entries"] == first
    transcript, cursor = kiosk.review_paths(DEV, A)
    assert json.loads(cursor.read_text()) == {"offset": transcript.stat().st_size}
    assert stat.S_IMODE(cursor.stat().st_mode) == 0o600

    # Nothing new: nothing enqueued, the cursor where it was.
    assert kiosk.review_since_cursor(DEV, A) == 0
    assert len(delivered) == 1

    # Only the new entry; a record still being appended waits for the next review.
    third = record(kiosk, A, 3)
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1004.0, "thread": "root-4"')
    assert kiosk.review_since_cursor(DEV, A) == 1
    assert delivered[-1][1].payload["entries"] == [third]
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(', "asker_id": "o", "question": "q4", "answer": "a4"}\n')
    assert kiosk.review_since_cursor(DEV, A) == 1
    assert delivered[-1][1].payload["entries"][0]["question"] == "q4"
    assert json.loads(cursor.read_text()) == {"offset": transcript.stat().st_size}


def test_review_skips_malformed_lines_and_restarts_a_replaced_transcript(tmp_path: Path) -> None:
    delivered: list[tuple[str, DoorbellEvent]] = []
    kiosk = make_kiosk(tmp_path, delivered)
    record(kiosk, A, 1)
    transcript, cursor = kiosk.review_paths(DEV, A)
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write("not json\n[1, 2]\n")
    good = record(kiosk, A, 2)
    assert kiosk.review_since_cursor(DEV, A) == 2
    assert [e["question"] for e in delivered[-1][1].payload["entries"]] == [
        "question 1 on srv-a",
        good["question"],
    ]
    # The transcript replaced by a shorter one: reviewed from its start.
    transcript.unlink()
    fresh = record(kiosk, A, 9)
    assert kiosk.review_since_cursor(DEV, A) == 1
    assert delivered[-1][1].payload["entries"] == [fresh]
    # An unreadable cursor reviews from the start rather than losing entries.
    cursor.write_text("garbage")
    assert kiosk.review_since_cursor(DEV, A) == 1


def test_review_only_that_servers_transcript(tmp_path: Path) -> None:
    delivered: list[tuple[str, DoorbellEvent]] = []
    kiosk = make_kiosk(tmp_path, delivered)
    a1 = record(kiosk, A, 1)
    record(kiosk, B, 1)
    record(kiosk, B, 2)
    b_cursor = cursor_of(kiosk, B)

    # Social engaged on server A: only A's transcript; B's cursor never written.
    assert kiosk.on_social(A, [DEV]) == 1
    [(_, event)] = delivered
    assert event.payload["server"] == A
    assert event.backend == f"mattermost:{A}"
    assert event.payload["entries"] == [a1]
    assert not b_cursor.exists()

    # B reviewed later: B's own entries, and A's cursor unchanged by it.
    a_before = cursor_of(kiosk, A).read_text()
    assert kiosk.on_social(B, [DEV]) == 2
    assert {e["question"] for e in delivered[-1][1].payload["entries"]} == {
        "question 1 on srv-b",
        "question 2 on srv-b",
    }
    assert cursor_of(kiosk, A).read_text() == a_before


def test_review_marked_outsider(tmp_path: Path) -> None:
    delivered: list[tuple[str, DoorbellEvent]] = []
    kiosk = make_kiosk(tmp_path, delivered)
    record(kiosk, A, 1)
    kiosk.review_since_cursor(DEV, A)
    payload = delivered[0][1].payload
    assert payload["type"] == REVIEW_TYPE == "kiosk.review"
    assert payload["outsider"] is True
    assert payload["text"] == REVIEW_TEXT
    # As the shim renders it, the text opens with the outsider marker.
    rendered = render_event(payload)
    assert rendered["text"].startswith(RENDERED)
    assert rendered["text"].startswith(OUTSIDER_PREFIX)
    assert render_event(rendered) == rendered  # never prefixed twice
    # An unmarked event is left alone.
    plain = {"type": "backend.up", "backend": "discord", "text": "x"}
    assert render_event(plain) == plain


def test_doorbell_one_shot_prints_the_rendered_review(tmp_path: Path, monkeypatch: Any) -> None:
    import crewchat_clients.doorbell as client

    payload = {
        "type": REVIEW_TYPE,
        "outsider": True,
        "server": A,
        "text": REVIEW_TEXT,
        "entries": [],
    }
    monkeypatch.setattr(client, "subscribe", lambda *_a: dict(payload))
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "agent-identity.json").write_text(json.dumps({"dev_name": DEV}))

    class Out:
        def __init__(self) -> None:
            self.text = ""

        def write(self, s: str) -> int:
            self.text += s
            return len(s)

        def flush(self) -> None:
            pass

    out, err = Out(), Out()
    assert doorbell_run(repo, tmp_path / "sock", {}, out, err) == 0  # type: ignore[arg-type]
    assert json.loads(out.text)["text"].startswith(RENDERED)


def test_review_full_queue_keeps_the_cursor(tmp_path: Path) -> None:
    kiosk = Kiosk(
        KioskQueue(),
        BriefStore(),
        {},
        NoRunner(),
        state_dir=tmp_path,
        deliver=lambda dev, event: False,
    )
    record(kiosk, A, 1)
    assert kiosk.review_since_cursor(DEV, A) == 0
    assert not cursor_of(kiosk, A).exists()


def test_review_backlog_split_to_fit_a_frame(tmp_path: Path, monkeypatch: Any) -> None:
    delivered: list[tuple[str, DoorbellEvent]] = []
    kiosk = make_kiosk(tmp_path, delivered)
    one = len(json.dumps(record(kiosk, A, 1)))
    for n in range(2, 6):
        record(kiosk, A, n)
    monkeypatch.setattr(kiosk_mod, "REVIEW_MAX_BYTES", one * 2)
    assert kiosk.review_since_cursor(DEV, A) == 5
    sizes = [len(e.payload["entries"]) for _, e in delivered]
    assert sizes == [2, 2, 1]
    assert [e["thread"] for _, ev in delivered for e in ev.payload["entries"]] == [
        f"root-{n}" for n in range(1, 6)
    ]


def test_review_with_no_doorbell_keeps_the_cursor(tmp_path: Path) -> None:
    kiosk = Kiosk(KioskQueue(), BriefStore(), {}, NoRunner(), state_dir=tmp_path)
    record(kiosk, A, 1)
    assert kiosk.review_since_cursor(DEV, A) == 0
    assert not cursor_of(kiosk, A).exists()


def test_review_rejects_unsafe_keys(tmp_path: Path) -> None:
    kiosk = make_kiosk(tmp_path)
    with pytest.raises(ValueError):
        kiosk.review_since_cursor("../x", A)
    assert kiosk.on_social("../x", [DEV]) == 0  # contained, logged


# -- wired as serve() wires it --------------------------------------------------------------
def outsider_event(n: int) -> DoorbellEvent:
    msg = InboundMessage(
        backend=f"mattermost:{A}",
        channel="chan",
        post_id=f"p{n}",
        root_id="",
        author_id="outsider-1",
        author_name="outsider",
        text=f"@{DEV} question {n}",
        ts=1000.0 + n,
    )
    payload = {"type": "message", "backend": msg.backend, "message": {"post_id": msg.post_id}}
    return DoorbellEvent(msg.backend, payload, (msg.post_id,), msg.ts, msg)


def test_no_kiosk_content_reaches_the_agent_while_social_is_off(tmp_path: Path) -> None:
    """R-31, R-33: during kiosk traffic under crew only, the working agent's queue stays empty;
    engaging social puts the one review there."""

    async def scenario() -> None:
        clock = Clock()
        broker = SwitchBroker("oaw", A)
        cache = SwitchCache({("oaw", A): broker}, clock=clock)
        kiosk_q = KioskQueue()
        doorbell = Doorbell({}, CursorStore(None), admit=lambda dev, ev: KIOSK, kiosk=kiosk_q)
        kiosk = Kiosk(
            kiosk_q, BriefStore(), {}, NoRunner(), state_dir=tmp_path, deliver=doorbell.deliver
        )
        cache.social_listeners.append(lambda crew, sid: kiosk.on_social(sid, [DEV]))
        await cache.refresh_all()

        # Kiosk traffic under crew: outsiders' messages go to the kiosk, answers to its
        # transcript, and the switch cache refreshes, but the agent's queue stays empty.
        for n in range(3):
            assert await doorbell.offer(DEV, f"mattermost:{A}", outsider_event(n)) is False
            record(kiosk, A, n)
            await cache.refresh_all()
        assert doorbell.queue(DEV).empty()
        assert kiosk_q.queue(DEV).qsize() == 3
        broker.set("trusted")
        await cache.refresh_all()
        assert doorbell.queue(DEV).empty()

        # Social engaged: one review, holding the transcript, marked outsider.
        broker.set("social", expires_at=T0 + timedelta(hours=1))
        await cache.refresh_all()
        assert doorbell.queue(DEV).qsize() == 1
        event = await doorbell.next_event(DEV)
        assert event.payload["type"] == "kiosk.review"
        assert event.payload["outsider"] is True
        assert [e["thread"] for e in event.payload["entries"]] == ["root-0", "root-1", "root-2"]
        await cache.refresh_all()
        assert doorbell.queue(DEV).empty()

    asyncio.run(scenario())
