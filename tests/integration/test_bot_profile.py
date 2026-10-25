"""IT-19 against the CI leg's Mattermost (Story 3.8; R-01): bot account avatars and `@`
autocomplete.

Two agents register through the daemon's registry over the module's in-process broker
(`test_mattermost_adapter.Daemon`), so their bot accounts are the broker's own. Each bot's
avatar is set by its owner, the broker account (`POST /users/{bot_id}/image`): a bot can't set
its own (403, spike 1.2, D-098). An ordinary user then sees each bot's own avatar at
`GET /users/{id}/image`, and finds each bot account in `GET /users/autocomplete?name=<dev>`.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import zlib
from pathlib import Path
from typing import Any

from tests.integration.mmhttp import mm_request
from tests.integration.test_filters import login_user
from tests.integration.test_mattermost_adapter import Daemon, setup

__all__ = ["setup"]  # the module's Mattermost team, broker and channel (a fixture)

COLOURS = {"avia": (0xD0, 0x40, 0x20), "avib": (0x20, 0x60, 0xD0)}


def png(rgb: tuple[int, int, int], size: int = 32) -> bytes:
    """A solid-colour PNG, `size` pixels square."""
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def image(mattermost: Any, token: str, user_id: str) -> bytes:
    resp = mm_request(mattermost, "GET", f"/users/{user_id}/image", token=token)
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("Content-Type", "").startswith("image/"), resp.headers
    return resp.content


def avatar_hash(mattermost: Any, token: str, user_id: str) -> bytes:
    """What `GET /users/{id}/image` serves, as `token` sees it, hashed."""
    return hashlib.sha256(image(mattermost, token, user_id)).digest()


def test_it19_bot_avatars_and_autocomplete(
    mattermost: Any, setup: dict[str, str], tmp_path: Path
) -> None:
    d = Daemon(mattermost, setup, tmp_path)

    async def register() -> dict[str, str]:
        return {dev: await d.register(dev) for dev in COLOURS}

    ids = asyncio.run(register())
    crew = setup["crew"]
    observer_id, observer = login_user(mattermost, f"observer-{crew}")
    resp = mm_request(
        mattermost,
        "POST",
        f"/teams/{setup['team_id']}/members",
        json={"team_id": setup["team_id"], "user_id": observer_id},
    )
    assert resp.status_code == 201, resp.text

    default = {dev: avatar_hash(mattermost, observer, i) for dev, i in ids.items()}
    for dev, bot_id in ids.items():
        # A bot can't set its own avatar; its owner, the broker, does (D-098).
        resp = mm_request(
            mattermost,
            "POST",
            f"/users/{bot_id}/image",
            token=setup["broker_token"],
            files={"image": (f"{dev}.png", png(COLOURS[dev]), "image/png")},
        )
        assert resp.status_code == 200, resp.text

    seen = {dev: avatar_hash(mattermost, observer, i) for dev, i in ids.items()}
    for dev in ids:
        assert seen[dev] != default[dev], f"{dev}'s avatar is still the default"
    assert seen["avia"] != seen["avib"], "the two bots share one avatar"

    for dev, bot_id in ids.items():
        resp = mm_request(
            mattermost,
            "GET",
            "/users/autocomplete",
            token=observer,
            params={"in_team": setup["team_id"], "name": dev},
        )
        assert resp.status_code == 200, resp.text
        users = {u["id"]: u for u in resp.json().get("users", [])}
        assert bot_id in users, f"{dev}.{crew} is not in @ autocomplete"
        assert users[bot_id]["username"] == f"{dev}.{crew}"  # R-01
        assert users[bot_id]["is_bot"] is True
        assert users[bot_id].get("last_picture_update"), f"{dev}'s avatar update is not shown"
