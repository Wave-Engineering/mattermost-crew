"""Deactivate bot accounts that have not registered in 30 days (R-30).

Only `register` (through `mint`) counts as activity. Rotation is the daemon's job (Story
1.4), and a `remint` never touches `last_register`, so an account that is re-minted daily
but never registered still ages out.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from crewbroker.mattermost import MattermostAPI, MattermostError, NotFound
from crewbroker.state import AccountStore

DEFAULT_IDLE_DAYS = 30

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
        except MattermostError as exc:
            # Left unmarked, so the next daily run retries it; one bad account must not stop
            # the accounts after it from being pruned (R-30).
            log.error("prune: disabling %s failed: %s; will retry", acct.name, exc)
            continue
        accounts.mark_pruned(acct.name, now)
        pruned.append(acct.name)
        log.info("prune: deactivated %s, last registered %s", acct.name, acct.last_register)
    return pruned
