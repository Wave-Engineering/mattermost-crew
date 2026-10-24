"""Deactivate bot accounts that have not registered in 30 days (R-30).

Only `register` (through `mint`) counts as activity. Rotation is the daemon's job (Story
1.4), and a `remint` never touches `last_register`, so an account that is re-minted daily
but never registered still ages out.

The same daily job reconciles `tokens.json` (`reconcile_tokens`): a token the daemon never
learned of, because its `mint` or `remint` timed out on the daemon's side, would otherwise
stay live forever on a server with no native expiry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from crewbroker.mattermost import MattermostAPI, MattermostError, NotFound
from crewbroker.state import AccountStore, TokenRecord, TokenStore

DEFAULT_IDLE_DAYS = 30
# The daemon rotates at startup and every 24 h, so the token an agent uses is never older than
# that plus a swap. A token that is not its bot's newest and is older than this is one the
# daemon will never revoke: most often a mint whose reply the daemon timed out on (#41).
ORPHAN_AGE = timedelta(hours=27)

log = logging.getLogger(__name__)


def prune_idle(
    client: MattermostAPI,
    accounts: AccountStore,
    now: datetime,
    idle_days: int = DEFAULT_IDLE_DAYS,
) -> list[str]:
    """Disable every account whose `last_register` is more than `idle_days` old.

    Returns the names deactivated. An account already pruned is left alone until
    `ensure_bot` reactivates it.
    """
    cutoff = now - timedelta(days=idle_days)
    pruned: list[str] = []
    for acct in accounts.all():
        if acct.pruned_at is not None or acct.last_register is None:
            continue
        if acct.last_register >= cutoff:
            continue
        try:
            client.disable_bot(acct.bot_user_id)
        except NotFound:
            # Reassigned away, or deleted: no longer ours to disable. Record it as pruned so
            # the daily job doesn't retry it forever; ensure_bot treats it as squatted.
            log.warning("prune: %s (%s) is no longer this broker's", acct.name, acct.bot_user_id)
        accounts.mark_pruned(acct.name, now)
        pruned.append(acct.name)
        log.info("prune: deactivated %s, last registered %s", acct.name, acct.last_register)
    return pruned


def reconcile_tokens(
    client: MattermostAPI,
    tokens: TokenStore,
    now: datetime,
    max_age: timedelta = ORPHAN_AGE,
) -> list[str]:
    """Revoke every token that is not its bot's newest and is older than `max_age`.

    Each bot's newest token is always kept, however old: the daemon may be down, and that
    token is the one it will use when it comes back. Returns the token ids revoked. An
    upstream 404 means the token is already gone and clears the entry. Any other error keeps
    the entry for the next day's run.
    """
    by_bot: dict[str, list[TokenRecord]] = {}
    for rec in tokens.all():
        by_bot.setdefault(rec.user_id, []).append(rec)
    cutoff = now - max_age
    revoked: list[str] = []
    for recs in by_bot.values():
        recs.sort(key=lambda r: r.created_at)
        for rec in recs[:-1]:
            if rec.created_at >= cutoff:
                continue
            try:
                client.revoke_token(rec.token_id)
            except NotFound:
                log.warning("reconcile: token %s is already gone upstream", rec.token_id)
            except MattermostError as exc:
                log.error("reconcile: revoke %s failed: %s; will retry", rec.token_id, exc)
                continue
            tokens.remove(rec.token_id)
            revoked.append(rec.token_id)
            log.info("reconcile: revoked orphan token %s, minted %s", rec.token_id, rec.created_at)
    return revoked
