"""Story 1.3: account names, ensure_bot, mint_token and record_register."""

from __future__ import annotations

import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from crewbroker.mint import (
    SquattedAccount,
    account_name,
    ensure_bot,
    mint_token,
    native_expiry_supported,
)
from crewbroker.service import Broker, OpError
from crewbroker.state import AccountStore, TokenStore

BROKER_ID = "broker-user-id"


def test_account_name_format_and_limits() -> None:
    assert account_name("babelfish", "oaw") == "babelfish.oaw"
    assert account_name("a-b_c", "x.y") == "a-b_c.x.y"
    assert len(account_name("d" * 60, "oaw")) == 64
    with pytest.raises(ValueError, match="longer than 64"):
        account_name("d" * 61, "oaw")
    for dev, crew in [("Babel", "oaw"), ("babel fish", "oaw"), ("babel", "o@w"), ("", "oaw")]:
        with pytest.raises(ValueError):
            account_name(dev, crew)


def test_ensure_bot_creates_once(fake: Any, tmp_path: Path) -> None:
    accounts = AccountStore(tmp_path)
    first = ensure_bot(fake, "babel.oaw", accounts, BROKER_ID)
    second = ensure_bot(fake, "babel.oaw", accounts, BROKER_ID)
    assert first == second
    assert fake.count("create_bot") == 1
    assert accounts.minted_bot_ids() == [first]
    assert stat.S_IMODE(accounts.path.stat().st_mode) == 0o600


@pytest.mark.parametrize("supported", [True, False])
def test_mint_sets_expiry_when_supported(fake: Any, tmp_path: Path, supported: bool) -> None:
    fake.supports_expiry = supported
    probe = native_expiry_supported(fake, "http://mm.invalid", BROKER_ID)
    assert probe is supported
    # The probe's own token is revoked, and the result is cached per server.
    probe_tokens = [t for t in fake.tokens.values() if t["user_id"] == BROKER_ID]
    assert len(probe_tokens) == 1 and probe_tokens[0]["revoked"]
    assert native_expiry_supported(fake, "http://mm.invalid", BROKER_ID) is supported
    assert fake.count("create_token") == 1

    accounts, tokens = AccountStore(tmp_path), TokenStore(tmp_path)
    bot_id = ensure_bot(fake, "babel.oaw", accounts, BROKER_ID)
    from datetime import UTC, datetime

    now = datetime(2026, 10, 1, tzinfo=UTC)
    token = mint_token(fake, bot_id, tokens, expiry_supported=probe, now=now)
    server_side = fake.tokens[token.token_id]
    if supported:
        assert token.expires_at is not None
        assert server_side["expires_at"] == token.expires_at
    else:
        assert token.expires_at is None
        assert "expires_at" not in server_side
    assert fake.token_works(token.token)
    rec = tokens.get(token.token_id)
    assert rec is not None and rec.created_at == now and rec.user_id == bot_id
    assert "secret" not in tokens.path.read_text()
    assert stat.S_IMODE(tokens.path.stat().st_mode) == 0o600


def test_pruned_agent_reactivated_on_register(broker: Broker, fake: Any, clock: Any) -> None:
    first = broker.mint({"dev_name": "babel", "crew": "oaw"})
    bot_id = fake.users[next(u for u in fake.users if fake.users[u]["username"] == "babel.oaw")][
        "id"
    ]
    clock.now += timedelta(days=31)
    assert broker.prune() == ["babel.oaw"]
    assert fake.users[bot_id]["delete_at"]
    assert not fake.token_works(first["token"])

    again = broker.mint({"dev_name": "babel", "crew": "oaw"})
    assert again["account"] == "babel.oaw"
    assert fake.count("create_bot") == 1
    assert fake.count("enable_bot") == 1
    assert fake.token_works(again["token"])
    acct = broker.accounts.get("babel.oaw")
    assert acct is not None and acct.pruned_at is None and acct.last_register == clock.now


def test_ensure_bot_rejects_squatted_name(broker: Broker, fake: Any) -> None:
    # A human user holding the name.
    fake.add_user("human.oaw")
    # Another owner's bot: the broker's GET /bots/{id} is 404.
    fake.add_user("other.oaw", is_bot=True, owner_id="someone-else")
    # The broker's own bot, but not one it minted (not on minted_bot_ids).
    fake.add_user("stray.oaw", is_bot=True, owner_id=BROKER_ID)
    tokens_before = len(fake.tokens)
    for dev in ("human", "other", "stray"):
        with pytest.raises(OpError, match="squatted"):
            broker.mint({"dev_name": dev, "crew": "oaw"})
        with pytest.raises(SquattedAccount):
            ensure_bot(fake, f"{dev}.oaw", broker.accounts, BROKER_ID)
    assert fake.count("create_bot") == 0
    assert len(fake.tokens) == tokens_before
    assert broker.accounts.minted_bot_ids() == []


def test_ensure_bot_rejects_minted_bot_now_owned_elsewhere(fake: Any, tmp_path: Path) -> None:
    """On minted_bot_ids, but reassigned: the 404 is squatted, never a reason to create."""
    accounts = AccountStore(tmp_path)
    bot_id = ensure_bot(fake, "babel.oaw", accounts, BROKER_ID)
    fake.reassign(bot_id, "someone-else")
    with pytest.raises(SquattedAccount):
        ensure_bot(fake, "babel.oaw", accounts, BROKER_ID)
    assert fake.count("create_bot") == 1


def test_remint_does_not_record_register(broker: Broker, clock: Any) -> None:
    broker.mint({"dev_name": "babel", "crew": "oaw"})
    registered = clock.now
    clock.now += timedelta(days=1)
    broker.remint({"dev_name": "babel", "crew": "oaw"})
    acct = broker.accounts.get("babel.oaw")
    assert acct is not None and acct.last_register == registered
    clock.now += timedelta(days=1)
    broker.mint({"dev_name": "babel", "crew": "oaw"})
    acct = broker.accounts.get("babel.oaw")
    assert acct is not None and acct.last_register == clock.now
