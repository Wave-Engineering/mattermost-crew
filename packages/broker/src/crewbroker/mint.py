"""Account names, bot accounts and tokens (Dev Spec §5.6; R-01, R-03, R-05, R-07; CT-05)."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from crewbroker.mattermost import MattermostAPI, MattermostError, NotFound
from crewbroker.state import AccountStore, TokenRecord, TokenStore

log = logging.getLogger(__name__)

ACCOUNT_NAME_MAX = 64  # Mattermost's UserNameMaxLength (CT-05)
ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9._-]+$")
DEFAULT_TTL_HOURS = 26
BOT_DESCRIPTION = "mattermost-crew participant"
PROBE_DESCRIPTION = "crewbroker native-expiry probe"
PROBE_TTL_MS = 3_600_000
SUPERSEDE_PER_CALL = 5


class SquattedAccount(Exception):
    """An account with this name exists and is not one this broker minted and still owns."""


def account_name(dev_name: str, crew: str) -> str:
    """`<dev_name>.<crew>`: at most 64 characters, from a-z 0-9 . _ - (CT-05)."""
    name = f"{dev_name}.{crew}"
    if not dev_name or not crew:
        raise ValueError("dev_name and crew must both be non-empty")
    if len(name) > ACCOUNT_NAME_MAX:
        raise ValueError(f"account name {name!r} is longer than {ACCOUNT_NAME_MAX} characters")
    if not ACCOUNT_NAME_RE.fullmatch(name):
        raise ValueError(f"account name {name!r} has characters outside a-z0-9._-")
    return name


def ensure_bot(
    client: MattermostAPI, name: str, accounts: AccountStore, broker_user_id: str
) -> str:
    """Return the user ID of bot account `name`, creating it on first use (R-03).

    An existing account is used only if it is a bot, is on this broker's `minted_bot_ids`,
    and this broker can read it as its owner. The broker can read `owner_id` only for bots it
    owns (D-100), so a 404 from `GET /bots/{id}` is squatted, never "not a bot, so create
    it". A pruned (disabled) bot of ours is re-enabled, so a pruned agent can register again.
    """
    user = client.get_user_by_username(name)
    if user is None:
        bot = client.create_bot(name, name, BOT_DESCRIPTION)
        bot_id = str(bot["user_id"])
        accounts.add_minted(name, bot_id)
        return bot_id

    user_id = str(user["id"])
    if not user.get("is_bot"):
        raise SquattedAccount(f"{name} is held by a human user ({user_id})")
    if user_id not in accounts.minted_bot_ids():
        raise SquattedAccount(f"{name} ({user_id}) was not minted by this broker")
    try:
        bot = client.get_bot(user_id)
    except NotFound as exc:
        raise SquattedAccount(f"{name} ({user_id}) is not owned by this broker") from exc
    if bot.get("owner_id") != broker_user_id:
        raise SquattedAccount(f"{name} ({user_id}) is owned by {bot.get('owner_id')}")
    if bot.get("delete_at") or user.get("delete_at"):
        client.enable_bot(user_id)
        accounts.clear_pruned(name)
    return user_id


# The capability probe's results, per server (R-07). Keyed by the server's URL.
_expiry_capability: dict[str, bool] = {}
_expiry_lock = threading.Lock()


def native_expiry_supported(client: MattermostAPI, server_key: str, broker_user_id: str) -> bool:
    """Does this server honour `expires_at` on a new access token? Probed once per server.

    The probe creates a short-lived token on the broker's own account with `expires_at` set,
    reads whether the server echoes it (11.8 omits the key; 11.11 returns it), and revokes it.
    """
    with _expiry_lock:
        cached = _expiry_capability.get(server_key)
        if cached is not None:
            return cached
        wanted = int(time.time() * 1000) + PROBE_TTL_MS
        token = client.create_token(broker_user_id, PROBE_DESCRIPTION, expires_at_ms=wanted)
        try:
            supported = token.get("expires_at") == wanted
        finally:
            client.revoke_token(str(token["id"]))
        _expiry_capability[server_key] = supported
        return supported


def clear_expiry_cache() -> None:
    with _expiry_lock:
        _expiry_capability.clear()


@dataclass(frozen=True)
class Token:
    token: str
    token_id: str
    user_id: str
    created_at: datetime
    expires_at: int | None  # epoch ms; None where the server has no native expiry


def mint_token(
    client: MattermostAPI,
    bot_user_id: str,
    tokens: TokenStore,
    *,
    expiry_supported: bool,
    now: datetime,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> Token:
    """Create an access token for `bot_user_id` and record its id and creation time.

    Where the server supports native expiry (`native_expiry_supported`), the token expires
    `ttl_hours` from now by the server's clock. Otherwise the broker's revocation is the
    expiry (R-07).
    """
    expires_at = int(time.time() * 1000) + ttl_hours * 3_600_000 if expiry_supported else None
    created = client.create_token(bot_user_id, "crewbroker", expires_at_ms=expires_at)
    token = Token(
        token=str(created["token"]),
        token_id=str(created["id"]),
        user_id=bot_user_id,
        created_at=now,
        expires_at=expires_at,
    )
    tokens.record(
        TokenRecord(
            token_id=token.token_id,
            user_id=bot_user_id,
            created_at=now,
            expires_at=expires_at,
        )
    )
    return token


def revoke_superseded(
    client: MattermostAPI,
    bot_user_id: str,
    tokens: TokenStore,
    keep: set[str],
    limit: int = SUPERSEDE_PER_CALL,
) -> list[str]:
    """Revoke recorded tokens of `bot_user_id` not in `keep`, at most `limit`; return their ids.

    `mint` and `remint` call this with the token the daemon says it holds and the one just
    minted, so a token the daemon never learned of (its call timed out, #41) is revoked on
    its next successful call. `limit` keeps a backlog from holding the reply past the
    daemon's timeout; the rest go on later calls. An upstream 404 means already gone and
    clears the entry; any other error keeps it for the next call.
    """
    revoked: list[str] = []
    stale = [r for r in tokens.all() if r.user_id == bot_user_id and r.token_id not in keep]
    for rec in stale[:limit]:
        try:
            client.revoke_token(rec.token_id)
        except NotFound:
            log.warning("token %s of %s is already gone upstream", rec.token_id, bot_user_id)
        except MattermostError as exc:
            log.error("revoke superseded %s of %s: %s", rec.token_id, bot_user_id, exc)
            continue
        tokens.remove(rec.token_id)
        revoked.append(rec.token_id)
    return revoked


def record_register(accounts: AccountStore, account: str, now: datetime) -> None:
    """Write `last_register` for `account`. Only `mint` calls it; `remint` never does."""
    accounts.record_register(account, now)
