"""E2E-02: an outsider, and the partner's switch (Story 3.8; R-21, R-23, R-31, R-33).

An unlisted human asks a crew-only agent a question and is answered by the kiosk; the working
agent's doorbell stays silent (R-21, R-31). The partner DMs `social 1m` to the crew's broker:
the agent's doorbell rings with its `kiosk.review`, marked outsider (R-33). The outsider asks
again and the working agent receives it, marked outsider (R-23). Past the expiry, on the
injected clock, the next delivery is back under crew only: the kiosk answers, the doorbell
stays silent, and the broker tells the partner social lapsed (R-23).
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from crewchat.kiosk import REVIEW_TEXT
from crewchat_clients.mcp_shim import OUTSIDER_PREFIX

from tests.e2e.conftest import MM, REPLY_TIMEOUT_S, SID, Daemon, World
from tests.integration.test_kiosk import kiosk_answers, wait_answers

BRIEF = "This crew is building mattermost-crew, a chat daemon and a per-crew token broker."


def test_e2e02_outsider_and_switch(e2e: World) -> None:
    oaw = e2e.crews["oaw"]
    babel, _kessel = oaw.agents

    async def scenario() -> None:
        async with Daemon(e2e) as d:
            channel = await asyncio.to_thread(e2e.channel, "outsider")
            for dev in oaw.agents:
                await d.register(dev, oaw, [channel])
            await d.op("kiosk_brief", {"dev_name": babel, "text": BRIEF})
            await d.cache.refresh(oaw.name, SID)
            state = d.cache.switch_state(oaw.name, SID)
            assert state is not None and state.mode == "crew" and state.kiosk_on

            # Crew only: the outsider's question is answered by the kiosk, in its thread, as
            # babel; babel's doorbell stays silent (R-21, R-31).
            ask = await asyncio.to_thread(
                e2e.post, e2e.outsider_token, f"@{babel} what are you building?", channel
            )
            answers = await wait_answers(e2e.mattermost, ask, 1)
            assert answers[0]["user_id"] == d.ids[babel]
            assert answers[0]["message"].startswith("We build a chat daemon")
            assert any(BRIEF in p for p in d.runner.prompts)
            await d.silent(babel)

            # The partner DMs `social 1m`: babel's doorbell rings with the kiosk review,
            # marked outsider and rendered as outsider content (R-33).
            reply = await asyncio.to_thread(e2e.switch, oaw, "social 1m")
            assert "social until" in reply
            await d.cache.refresh(oaw.name, SID)
            review = await d.ring(babel)
            assert review["type"] == "kiosk.review", review
            assert review["outsider"] is True
            assert review["server"] == SID
            assert review["text"] == f"{OUTSIDER_PREFIX} {REVIEW_TEXT}"
            assert [e["question"] for e in review["entries"]] == [
                f"@{babel} what are you building?"
            ]
            assert {e["asker_id"] for e in review["entries"]} == {e2e.outsider_id}
            await d.silent(babel)

            # The outsider asks again: babel receives it, marked outsider; no kiosk answer.
            again = await asyncio.to_thread(
                e2e.post, e2e.outsider_token, f"@{babel} and who runs it?", channel
            )
            got = await d.delivered(babel, again)
            assert got["backend"] == MM
            assert got["outsider"] is True and got["message"]["outsider"] is True
            await d.silent(babel)
            await asyncio.sleep(1)
            assert await asyncio.to_thread(_kiosk_answers, e2e, again) == []

            # Past the expiry (the injected clock): the next delivery uses crew. The kiosk
            # answers, babel's doorbell stays silent.
            e2e.clock.now += timedelta(minutes=1, seconds=1)
            late = await asyncio.to_thread(
                e2e.post, e2e.outsider_token, f"@{babel} still there?", channel
            )
            answers = await wait_answers(e2e.mattermost, late, 1)
            assert answers[0]["user_id"] == d.ids[babel]
            await d.silent(babel)

            # The broker settles the lapse on its next switch_state, and tells the partner.
            await d.cache.refresh(oaw.name, SID)
            state = d.cache.switch_state(oaw.name, SID)
            assert state is not None and state.mode == "crew" and state.expires_at is None
            deadline = time.monotonic() + REPLY_TIMEOUT_S
            while not any(
                p["message"] == "social lapsed → crew"
                for p in await asyncio.to_thread(e2e.broker_posts, oaw)
            ):
                assert time.monotonic() < deadline, "the partner was never told social lapsed"
                await asyncio.sleep(0.3)

    asyncio.run(scenario())


def _kiosk_answers(e2e: World, root: str) -> list[str]:
    return [str(p["id"]) for p in kiosk_answers(e2e.mattermost, root)]
