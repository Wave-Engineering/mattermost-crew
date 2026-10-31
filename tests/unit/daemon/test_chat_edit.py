"""#149: `edit` on the Discord adapter. Every agent posts as the one bot, so Discord itself
would let any agent rewrite any other's message: the daemon checks ownership first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from crewchat.adapters.base import Embed, Forbidden, NotFound
from crewchat.adapters.discord import MAX_CHUNK

from tests.fakes.screamhole import FakeDiscord
from tests.unit.daemon.test_discord import C1, ME, backend, credential, discord

__all__ = ["credential", "discord"]  # fixtures, shared with the adapter's own tests

OTHER = "babelfish"


def test_edit_refuses_foreign_discord_post(
    discord: FakeDiscord, credential: Path, run: Any
) -> None:
    """Another agent's message, and one this daemon never sent (a restart), are `Forbidden`
    with no request to Discord."""
    b = backend(discord, credential)
    b.set_identity(OTHER, avatar="🐟", team="oaw")
    theirs = run(b.send(OTHER, C1, "mine, not yours"))
    with pytest.raises(Forbidden):
        run(b.edit(ME, f"{C1}/{theirs.post_id}", "hijacked"))
    fresh = backend(discord, credential)
    mine = run(b.send(ME, C1, "before the restart"))
    with pytest.raises(Forbidden):
        run(fresh.edit(ME, f"{C1}/{mine.post_id}", "after"))
    assert discord.edits == []
    assert discord.messages[C1][0]["content"].startswith("mine, not yours")


def test_edit_resigns_once_on_discord(discord: FakeDiscord, credential: Path, run: Any) -> None:
    """The new text is signed exactly as `send` signs it, one signature, and an embed rides
    along; text too long for one message is refused before any request."""
    b = backend(discord, credential)
    sent = run(b.send(ME, C1, "first draft"))
    edited = run(b.edit(ME, sent.post_id, "second draft", embed=Embed("T", "body")))
    [(channel, _, body)] = discord.edits
    assert channel == C1
    assert body["content"] == "second draft\n\n— **hailing-frequencies** 📡 (oaw) ·p1"
    assert body["content"].count("— **") == 1
    assert body["embeds"] == [{"title": "T", "description": "body"}]
    assert edited.text == body["content"] and edited.author_name == ME
    # An already-signed text is re-signed, not signed twice.
    run(b.edit(ME, sent.post_id, "third\n\n— **hailing-frequencies** 📡 (oaw)"))
    assert discord.edits[-1][2]["content"].count("— **") == 1
    with pytest.raises(ValueError, match="one message"):
        run(b.edit(ME, sent.post_id, "x" * MAX_CHUNK))
    assert len(discord.edits) == 2


def test_edit_of_a_deleted_discord_post_is_not_found(
    discord: FakeDiscord, credential: Path, run: Any
) -> None:
    b = backend(discord, credential)
    sent = run(b.send(ME, C1, "soon gone"))
    discord.messages[C1].clear()
    with pytest.raises(NotFound):
        run(b.edit(ME, sent.post_id, "too late"))
