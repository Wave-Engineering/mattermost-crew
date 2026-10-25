"""IT-21 against the CI leg's Mattermost (Story 3.5; R-31, R-32).

The daemon is wired as `crewchat serve` wires it, through the Story 3.3 rig: real brokers,
token store, `MattermostBackend`, `SwitchCache`, `Filters` and `Doorbell`, whose `KIOSK`
verdicts land on the `KioskQueue` the `Kiosk` consumes. The brief arrives through the real
shim (`crewchat_clients.mcp_shim.Shim.sync_files`), which reads a fixture seed holding a
`## Kiosk` section and a secret elsewhere, and pushes `kiosk_brief` into the daemon's real
`build_chat_handlers`. The model runner is a recorder: the assertions are on the prompts it
received, and on the posts the kiosk made on the real server.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.kiosk import KIOSK_HEADER, NO_BRIEF_REPLY, Kiosk
from crewchat_clients.mcp_shim import Shim

from tests.integration.mmhttp import mm_request
from tests.integration.test_filters import MM, Rig, world  # noqa: F401  (the module fixture)
from tests.integration.test_mattermost_adapter import SID

# Markers that must never reach a prompt: one elsewhere in the seed, one in a crewmate's post.
SEED_SECRET = "ELSEWHERE-IN-THE-SEED-MARKER"
CREWMATE_SECRET = "CREWMATE-ONLY-MARKER"
SEED = f"""# Seed

## Working state

We are mid-way through the token rotation fix. Do not share: {SEED_SECRET}.

## Kiosk

This crew is building mattermost-crew, a chat daemon and a per-crew token broker.

## Next steps

- finish the rotation fix
"""
ANSWER_WAIT_S = 20.0


class Recorder:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def answer(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"We build a chat daemon (answer {len(self.prompts)}). Ask @kessel or @channel."


def write_seed(repo: Path, text: str) -> None:
    seed = repo / ".claude" / "reseed-revive.md"
    seed.write_text(text, encoding="utf-8")
    st = seed.stat()
    # A distinct mtime per write, so the shim's mtime check sees every change.
    os.utime(seed, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def kiosk_answers(mattermost: Any, root: str) -> list[dict[str, Any]]:
    resp = mm_request(mattermost, "GET", f"/posts/{root}/thread")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    posts = [data["posts"][pid] for pid in data["order"]]
    found = [p for p in posts if (p.get("props") or {}).get("crew_kiosk") == 1]
    return sorted(found, key=lambda p: p["create_at"])


async def wait_answers(mattermost: Any, root: str, count: int) -> list[dict[str, Any]]:
    for _ in range(int(ANSWER_WAIT_S / 0.1)):
        found = kiosk_answers(mattermost, root)
        if len(found) >= count:
            return found
        await asyncio.sleep(0.1)
    raise AssertionError(f"the kiosk posted {len(kiosk_answers(mattermost, root))} of {count}")


def test_it21_kiosk(mattermost: Any, world: dict[str, Any], tmp_path: Path) -> None:  # noqa: F811
    """IT-21 (R-31, R-32)."""

    async def scenario() -> None:
        rig = Rig(mattermost, world, tmp_path)
        babel = await rig.register("babel")
        await rig.register("kessel")
        await rig.cache.refresh_all()

        # The daemon's chat ops, holding the one brief store the kiosk reads.
        context = AgentContext()
        chat = build_chat_handlers(
            rig.registry, {MM: rig.backend}, rig.threads, context, filters=rig.filters
        )

        async def transport(op: str, args: dict[str, Any]) -> dict[str, Any]:
            result: dict[str, Any] = await chat[op](args)
            return result

        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "agent-identity.json").write_text(
            json.dumps({"dev_name": "babel", "dev_team": "oaw", "dev_avatar": "x"})
        )
        write_seed(repo, SEED)
        shim = Shim(
            repo,
            transport,
            headless=True,
            defaults_file=tmp_path / "home" / "chat-defaults.json",
            remote_for=lambda _root: None,
            session_id="it21",
            environ={},
            legacy_dir=tmp_path / "no-legacy",
        )
        await shim.sync_files(force=True)
        assert SEED_SECRET not in context.briefs["babel"]
        assert "mattermost-crew" in context.briefs["babel"]

        runner = Recorder()
        kiosk = Kiosk(
            rig.kiosk, context.briefs, {MM: rig.backend}, runner, state_dir=tmp_path / "state"
        )
        kiosk.start()
        await rig.listen()
        try:
            # Lazy: no session before an outsider asks.
            assert kiosk.sessions == {}

            # Crew only, kiosk on: the outsider's direct question reaches the kiosk.
            root = rig.post_as(rig.outsider_token, "@babel what are you building?")
            first = await wait_answers(mattermost, root, 1)
            assert list(kiosk.sessions) == [("babel", SID)]

            # A crewmate's post in the thread, then the outsider follows up.
            await rig.send("kessel", f"internal: {CREWMATE_SECRET}", thread=root)
            rig.post_as(rig.outsider_token, "@babel and who runs it?", root=root)
            answers = await wait_answers(mattermost, root, 2)

            assert len(runner.prompts) == 2
            for prompt in runner.prompts:
                assert prompt.startswith(KIOSK_HEADER)
                assert "mattermost-crew" in prompt  # answered from the ## Kiosk section
                assert SEED_SECRET not in prompt and "rotation fix" not in prompt
                assert CREWMATE_SECRET not in prompt
            assert "what are you building?" in runner.prompts[1]
            assert "answer 1" in runner.prompts[1]  # the kiosk's own earlier answer

            # Kiosk posts: as babel, in the outsider's thread, tagged, with no @mentions.
            for post in answers:
                assert post["user_id"] == babel and post["root_id"] == root
                assert post["props"]["crew_kiosk"] == 1
                assert "@" not in post["message"]
            assert first[0]["message"].startswith("We build a chat daemon (answer 1)")

            # Outsider content: a crewmate's pull omits the kiosk's answers under crew only.
            reads = await rig.read("kessel")
            ids = {m["post_id"] for m in reads}
            assert not ids & {p["id"] for p in answers}

            # An empty ## Kiosk section, pushed through the shim: the fixed reply, no model.
            write_seed(repo, SEED.split("## Kiosk")[0] + "## Kiosk\n\n## Next steps\n")
            await shim.sync_files()
            assert context.briefs["babel"] == ""
            again = rig.post_as(rig.outsider_token, "@babel anything new?")
            [fixed] = await wait_answers(mattermost, again, 1)
            assert fixed["message"] == NO_BRIEF_REPLY
            assert len(runner.prompts) == 2

            # Every answered question is in that server's transcript.
            transcript = tmp_path / "state" / "kiosk" / f"babel-{SID}.jsonl"
            records = [json.loads(line) for line in transcript.read_text().splitlines()]
            assert [r["thread"] for r in records] == [root, root, again]
            assert {r["asker_id"] for r in records} == {rig.outsider_id}
            assert [r["answer"] for r in records][-1] == NO_BRIEF_REPLY
        finally:
            await kiosk.aclose()
            await rig.aclose()

    asyncio.run(scenario())
