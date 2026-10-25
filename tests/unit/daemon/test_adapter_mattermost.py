"""Story 2.4c: the Mattermost adapter's attachments, embeds, single-post read and thread probe
(R-12, R-15; CT-01). The server is `FakeMattermost` (conftest)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from crewchat.adapters.base import Attachment, Embed, NotFound, PostProbe, parse_embed
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.server import DaemonServer, build_handlers
from crewchat.threads import MyThreads

from tests.unit.daemon.test_mattermost import Tokens, backend

DEV = "babel"


def test_embed_mattermost_attachment(mm: Any, run: Any, daemon: Any) -> None:
    """`embed` becomes `props.attachments[0] = {title, text}`, beside `crew_proto`; a
    malformed embed is `bad_request` and nothing is posted."""
    tokens = Tokens(mm)
    tokens.add(DEV)
    b = backend(mm, tokens)
    msg = run(b.send(DEV, "chan", "with an embed", embed=parse_embed("Build: green: 12/12")))
    props = mm.posts[msg.post_id]["props"]
    assert props["attachments"][0] == {"title": "Build", "text": "green: 12/12"}  # first colon
    assert props["crew_proto"] == 1
    dm = run(b.dm(DEV, "u" + "0" * 25, "a dm", embed=Embed("Only a title", "")))
    assert mm.posts[dm.post_id]["props"]["attachments"] == [{"title": "Only a title", "text": ""}]

    for bad in ("", ":no title", "   :x", 42, "t" * 300):
        with pytest.raises(ValueError):
            parse_embed(bad)

    # Through the daemon's `send`: a malformed embed is bad_request, and nothing reaches the
    # server.
    asyncio.run(daemon.registry.register(DEV, None, "s1"))
    handlers = build_handlers(daemon.config, daemon.registry)
    handlers.update(build_chat_handlers(daemon.registry, {b.name: b}, MyThreads(), AgentContext()))
    server = DaemonServer(handlers)
    before = len(mm.posts)
    for bad in (":no title", 7):
        frame = {
            "proto": 1,
            "id": "x",
            "op": "send",
            "args": {"dev_name": DEV, "backend": b.name, "to": "chan", "text": "x", "embed": bad},
        }
        got = asyncio.run(server.handle_line(json.dumps(frame).encode()))
        assert got["ok"] is False and got["error"].startswith("bad_request"), bad
    assert len(mm.posts) == before


def test_attachments_upload_with_the_agents_own_token(
    mm: Any, run: Any, tmp_path: Path
) -> None:
    """CT-01: `POST /api/v4/files` with the agent's token, then the post with `file_ids`."""
    tokens = Tokens(mm)
    me = tokens.add(DEV)
    b = backend(mm, tokens)
    held = tmp_path / "held"
    held.write_bytes(b"log line 1\nlog line 2\n")
    att = Attachment(name="build.log", path=held, size=held.stat().st_size)
    msg = run(b.send(DEV, "chan", "see attached", attachments=[att], embed=Embed("Log", "")))
    post = mm.posts[msg.post_id]
    (file_id,) = post["file_ids"]
    stored = mm.files[file_id]
    assert (stored["name"], stored["data"]) == ("build.log", held.read_bytes())
    assert stored["user_id"] == me
    assert stored["channel_id"] == "chan"
    agent_token = tokens.held[(DEV, "dev")]
    assert ("POST", "/files", agent_token) in mm.requests
    assert post["props"]["attachments"] == [{"title": "Log", "text": ""}]


def test_read_one_and_has_post(mm: Any, run: Any) -> None:
    tokens = Tokens(mm)
    tokens.add(DEV)
    b = backend(mm, tokens)
    assert isinstance(b, PostProbe)
    human = mm.add_user("bj", is_bot=False)
    post = mm.add_post(human, "chan", "a human root")
    one = run(b.read_one(DEV, "chan", post["id"]))
    assert (one.post_id, one.text, one.backend) == (post["id"], "a human root", "mattermost:dev")
    with pytest.raises(NotFound):
        run(b.read_one(DEV, "other-chan", post["id"]))
    with pytest.raises(NotFound):
        run(b.read_one(DEV, "chan", "p" + "9" * 25))
    assert run(b.has_post(DEV, post["id"])) is True
    assert run(b.has_post(DEV, "p" + "9" * 25)) is False
    mm.hidden_from[post["id"]] = {tokens.user_ids[DEV]}
    assert run(b.has_post(DEV, post["id"])) is False
    assert run(b.has_post("nobody", post["id"])) is False  # holds no token here
