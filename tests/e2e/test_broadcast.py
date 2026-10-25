"""E2E-03: a fleet broadcast (Story 3.8; R-16, R-25).

Thirty agents of the `oaw` crew register through the daemon's socket and sit in one channel.
The crew's human partner posts `@agents`: every one of the thirty doorbells rings exactly once
with that post (R-16), and none rings again (R-25's loop guard admits a human's addressed
message once per agent; nothing echoes). Throughout the broadcast the server sends the daemon
and the brokers no `429`, under the CI leg's configured limits (per user, 10/s, burst 50;
CT-06). Registration itself goes through one broker account and may meet the limit; the broker
waits those out, and they are not part of the broadcast's count.
"""

from __future__ import annotations

import asyncio

from tests.e2e.conftest import MM, SID, Daemon, World

FLEET = 30
BURST_REFILL_S = 6.0


def test_e2e03_fleet_broadcast_within_rate_limit(e2e: World) -> None:
    oaw = e2e.crews["oaw"]
    fleet = [f"fleet{i:02d}" for i in range(FLEET)]

    async def scenario() -> None:
        async with Daemon(e2e) as d:
            channel = await asyncio.to_thread(e2e.channel, "fleet")
            for dev in fleet:
                await d.register(dev, oaw, [channel])
            assert len(d.registry.agents_on(oaw.name, SID)) == FLEET
            # Let registration's burst refill (50 at 10/s), so the count is the broadcast's.
            await asyncio.sleep(BURST_REFILL_S)

            before = e2e.counter.snapshot()
            post = await asyncio.to_thread(
                e2e.post, oaw.partner_token, "@agents roll call, please", channel
            )
            events = await asyncio.gather(*(d.ring(dev) for dev in fleet))
            for dev, event in zip(fleet, events, strict=True):
                assert event["type"] == "message", (dev, event)
                assert event["backend"] == MM
                assert event["message"]["post_id"] == post, dev
                assert event["message"]["author_id"] == oaw.partner_id
            await asyncio.gather(*(d.silent(dev) for dev in fleet))
            assert e2e.counter.since(before) == {}, "the server rate-limited the broadcast"

    asyncio.run(scenario())
