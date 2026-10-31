"""Story 1.3: account names, ensure_bot, mint_token and record_register."""

from __future__ import annotations

import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from crewbroker.avatar import identicon_png
from crewbroker.membership import DEADLINE_RESERVE_S
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


def test_create_writes_intent_first(
    fake: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#151: `pending_create[name]` is on disk before `create_bot` runs, and the description
    carries its nonce."""
    accounts = AccountStore(tmp_path)
    seen: list[str | None] = []
    create = fake.create_bot

    def create_bot(username: str, display_name: str, description: str) -> dict[str, Any]:
        seen.append(AccountStore(tmp_path).pending_nonce(username))  # read from disk
        return create(username, display_name, description)

    monkeypatch.setattr(fake, "create_bot", create_bot)
    bot_id = ensure_bot(fake, "babel.oaw", accounts, BROKER_ID)
    (nonce,) = seen
    assert nonce is not None
    assert fake.bots[bot_id]["description"].endswith(f"ref {nonce}")
    assert accounts.pending_nonce("babel.oaw") is None  # cleared by add_minted


def test_crash_between_create_and_record_is_adopted(
    fake: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#151: a crash after `create_bot` and before `add_minted` leaves a bot of ours that is
    not on `minted_bot_ids`. The next `ensure_bot` adopts it rather than calling it squatted."""
    accounts = AccountStore(tmp_path)

    def crash(name: str, bot_user_id: str) -> None:
        raise RuntimeError("the broker died here")

    monkeypatch.setattr(accounts, "add_minted", crash)
    with pytest.raises(RuntimeError):
        ensure_bot(fake, "babel.oaw", accounts, BROKER_ID)
    monkeypatch.undo()

    restarted = AccountStore(tmp_path)
    assert restarted.minted_bot_ids() == []
    bot_id = ensure_bot(fake, "babel.oaw", restarted, BROKER_ID)
    assert restarted.minted_bot_ids() == [bot_id]
    assert restarted.pending_nonce("babel.oaw") is None
    assert fake.count("create_bot") == 1
    assert any("adopted babel.oaw" in r.getMessage() for r in caplog.records)


def test_reassigned_bot_with_pending_name_stays_squatted(fake: Any, tmp_path: Path) -> None:
    """#151: a pending nonce exists, but the bot under that name lacks it (a bot reassigned to
    the broker account, IT-08): still squatted, never adopted."""
    accounts = AccountStore(tmp_path)
    accounts.begin_create("babel.oaw", "c0ffee00")
    fake.add_user(
        "babel.oaw", is_bot=True, owner_id=BROKER_ID, description="someone else's · ref 0"
    )
    with pytest.raises(SquattedAccount):
        ensure_bot(fake, "babel.oaw", accounts, BROKER_ID)
    assert accounts.minted_bot_ids() == []


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


def test_mint_sets_avatar_once(broker: Broker, fake: Any) -> None:
    """#151: the first `mint` sets the bot's identicon (D-098: only its owner can); the next
    one doesn't set it again."""
    broker.mint({"dev_name": "babel", "crew": "oaw"})
    acct = broker.accounts.get("babel.oaw")
    assert acct is not None
    assert fake.images[acct.bot_user_id] == identicon_png("babel.oaw")
    assert broker.accounts.avatar_set("babel.oaw")
    broker.mint({"dev_name": "babel", "crew": "oaw"})
    assert fake.count("set_user_image") == 1


def test_recreated_bot_gets_its_avatar(broker: Broker, fake: Any) -> None:
    """#151 review: a bot gone from the server (a dev-space wipe) is created again under the
    same name, and the new bot gets the avatar too: the flag belongs to the old bot id."""
    broker.mint({"dev_name": "babel", "crew": "oaw"})
    acct = broker.accounts.get("babel.oaw")
    assert acct is not None
    del fake.users[acct.bot_user_id], fake.bots[acct.bot_user_id]
    broker.mint({"dev_name": "babel", "crew": "oaw"})
    again = broker.accounts.get("babel.oaw")
    assert again is not None and again.bot_user_id != acct.bot_user_id
    assert fake.count("set_user_image") == 2
    assert fake.images[again.bot_user_id] == identicon_png("babel.oaw")


def test_avatar_failure_does_not_fail_mint(broker: Broker, fake: Any) -> None:
    """#151: `set_user_image` failing: the mint still returns its token, the flag stays unset,
    and the next mint tries again."""
    fake.image_fails = True
    minted = broker.mint({"dev_name": "babel", "crew": "oaw"})
    assert fake.token_works(minted["token"])
    assert not broker.accounts.avatar_set("babel.oaw")
    fake.image_fails = False
    broker.mint({"dev_name": "babel", "crew": "oaw"})
    assert fake.count("set_user_image") == 2
    assert broker.accounts.avatar_set("babel.oaw")


def test_avatar_skipped_near_the_deadline(broker: Broker, fake: Any, clock: Any) -> None:
    """#151: with less than `DEADLINE_RESERVE_S` left, the avatar waits for the next mint."""
    deadline = clock.now.timestamp() + DEADLINE_RESERVE_S / 2
    broker.mint({"dev_name": "babel", "crew": "oaw", "deadline": deadline})
    assert fake.count("set_user_image") == 0
    assert not broker.accounts.avatar_set("babel.oaw")
