"""The E2E world itself (Story 3.8): two crews, their brokers, the hung third, the outsiders."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from crewchat.config import load_config
from crewchat.filters import SwitchCache
from crewchat.tokens import BrokerClient, BrokerUnavailable, broker_clients

from tests.e2e.conftest import BROKER_TIMEOUT_S, SID, Daemon, World
from tests.integration.mmhttp import mm_request


def test_e2e_fixture_builds_two_crews(e2e: World) -> None:
    oaw, synth = e2e.crews["oaw"], e2e.crews["synth"]
    assert (oaw.name, synth.name) == (f"oaw{e2e.run}", f"synth{e2e.run}")
    assert oaw.partner_id != synth.partner_id  # each crew has its own human
    assert (oaw.instance, synth.instance) == (f"{oaw.name}-{SID}", f"{synth.name}-{SID}")

    # servers.toml names all three brokers, each crew's own table.
    config = load_config(e2e.servers_toml)
    assert set(config.crews) == {oaw.name, synth.name, e2e.hung_crew}
    for crew in (oaw.name, synth.name, e2e.hung_crew):
        assert set(config.crews[crew].brokers) == {SID}
    assert config.crews[oaw.name].brokers[SID].broker_socket == oaw.socket
    assert config.crews[synth.name].brokers[SID].broker_socket == synth.socket
    assert config.crews[e2e.hung_crew].brokers[SID].broker_socket == e2e.hung.path

    async def scenario() -> None:
        # Each broker answers on its own socket with its own crew, trusting the other by name.
        for crew, other in ((oaw, synth), (synth, oaw)):
            info = await BrokerClient(crew.socket, BROKER_TIMEOUT_S).crew_info()
            assert info["broker_user_id"] == crew.broker_id
            assert info["human_user_ids"] == [crew.partner_id]
            assert info["trusted"] == [other.name, e2e.hung_crew]
            assert e2e.outsider_id not in info["human_user_ids"]
            assert e2e.spoof_id not in info["minted_bot_ids"]
        # The third broker accepts and never answers.
        with pytest.raises(BrokerUnavailable):
            await BrokerClient(e2e.hung.path, 0.5).crew_info()

        # A hung broker never stalls the others: with every broker asked at once, both real
        # crews resolve while the hung one is still being waited on.
        cache = SwitchCache.from_config(broker_clients(config), config)
        started = time.monotonic()
        refresh = asyncio.create_task(cache.refresh_all())
        while cache.crew_info(oaw.name, SID) is None or cache.crew_info(synth.name, SID) is None:
            assert not refresh.done(), "the hung broker answered, or the refresh gave up early"
            assert time.monotonic() - started < BROKER_TIMEOUT_S / 2, "a real broker stalled"
            await asyncio.sleep(0.05)
        assert not refresh.done()
        await refresh
        assert time.monotonic() - started < BROKER_TIMEOUT_S + 2  # one timeout, not a sum
        assert cache.crew_info(e2e.hung_crew, SID) is None
        assert cache.switch_state(e2e.hung_crew, SID) is None
        assert cache.switch_state(oaw.name, SID) is not None

        # Two registered agents per crew, through the daemon's socket: each crew's broker
        # lists exactly its own as minted.
        async with Daemon(e2e) as d:
            channel = await asyncio.to_thread(e2e.channel, "fixture")
            for crew in (oaw, synth):
                assert len(crew.agents) == 2
                for dev in crew.agents:
                    await d.register(dev, crew, [channel])
            await d.cache.refresh(oaw.name, SID)
            await d.cache.refresh(synth.name, SID)
            for crew, other in ((oaw, synth), (synth, oaw)):
                cached = d.cache.crew_info(crew.name, SID)
                assert cached is not None
                minted = set(cached["minted_bot_ids"])
                assert {d.ids[dev] for dev in crew.agents} <= minted
                assert not {d.ids[dev] for dev in other.agents} & minted

    asyncio.run(scenario())

    # The outsiders: an unlisted human, and a bot named like an `oaw` agent that the `oaw`
    # broker does not own.
    spoof = mm_request(e2e.mattermost, "GET", f"/users/{e2e.spoof_id}").json()
    assert spoof["username"] == f"x.{oaw.name}" and spoof["is_bot"] is True
    bot: dict[str, Any] = mm_request(e2e.mattermost, "GET", f"/bots/{e2e.spoof_id}").json()
    assert bot["owner_id"] not in (oaw.broker_id, synth.broker_id)
    outsider = mm_request(e2e.mattermost, "GET", f"/users/{e2e.outsider_id}").json()
    assert outsider.get("is_bot") is not True
    assert e2e.outsider_id not in (oaw.partner_id, synth.partner_id)
