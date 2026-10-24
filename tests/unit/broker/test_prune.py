"""Story 1.3: prune_idle counts only register, never remint (R-30)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from crewbroker.prune import reconcile_tokens
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


def _mint_at(broker: Broker, clock: Any, start: Any, hours: float, op: str = "remint") -> str:
    clock.now = start + timedelta(hours=hours)
    return str(getattr(broker, op)({"dev_name": "babel", "crew": "oaw"})["token_id"])


def test_reconcile_revokes_orphan_older_than_max_age(broker: Broker, fake: Any, clock: Any) -> None:
    start = clock.now
    a = _mint_at(broker, clock, start, 0, "mint")
    b = _mint_at(broker, clock, start, 2)  # a remint whose reply the daemon timed out on
    c = _mint_at(broker, clock, start, 28)  # not the newest, but only 2 h old at the run
    d = _mint_at(broker, clock, start, 29)  # the newest
    clock.now = start + timedelta(hours=30)

    assert reconcile_tokens(fake, broker.tokens, clock.now) == [a, b]
    assert fake.tokens[a]["revoked"] and fake.tokens[b]["revoked"]
    assert {r.token_id for r in broker.tokens.all()} == {c, d}
    assert not fake.tokens[c]["revoked"] and not fake.tokens[d]["revoked"]


def test_reconcile_keeps_newest_even_if_old(broker: Broker, fake: Any, clock: Any) -> None:
    only = broker.mint({"dev_name": "babel", "crew": "oaw"})["token_id"]
    clock.now += timedelta(hours=40)
    before = fake.count("revoke_token")  # the expiry probe revokes its own canary
    assert reconcile_tokens(fake, broker.tokens, clock.now) == []
    assert broker.tokens.get(only) is not None
    assert fake.count("revoke_token") == before


def test_reconcile_notfound_removes_entry(broker: Broker, fake: Any, clock: Any) -> None:
    start = clock.now
    gone = _mint_at(broker, clock, start, 0, "mint")
    failing = _mint_at(broker, clock, start, 1)
    later = _mint_at(broker, clock, start, 2)
    _mint_at(broker, clock, start, 3)  # the newest
    del fake.tokens[gone]  # revoked on the server by someone else
    fake.revoke_fails.add(failing)
    clock.now = start + timedelta(hours=40)

    # The 404 clears its entry; the 500 keeps its entry and the run goes on to the next one.
    assert reconcile_tokens(fake, broker.tokens, clock.now) == [gone, later]
    assert broker.tokens.get(gone) is None
    assert broker.tokens.get(failing) is not None
    assert broker.tokens.get(later) is None
    # Retried, and cleared, on the next day's run.
    fake.revoke_fails.clear()
    clock.now += timedelta(days=1)
    assert reconcile_tokens(fake, broker.tokens, clock.now) == [failing]


def test_prune_runs_reconcile(broker: Broker, fake: Any, clock: Any) -> None:
    orphan = broker.mint({"dev_name": "babel", "crew": "oaw"})["token_id"]
    clock.now += timedelta(hours=1)
    broker.remint({"dev_name": "babel", "crew": "oaw"})
    clock.now += timedelta(hours=27)
    assert broker.prune() == []
    assert fake.tokens[orphan]["revoked"]
    assert broker.tokens.get(orphan) is None
