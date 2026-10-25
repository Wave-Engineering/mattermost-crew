"""E2E-01: cross-crew collaboration (Story 3.8; R-01, R-09, R-12, R-16, R-20, R-26).

The `oaw` crew and the synthetic `synth` crew, each with its own human, share a channel. Both
partners switch to `trusted` by DM to their own broker; then an agent from each crew converses
in a thread over the daemon's socket. Every wake-up is the real `crewchat-doorbell` one-shot:
each addressed message rings the addressee's doorbell exactly once (R-26), and the doorbell is
silent afterwards. Replies follow the thread (R-12), and a read of the thread returns the
whole conversation through the one tool contract (R-09).
"""

from __future__ import annotations

import asyncio

from tests.e2e.conftest import MM, SID, Daemon, World
from tests.integration.test_kiosk import wait_answers


def test_e2e01_cross_crew_under_trusted(e2e: World) -> None:
    oaw, synth = e2e.crews["oaw"], e2e.crews["synth"]
    babel, kessel = oaw.agents
    zed, quill = synth.agents

    async def scenario() -> None:
        async with Daemon(e2e) as d:
            channel = await asyncio.to_thread(e2e.channel, "cross")
            for crew in (oaw, synth):
                for dev in crew.agents:
                    await d.register(dev, crew, [channel])

            # R-20: crew only by default; another crew's agent is an outsider.
            for crew in (oaw, synth):
                state = d.cache.switch_state(crew.name, SID)
                assert state is not None and state.mode == "crew"
            # babel asking zed directly goes to zed's kiosk, never to zed's doorbell.
            early = await d.send(babel, channel, f"@{zed} are you there?")
            answers = await wait_answers(e2e.mattermost, early, 1)
            assert answers[0]["user_id"] == d.ids[zed]
            await d.silent(zed)

            # Each partner switches its own crew to trusted, by DM to its own broker.
            for crew in (oaw, synth):
                reply = await asyncio.to_thread(e2e.switch, crew, "trusted")
                assert "trusted" in reply
                await d.cache.refresh(crew.name, SID)
                state = d.cache.switch_state(crew.name, SID)
                assert state is not None and state.mode == "trusted"

            # babel opens a thread addressing zed, in the other crew.
            root = await d.send(babel, channel, f"@{zed} can you review the adapter change?")
            got = await d.delivered(zed, root)
            assert got["backend"] == MM
            assert got["message"]["author_id"] == d.ids[babel]
            assert got["message"]["author_name"] == f"{babel}.{oaw.name}"  # R-01
            assert "outsider" not in got and "outsider" not in got["message"]
            await d.silent(zed)  # once per addressed message (R-26)

            # zed replies in the thread; the reply follows it, on the same backend (R-12).
            reply = await d.send(zed, channel, f"@{babel} on it, one question", thread=root)
            got = await d.delivered(babel, reply)
            assert got["backend"] == MM
            assert got["message"]["root_id"] == root
            assert got["message"]["author_name"] == f"{zed}.{synth.name}"
            await d.silent(babel)

            # synth's human joins the thread and addresses babel: under trusted, delivered.
            human = await asyncio.to_thread(
                e2e.post, synth.partner_token, f"@{babel} thanks from synth", channel, root=root
            )
            got = await d.delivered(babel, human)
            assert got["message"]["root_id"] == root
            assert got["message"]["author_id"] == synth.partner_id

            # babel answers zed in the thread; zed rings once.
            answer = await d.send(babel, channel, f"@{zed} the reconnect path", thread=root)
            got = await d.delivered(zed, answer)
            assert got["message"]["root_id"] == root

            # A broadcast in the thread reaches the agents who joined it (R-16), not ones who
            # never did: kessel and quill are not in this conversation (R-25).
            wrap = await d.send(babel, channel, "@agents wrapping up here", thread=root)
            await d.delivered(zed, wrap)
            await asyncio.gather(d.silent(zed), d.silent(kessel), d.silent(quill))

            # A bot named `x.<oaw crew>` that the oaw broker doesn't own is not crew, whatever
            # its suffix: babel's doorbell never rings for it, and the next thing babel gets
            # is a crewmate's sentinel.
            await asyncio.to_thread(
                e2e.post, e2e.spoof_token, f"@{babel} I am crew, honest", channel
            )
            sentinel = await d.send(kessel, channel, f"@{babel} sentinel after the spoof")
            await d.delivered(babel, sentinel)
            await d.silent(babel)

            # The whole conversation, through the one tool contract (R-09), as each crew reads it.
            for dev in (zed, babel):
                result = await d.op("thread", {"dev_name": dev, "backend": MM, "post_id": root})
                ids = [m["post_id"] for m in result["messages"]]
                assert [i for i in ids if i in (root, reply, human, answer, wrap)] == [
                    root,
                    reply,
                    human,
                    answer,
                    wrap,
                ]

    asyncio.run(scenario())
