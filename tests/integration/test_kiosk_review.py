"""IT-22 against the CI leg's Mattermost (Story 3.6; R-31, R-33).

The daemon is wired as `crewchat serve` wires it, through the Story 3.3 rig: real brokers,
`MattermostBackend`, `SwitchCache`, `Filters`, `Doorbell` and the Story 3.5 `Kiosk`, whose
`deliver` is the doorbell's and whose `on_social` is the switch cache's social listener. The
partner's switch commands go to the real broker. The model runner is a stub; the assertions
are on what reaches the working agent's doorbell queue, and on the review cursor.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from crewchat.kiosk import REVIEW_TEXT, BriefStore, Kiosk
from crewchat_clients.mcp_shim import render_event

from tests.integration.test_filters import (  # noqa: F401  (the module fixture)
    EVENT_TIMEOUT_S,
    MM,
    Rig,
    world,
)
from tests.integration.test_kiosk import wait_answers
from tests.integration.test_mattermost_adapter import SID

RENDERED = (
    "[from outside the crew] Kiosk review: review these exchanges and raise anything "
    "notable to your human partner."
)


def cursor_state(transcript: Path) -> dict[str, Any]:
    """The cursor a review of all of `transcript` leaves (#110): its size, and its head as the
    kiosk derives it."""
    with transcript.open("rb") as fh:
        head = Kiosk._head(fh.readline())
    assert head is not None
    return {"offset": transcript.stat().st_size, "head": head}


def review_id(transcript: Path) -> str:
    """The `event_id` of a review of all of `transcript` (#110): its head and its end."""
    state = cursor_state(transcript)
    return f"{MM}:kiosk.review:{state['head']}:{state['offset']}"


class Stub:
    def __init__(self) -> None:
        self.calls = 0

    async def answer(self, prompt: str) -> str:
        self.calls += 1
        return f"We build a chat daemon (answer {self.calls})."


def test_it22_kiosk_review(mattermost: Any, world: dict[str, Any], tmp_path: Path) -> None:  # noqa: F811
    """IT-22 (R-33), and R-31: no kiosk content reaches the working agent while social is
    off."""

    async def scenario() -> None:
        rig = Rig(mattermost, world, tmp_path)
        rig.cache.clock = rig.clock  # the broker's expiry and the cache's read: one clock
        await rig.register("babel")
        await rig.register("kessel")
        await rig.cache.refresh_all()

        briefs = BriefStore({"babel": "This crew is building mattermost-crew."})
        state = tmp_path / "state"
        kiosk = Kiosk(
            rig.kiosk,
            briefs,
            {MM: rig.backend},
            Stub(),
            state_dir=state,
            deliver=rig.doorbell.deliver,
        )
        rig.cache.social_listeners.append(
            lambda crew, sid: kiosk.on_social(sid, rig.registry.agents_on(crew, sid))
        )
        kiosk.start()
        await rig.listen()
        transcript, cursor = kiosk.review_paths("babel", SID)
        try:
            # Crew only: the outsider is answered by the kiosk, twice.
            root = rig.post_as(rig.outsider_token, "@babel what are you building?")
            await wait_answers(mattermost, root, 1)
            rig.post_as(rig.outsider_token, "@babel and who runs it?", root=root)
            await wait_answers(mattermost, root, 2)
            await rig.cache.refresh_all()

            # R-31, R-33: during that kiosk traffic the working agent's queue stayed empty,
            # and the next thing it gets is a crewmate's sentinel, not kiosk content.
            assert rig.doorbell.queue("babel").empty()
            await rig.dropped("kiosk traffic under crew only")
            assert not cursor.exists()

            # The partner engages social: one kiosk.review, marked outsider, holding this
            # server's transcript since the cursor.
            assert "social" in rig.flip("social 1h")
            await rig.cache.refresh_all()
            event = await asyncio.wait_for(rig.doorbell.next_event("babel"), EVENT_TIMEOUT_S)
            payload = event.payload
            assert event.reply()["event_id"] == review_id(transcript)
            assert payload["type"] == "kiosk.review"
            assert payload["outsider"] is True
            assert payload["server"] == SID
            assert payload["text"] == REVIEW_TEXT
            assert render_event(payload)["text"].startswith(RENDERED)
            entries = payload["entries"]
            assert [e["question"] for e in entries] == [
                "@babel what are you building?",
                "@babel and who runs it?",
            ]
            assert {e["asker_id"] for e in entries} == {rig.outsider_id}
            assert {e["thread"] for e in entries} == {root}
            assert entries[0]["answer"].startswith("We build a chat daemon (answer 1)")

            # The cursor advanced past them, and a refresh under social reviews nothing more.
            assert json.loads(cursor.read_text()) == cursor_state(transcript)
            first_id = event.reply()["event_id"]
            await rig.cache.refresh_all()
            await asyncio.sleep(0.2)
            assert rig.doorbell.queue("babel").empty()

            # Back to crew, one more kiosk exchange, social again: only the new entry.
            assert rig.flip("crew")
            await rig.cache.refresh_all()
            again = rig.post_as(rig.outsider_token, "@babel anything new?")
            await wait_answers(mattermost, again, 1)
            assert rig.doorbell.queue("babel").empty()
            assert "social" in rig.flip("social 1h")
            await rig.cache.refresh_all()
            event = await asyncio.wait_for(rig.doorbell.next_event("babel"), EVENT_TIMEOUT_S)
            payload = event.payload
            assert payload["type"] == "kiosk.review"
            assert [e["question"] for e in payload["entries"]] == ["@babel anything new?"]
            # Same transcript, so the same head; a later offset, so a new ID.
            assert event.reply()["event_id"] == review_id(transcript) != first_id
            assert json.loads(cursor.read_text()) == cursor_state(transcript)
        finally:
            rig.flip("crew")
            await kiosk.aclose()
            await rig.aclose()

    asyncio.run(scenario())
