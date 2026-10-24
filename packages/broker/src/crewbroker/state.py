"""The broker's state files under `/var/lib/crewbroker/<crew>-<server_id>/`.

- `accounts.json`: `minted_bot_ids` (every bot account this broker created) and, per account
  name, its `bot_user_id`, `last_register` and `pruned_at`.
- `tokens.json`: the token-id ledger. Token ids, owners and times only; never a token.

Every file is written whole, through a mode-`0600` temporary file and an atomic rename, so a
crash never leaves a half-written file and no other user can ever read one (R-08).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

FILE_MODE = 0o600
DIR_MODE = 0o700


def write_private_json(path: Path, data: Any) -> None:
    """Write `data` to `path` as JSON, mode 0600, atomically."""
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return default
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


@dataclass(frozen=True)
class Account:
    name: str
    bot_user_id: str
    last_register: datetime | None
    pruned_at: datetime | None


def _ts(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


class AccountStore:
    """`accounts.json`: the bot accounts this broker created, and when each last registered."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "accounts.json"

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path, {"minted_bot_ids": [], "accounts": {}})
        data.setdefault("minted_bot_ids", [])
        data.setdefault("accounts", {})
        return data

    def minted_bot_ids(self) -> list[str]:
        return list(self._load()["minted_bot_ids"])

    def add_minted(self, name: str, bot_user_id: str) -> None:
        data = self._load()
        if bot_user_id not in data["minted_bot_ids"]:
            data["minted_bot_ids"].append(bot_user_id)
        data["accounts"].setdefault(name, {})["bot_user_id"] = bot_user_id
        write_private_json(self.path, data)

    def get(self, name: str) -> Account | None:
        entry = self._load()["accounts"].get(name)
        if entry is None:
            return None
        return Account(
            name=name,
            bot_user_id=entry["bot_user_id"],
            last_register=_ts(entry.get("last_register")),
            pruned_at=_ts(entry.get("pruned_at")),
        )

    def all(self) -> list[Account]:
        names = sorted(self._load()["accounts"])
        return [acct for acct in (self.get(n) for n in names) if acct is not None]

    def record_register(self, account: str, now: datetime) -> None:
        """Write `last_register` for `account`. Only `mint` calls this; `remint` never does."""
        data = self._load()
        entry = data["accounts"].get(account)
        if entry is None:
            raise KeyError(f"unknown account {account!r}")
        entry["last_register"] = now.isoformat()
        write_private_json(self.path, data)

    def mark_pruned(self, account: str, now: datetime) -> None:
        data = self._load()
        data["accounts"][account]["pruned_at"] = now.isoformat()
        write_private_json(self.path, data)

    def clear_pruned(self, account: str) -> None:
        data = self._load()
        entry = data["accounts"].get(account)
        if entry is not None and entry.pop("pruned_at", None) is not None:
            write_private_json(self.path, data)


@dataclass(frozen=True)
class TokenRecord:
    token_id: str
    user_id: str
    created_at: datetime
    expires_at: int | None  # epoch milliseconds, when native expiry was set


class TokenStore:
    """`tokens.json`: the id and creation time of every live token this broker minted."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "tokens.json"

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path, {"tokens": {}})
        data.setdefault("tokens", {})
        return data

    def record(self, rec: TokenRecord) -> None:
        data = self._load()
        data["tokens"][rec.token_id] = {
            "user_id": rec.user_id,
            "created_at": rec.created_at.isoformat(),
            "expires_at": rec.expires_at,
        }
        write_private_json(self.path, data)

    def get(self, token_id: str) -> TokenRecord | None:
        entry = self._load()["tokens"].get(token_id)
        if entry is None:
            return None
        return TokenRecord(
            token_id=token_id,
            user_id=entry["user_id"],
            created_at=datetime.fromisoformat(entry["created_at"]),
            expires_at=entry.get("expires_at"),
        )

    def remove(self, token_id: str) -> None:
        data = self._load()
        if data["tokens"].pop(token_id, None) is not None:
            write_private_json(self.path, data)
