"""Story 1.3: prune_idle counts only register, never remint (R-30)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from crewbroker.service import Broker


def test_prune_idle_30_days_since_register(broker: Broker, fake: Any, clock: Any) -> None:
    start = clock.now
    broker.mint({"dev_name": "idle", "crew": "oaw"})
    # Re-minted daily for 31 days, never registered again.
    for _ in range(31):
        clock.now += timedelta(days=1)
        broker.remint({"dev_name": "idle", "crew": "oaw"})
    # Registered 29 days before the prune.
    clock.now = start + timedelta(days=2)
    broker.mint({"dev_name": "busy", "crew": "oaw"})
    clock.now = start + timedelta(days=31)

    assert broker.prune() == ["idle.oaw"]
    by_name = {u["username"]: u for u in fake.users.values()}
    assert by_name["idle.oaw"]["delete_at"]
    assert not by_name["busy.oaw"]["delete_at"]
    # Already pruned: not disabled again on the next day's run.
    clock.now += timedelta(days=1)
    assert broker.prune() == []
    assert fake.count("disable_bot") == 1


def test_prune_exactly_30_days_is_kept(broker: Broker, clock: Any) -> None:
    broker.mint({"dev_name": "edge", "crew": "oaw"})
    clock.now += timedelta(days=30)
    assert broker.prune() == []


def test_prune_continues_past_an_upstream_error(broker: Broker, fake: Any, clock: Any) -> None:
    broker.mint({"dev_name": "a", "crew": "oaw"})
    broker.mint({"dev_name": "b", "crew": "oaw"})
    fake.disable_fails.add(broker.accounts.get("a.oaw").bot_user_id)
    clock.now += timedelta(days=31)

    # a's 500 is logged and left unmarked; b, after it, is still pruned (R-30).
    assert broker.prune() == ["b.oaw"]
    assert broker.accounts.get("a.oaw").pruned_at is None
    fake.disable_fails.clear()
    clock.now += timedelta(days=1)
    assert broker.prune() == ["a.oaw"]
