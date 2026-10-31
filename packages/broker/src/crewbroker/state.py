"""The broker's state files under `/var/lib/crewbroker/<crew>-<server_id>/`.

- `accounts.json`: `minted_bot_ids` (every bot account this broker created); per account
  name, its `bot_user_id`, `last_register`, `pruned_at` and `avatar_set`; and
  `pending_create`, `{"<name>": "<nonce>"}`, the bots this broker is about to create. Each is
  written before `create_bot` and cleared by `add_minted`, so a crash between the two leaves
  the nonce the bot's description carries, and the bot is adopted rather than squatted
  (#151).
- `tokens.json`: the token-id ledger. Token ids, owners and times only; never a token.
- `dm_cursor.json`: `last_create_at`, the `create_at` in ms of the newest DM to the broker it
  has handled. On every websocket `hello` it backfills the DMs after it (#159). Beside it,
  `newest_partner_dm` (ms) and `handled` (the last 500 post ids), so a restart that replays
  DMs from a held cursor still knows which were overtaken and which were applied.

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
        data.setdefault("pending_create", {})
        return data

    def minted_bot_ids(self) -> list[str]:
        return list(self._load()["minted_bot_ids"])

    def add_minted(self, name: str, bot_user_id: str) -> None:
        data = self._load()
        if bot_user_id not in data["minted_bot_ids"]:
            data["minted_bot_ids"].append(bot_user_id)
        entry = data["accounts"].setdefault(name, {})
        if entry.get("bot_user_id") != bot_user_id:
            entry.pop("avatar_set", None)  # a new bot under the name: its avatar is unset
        entry["bot_user_id"] = bot_user_id
        data["pending_create"].pop(name, None)
        write_private_json(self.path, data)

    def begin_create(self, name: str, nonce: str) -> None:
        """Record that bot `name` is about to be created with `nonce` in its description."""
        data = self._load()
        data["pending_create"][name] = nonce
        write_private_json(self.path, data)

    def pending_nonce(self, name: str) -> str | None:
        nonce = self._load()["pending_create"].get(name)
        return nonce if isinstance(nonce, str) else None

    def avatar_set(self, name: str) -> bool:
        entry = self._load()["accounts"].get(name)
        return bool(entry and entry.get("avatar_set"))

    def mark_avatar_set(self, name: str) -> None:
        data = self._load()
        data["accounts"].setdefault(name, {})["avatar_set"] = True
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


def _token_record(token_id: str, entry: dict[str, Any]) -> TokenRecord:
    return TokenRecord(
        token_id=token_id,
        user_id=entry["user_id"],
        created_at=datetime.fromisoformat(entry["created_at"]),
        expires_at=entry.get("expires_at"),
    )


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
        return _token_record(token_id, entry)

    def all(self) -> list[TokenRecord]:
        return [_token_record(tid, entry) for tid, entry in sorted(self._load()["tokens"].items())]

    def remove(self, token_id: str) -> None:
        data = self._load()
        if data["tokens"].pop(token_id, None) is not None:
            write_private_json(self.path, data)


class DMCursorStore:
    """`dm_cursor.json`: where the DM backfill resumes, and what it has already seen (#159).

    The cursor holds behind DMs already applied while a backfill is owed, so a restart
    replays them. `newest_partner_dm` and `handled` survive it too: without them a replayed
    command overtaken before the restart would be judged as if nothing newer had come, and
    a DM already applied would be applied again.
    """

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "dm_cursor.json"

    def _read(self) -> dict[str, Any]:
        try:
            data = read_json(self.path, {})
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def load(self) -> int:
        """The cursor in ms; 0 when the file is missing or unreadable."""
        value = self._read().get("last_create_at")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    def load_seen(self) -> tuple[int, list[str]]:
        """The newest partner DM handled (ms) and the handled post ids, oldest first; 0 and
        none for anything missing or unreadable."""
        data = self._read()
        newest = data.get("newest_partner_dm")
        if isinstance(newest, bool) or not isinstance(newest, int) or newest < 0:
            newest = 0
        handled = data.get("handled")
        if not isinstance(handled, list):
            return newest, []
        return newest, [h for h in handled if isinstance(h, str) and h]

    def save(self, ms: int) -> None:
        write_private_json(self.path, {**self._read(), "last_create_at": ms})

    def save_seen(self, newest_partner_dm: int, handled: list[str]) -> None:
        data = {**self._read(), "newest_partner_dm": newest_partner_dm, "handled": handled}
        write_private_json(self.path, data)
