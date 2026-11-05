"""Story 3.2: the squawkie adapter (R-26, R-27, R-28).

The predicate is held to `sqtk-doorbell.sh`'s jq by a shared fixture,
`tests/fixtures/sqtk_doorbell_predicate.json`: the daemon's `rings` and the script's jq (run
with `jq`, when it is installed) must agree with every case. The feed comes from
`tests/fakes/squawkie.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from crewchat.adapters.base import StreamCursor
from crewchat.adapters.squawkie import (
    SquawkEvent,
    SquawkieBackend,
    own_initials,
    rings,
    squawk_answer,
    squawk_key,
)

from tests.fakes.squawkie import URL, FakeSquawkie
from tests.required import REQUIRE_ENV, missing

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
FIXTURE = FIXTURES / "sqtk_doorbell_predicate.json"
# The kit's `hooks/workflow/sqtk-answer.jq` (cc#1363), vendored so the jq runs without the kit.
ANSWER_JQ = FIXTURES / "sqtk-answer.jq"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
DEV = "babel"


def case_ids(case: dict[str, Any]) -> list[str]:
    return [str(i) for i in case["ids"]]


def case_texts(case: dict[str, Any]) -> dict[str, Any]:
    """The script's `$bl`: the squawk's original text by id, when the case knows it."""
    squawk = case["event"].get("squawk") or {}
    return {str(squawk.get("id")): case["original"]} if "original" in case else {}


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_predicate_matches_the_fixture(case: dict[str, Any]) -> None:
    assert rings(case["event"], case_ids(case), case_texts(case)) is case["rings"]


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_fixture_is_sqtk_doorbells_own_verdict(case: dict[str, Any]) -> None:
    """The fixture's verdicts are the script's: its jq, run as the script runs it, with
    `--slurpfile`'s one-element arrays for `$ids` and `$bl`."""
    if shutil.which("jq") is None:
        missing("jq")
    proc = subprocess.run(
        [
            "jq",
            "-e",
            "-L",
            str(FIXTURES),
            "--argjson",
            "ids",
            json.dumps([case["ids"]]),
            "--argjson",
            "bl",
            json.dumps([case_texts(case)], ensure_ascii=False),
            CASES["jq"],
        ],
        input=json.dumps(case["event"], ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    assert (proc.returncode == 0) is case["rings"], proc.stdout + proc.stderr


def test_fixture_jq_is_the_installed_scripts() -> None:
    """When the kit's `sqtk-doorbell.sh` is on this host, the fixture's jq is its jq, and the
    vendored `sqtk-answer.jq` is the one installed beside it. Under `CREWCHAT_REQUIRE_TEST_TOOLS=1`
    only the job's pinned `SQTK_DOORBELL` counts, never a kit in the runner's home (#156)."""
    candidates = [os.environ.get("SQTK_DOORBELL", "")]
    if os.environ.get(REQUIRE_ENV) != "1":
        candidates.append("~/.claude/scripts/sqtk-doorbell.sh")
    paths = [Path(c).expanduser() for c in candidates if c]
    script = next((p for p in paths if p.is_file()), None)
    if script is None:
        missing("sqtk-doorbell.sh")
    text = script.read_text(encoding="utf-8")
    for line in CASES["jq"].splitlines():
        assert line in text, line
    installed = script.resolve().parent / "hooks" / "workflow" / "sqtk-answer.jq"
    assert installed.read_text(encoding="utf-8") == ANSWER_JQ.read_text(encoding="utf-8")


def test_answer_and_key() -> None:
    edited = {"created_at": "t0", "updated_at": "t1"}
    assert squawk_answer({**edited, "text": "a → b → c "}) == "c"
    assert squawk_answer({**edited, "text": "no arrow"}) == ""
    assert squawk_answer({"created_at": "t0", "updated_at": "t0", "text": "q → yes"}) == ""
    assert squawk_answer({**edited, "text": "campaign→main fix, and more"}) == "main fix, and more"
    original = "campaign→main fix"
    assert squawk_answer({**edited, "text": "campaign→main fix, and more"}, original) == ""
    assert squawk_answer({**edited, "text": "q → a → b"}, "q →") == "a → b"
    assert squawk_key({"id": 5, "state": "open", "text": "q → yes"}) == "5:open|yes"
    key = squawk_key({"id": 5, "state": "open", "text": "x→y z"}, {"5": "x→y"})
    assert key == "5:open|"  # O's arrow: no answer, so the key carries none


async def take(gen: AsyncIterator[SquawkEvent], n: int) -> list[SquawkEvent]:
    return [await asyncio.wait_for(anext(gen), 2) for _ in range(n)]


class Status:
    def __init__(self) -> None:
        self.seen: list[tuple[bool, str]] = []

    def __call__(self, up: bool, reason: str) -> None:
        self.seen.append((up, reason))


async def no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def make(
    sq: FakeSquawkie,
    ids: dict[str, list[str]],
    token: str | None = None,
    texts: dict[str, dict[str, str]] | None = None,
) -> SquawkieBackend:
    return SquawkieBackend(
        URL,
        token,
        ids_for=lambda d: ids.get(d, []),
        texts_for=lambda d: (texts or {}).get(d, {}),
        http_client=sq.http_client(),
        sleep=no_sleep,
    )


def test_a_prose_arrow_never_rings(run: Any) -> None:
    """#170: with O known, an edit to a squawk whose own text holds an arrow is no answer,
    live or in the catch-up; an answer in its slot rings, keyed by that answer."""
    sq = FakeSquawkie()
    needs = sq.add_list("bj:needs")
    prose = sq.add_squawk(needs, "merge campaign→main?")
    sid = str(prose["id"])
    # Edited, still unanswered. By the last-arrow rule this reads "main now?" and would ring.
    sq.update(prose["id"], text="merge campaign→main now?")
    b = make(sq, {DEV: [sid]}, texts={DEV: {sid: "merge campaign→main?"}})

    async def scenario() -> SquawkEvent:
        gen = b.stream(DEV)
        nxt = asyncio.ensure_future(take(gen, 1))
        while sq.open_streams() == 0:
            await asyncio.sleep(0)
        sq.update(prose["id"], text="merge campaign→main today?")  # still no answer
        sq.update(prose["id"], text="merge campaign→main today? → yes")
        got = (await nxt)[0]
        await b.aclose()
        await gen.aclose()
        return got

    got = run(scenario())
    assert got.payload["type"] == "squawk.updated"  # no catch-up rang first
    assert got.keys == (f"{sid}:open|yes",)


def test_squawkie_filter_and_catchup(run: Any) -> None:
    """Only my squawks ring, by the predicate; an answer that landed before the stream opened
    rings at once as one catch-up, and a re-broadcast of the same answer is not rung again."""
    sq = FakeSquawkie(token="sq-token")
    needs = sq.add_list("bj:needs")
    other = sq.add_list("other:triage")
    early = sq.add_squawk(needs, "which server? →")
    sq.update(early["id"], text="which server? → dev")  # answered before we connect
    mine = sq.add_squawk(needs, "deploy now?")
    theirs = sq.add_squawk(needs, "not mine")
    settled = sq.add_squawk(other, "old one", state="retired")
    open_one = sq.add_squawk(other, "still open")
    ids = {DEV: [str(early["id"]), str(mine["id"]), str(settled["id"]), str(open_one["id"])]}
    b = make(sq, ids, token="sq-token")
    status = Status()

    async def scenario() -> list[SquawkEvent]:
        gen = b.stream(DEV, on_status=status)
        first = await take(gen, 1)
        nxt = asyncio.ensure_future(take(gen, 1))
        while sq.open_streams() == 0:
            await asyncio.sleep(0)
        sq.update(theirs["id"], state="confirmed")  # not mine
        sq.update(open_one["id"], text="still open, edited")  # mine, but no answer
        sq.update(early["id"], text="which server? → dev")  # same answer: rung already
        sq.update(mine["id"], text="deploy now? → yes")
        got = first + await nxt
        await b.aclose()
        await gen.aclose()
        return got

    catch_up, live = run(scenario())
    assert catch_up.payload["type"] == "sqtk.catch-up"
    changes = catch_up.payload["changes"]
    assert sorted(c["id"] for c in changes) == sorted([early["id"], settled["id"]])
    by_id = {c["id"]: c for c in changes}
    assert by_id[early["id"]] == {
        "id": early["id"],
        "list": "bj:needs",
        "seq": early["seq"],
        "state": "open",
        "answer": "dev",
        "text": "which server? → dev",
        "choice": None,
        "note": "",
    }
    assert by_id[settled["id"]]["list"] == "other:triage"
    assert live.payload["type"] == "squawk.updated"
    assert live.payload["squawk"]["id"] == mine["id"]
    assert live.keys == (f"{mine['id']}:open|yes",)
    assert status.seen[0] == (True, "")


def test_own_initials_follow_sqtks_rule() -> None:
    """`norm_initials`: uppercase, ASCII alphanumerics only, at most 3; none when none are left."""
    assert own_initials("babel") == "BAB"
    assert own_initials("dixie-flatline") == "DIX"
    assert own_initials("k2") == "K2"
    assert own_initials("") is None
    assert own_initials("---") is None


def test_the_answer_that_is_only_the_editors_own_tag_is_no_answer() -> None:
    """#206 / claudecode-workflow#93: `(CER)` by CER is a signature; `(ok)` by someone else, or
    text with the tag, is an answer. Matched to the kit's jq by the shared fixture."""
    edited = {"created_at": "t0", "updated_at": "t1"}
    mine = {**edited, "initials": "CER"}
    assert squawk_answer({**mine, "text": "q →  (CER)"}) == ""
    assert squawk_answer({**mine, "text": "q →  (CER)  (CER)"}) == ""
    assert squawk_answer({**mine, "text": "q → yes (CER)"}) == "yes (CER)"
    assert squawk_answer({**edited, "initials": "BJ", "text": "q → (ok)"}) == "(ok)"
    assert squawk_answer({**edited, "initials": "BJ", "text": "q → (CER)"}) == "(CER)"
    assert squawk_answer({**edited, "text": "q → (CER)"}) == "(CER)"  # no editor recorded
    assert (
        squawk_answer({**mine, "initials": "a.b", "text": "q → (a.b)"}) == "(a.b)"
    )  # not initials


def test_own_changes_do_not_ring_but_a_later_answer_does() -> None:
    """#206: the last change was the agent's own: nothing rings. Someone else's change after it
    rings as before, so an answer is never lost behind the agent's own edit."""

    def event(initials: str, state: str, text: str) -> dict[str, Any]:
        squawk = {
            "id": 5,
            "state": state,
            "text": text,
            "initials": initials,
            "created_at": "t0",
            "updated_at": "t1",
        }
        return {"type": "squawk.updated", "squawk": squawk}

    retired = event("BAB", "retired", "mine →")
    assert rings(retired, ["5"], None) is True  # without `own`: as before
    assert rings(retired, ["5"], None, "BAB") is False
    assert rings(event("BAB", "open", "mine → yes"), ["5"], None, "BAB") is False
    assert rings(event("BJ", "open", "mine → yes"), ["5"], None, "BAB") is True
    assert rings(event("BJ", "retired", "mine →"), ["5"], None, "BAB") is True
    assert rings(retired, ["5"], None, None) is True


def test_own_changes_neither_ring_live_nor_in_catch_up(run: Any) -> None:
    """#206 through the stream: a squawk the agent itself last changed is left out of the
    catch-up and rings nothing live, and an answer from someone else after it still rings."""
    sq = FakeSquawkie()
    needs = sq.add_list("bj:needs")
    theirs = sq.add_squawk(needs, "which server? →")
    sq.update(theirs["id"], text="which server? → dev", initials="BJ")  # settled by someone else
    own_early = sq.add_squawk(needs, "recorded →")
    sq.update(own_early["id"], state="retired", initials="BAB")  # settled by the agent
    live = sq.add_squawk(needs, "deploy now?")
    ids = {DEV: [str(theirs["id"]), str(own_early["id"]), str(live["id"])]}
    b = make(sq, ids)

    async def scenario() -> list[SquawkEvent]:
        gen = b.stream(DEV)
        first = await take(gen, 1)
        nxt = asyncio.ensure_future(take(gen, 1))
        while sq.open_streams() == 0:
            await asyncio.sleep(0)
        sq.update(live["id"], state="retired", initials="BAB")  # the agent retires it: silent
        sq.update(live["id"], text="deploy now? → yes", initials="BJ")  # then BJ answers
        got = first + await nxt
        await b.aclose()
        await gen.aclose()
        return got

    catch_up, ring = run(scenario())
    assert [c["id"] for c in catch_up.payload["changes"]] == [theirs["id"]]
    assert ring.payload["squawk"]["id"] == live["id"]
    assert ring.payload["squawk"]["initials"] == "BJ"
    assert ring.keys == (f"{live['id']}:retired|yes",)


def test_catch_up_skips_what_the_cursor_says_rang(run: Any) -> None:
    """A restart's catch-up leaves out the keys in `cursor.seen` (R-28)."""
    sq = FakeSquawkie()
    needs = sq.add_list("bj:needs")
    a = sq.add_squawk(needs, "a →")
    b_sq = sq.add_squawk(needs, "b →")
    sq.update(a["id"], text="a → yes")  # answers are edits: a pristine squawk has none
    sq.update(b_sq["id"], text="b → no")
    a = sq.squawks[a["id"]]
    ids = {DEV: [str(a["id"]), str(b_sq["id"])]}
    b = make(sq, ids)

    async def scenario() -> SquawkEvent:
        cursor = StreamCursor(squawk_key(a), 0.0, frozenset({squawk_key(a)}))
        gen = b.stream(DEV, cursor=cursor)
        got = (await take(gen, 1))[0]
        await b.aclose()
        await gen.aclose()
        return got

    got = run(scenario())
    assert [c["id"] for c in got.payload["changes"]] == [b_sq["id"]]


def test_key_carries_a_choice_and_a_note_only_when_there_are_some() -> None:
    """#233: a squawk with neither keeps today's key byte for byte (a saved cursor still
    matches); a choice and a note each add to it, and a different one is a different key."""
    base = {"id": 7, "state": "confirmed", "text": "q? →", "created_at": "t0", "updated_at": "t1"}
    plain = squawk_key(base)
    assert plain == "7:confirmed|"
    assert squawk_key({**base, "choice": None, "note": ""}) == plain
    a = squawk_key({**base, "choice": "Ship it"})
    assert a == "7:confirmed||choice=Ship it"
    assert squawk_key({**base, "choice": "Hold"}) != a
    noted = squawk_key({**base, "choice": "Ship it", "note": "after the freeze"})
    assert noted.startswith(a + "|note=") and len(noted) == len(a) + len("|note=") + 12
    assert squawk_key({**base, "choice": "Ship it", "note": "after the thaw"}) != noted
    assert squawk_key({**base, "choice": "Ship it", "note": "after the freeze"}) == noted


def test_an_open_squawks_note_is_never_read() -> None:
    """The server sends an open item's note empty in events but `GET` returns the draft, which
    a catch-up reads. The adapter keeps it out of both the key and the change, so a catch-up
    and a live ring of the same open item are one key."""
    open_answered = {
        "id": 9,
        "state": "open",
        "text": "q? → yes",
        "created_at": "t0",
        "updated_at": "t1",
        "note": "half-written draft",
    }
    assert squawk_key(open_answered) == "9:open|yes"
    assert squawk_key({**open_answered, "note": ""}) == "9:open|yes"


def test_choice_and_note_reach_the_agent_in_catch_up_and_live(run: Any) -> None:
    """#233: the catch-up change carries `choice` and `note` (an open item's draft left out),
    and a live ring carries them as squawkie sent them."""
    sq = FakeSquawkie()
    needs = sq.add_list("bj:needs")
    chosen = sq.add_squawk(needs, "deploy now?")
    sq.update(chosen["id"], note="after the freeze")  # a draft: still open
    sq.update(chosen["id"], choice="Ship it")  # picking an option disposes it
    drafted = sq.add_squawk(needs, "which server? →")
    sq.update(drafted["id"], text="which server? → dev", note="my draft")  # answered, still open
    live = sq.add_squawk(needs, "roll back?")
    ids = {DEV: [str(chosen["id"]), str(drafted["id"]), str(live["id"])]}
    b = make(sq, ids)

    async def scenario() -> list[SquawkEvent]:
        gen = b.stream(DEV)
        first = await take(gen, 1)
        nxt = asyncio.ensure_future(take(gen, 1))
        while sq.open_streams() == 0:
            await asyncio.sleep(0)
        sq.update(live["id"], note="not yet")
        sq.update(live["id"], choice="Roll back")
        got = first + await nxt
        await b.aclose()
        await gen.aclose()
        return got

    catch_up, ring = run(scenario())
    by_id = {c["id"]: c for c in catch_up.payload["changes"]}
    assert by_id[chosen["id"]]["state"] == "confirmed"
    assert by_id[chosen["id"]]["choice"] == "Ship it"
    assert by_id[chosen["id"]]["note"] == "after the freeze"
    assert by_id[drafted["id"]]["choice"] is None
    assert by_id[drafted["id"]]["note"] == ""  # the open item's draft is not handed over
    assert ring.payload["squawk"]["id"] == live["id"]
    assert ring.payload["squawk"]["choice"] == "Roll back"
    assert ring.payload["squawk"]["note"] == "not yet"


def test_a_new_choice_or_note_rings_again_and_the_same_one_does_not(run: Any) -> None:
    """#233 and R-28: the same state and answer with another choice, or a note added after,
    is a new key and rings; a re-broadcast of the same choice and note is not rung twice."""
    sq = FakeSquawkie()
    needs = sq.add_list("bj:needs")
    item = sq.add_squawk(needs, "deploy now?")
    ids = {DEV: [str(item["id"])]}
    b = make(sq, ids)

    async def scenario() -> list[SquawkEvent]:
        gen = b.stream(DEV)
        # No catch-up event: nothing has changed yet, so the first event is a live one.
        nxt = asyncio.ensure_future(take(gen, 3))
        while sq.open_streams() == 0:
            await asyncio.sleep(0)
        sq.update(item["id"], choice="Ship it")  # rings
        sq.update(item["id"], choice="Ship it")  # same choice, same note: not again
        sq.update(item["id"], choice="Hold")  # another choice: rings
        sq.update(item["id"], note="because of the freeze")  # a note after disposal: rings
        got = await nxt
        await b.aclose()
        await gen.aclose()
        return got

    got = run(scenario())
    choices = [(e.payload["squawk"]["choice"], e.payload["squawk"]["note"]) for e in got]
    assert choices == [("Ship it", ""), ("Hold", ""), ("Hold", "because of the freeze")]


def test_unauthorized_and_refused_streams_report_down(run: Any) -> None:
    sq = FakeSquawkie(token="right")
    needs = sq.add_list("bj:needs")
    s = sq.add_squawk(needs, "q")
    ids = {DEV: [str(s["id"])]}
    status = Status()
    wrong = make(sq, ids, token="wrong")

    async def unauthorized() -> None:
        gen = wrong.stream(DEV, on_status=status)
        task = asyncio.ensure_future(anext(gen))
        while not status.seen:
            await asyncio.sleep(0)
        await wrong.aclose()
        task.cancel()
        with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
            await task

    run(unauthorized())
    assert status.seen[0] == (False, "unauthorized")

    sq.refuse_streams = 2
    status2 = Status()
    right = make(sq, ids, token="right")

    async def recovers() -> SquawkEvent:
        got: list[SquawkEvent] = []

        async def pump() -> None:
            async for event in right.stream(DEV, on_status=status2):
                got.append(event)

        task = asyncio.ensure_future(pump())
        async with asyncio.timeout(2):
            while sq.open_streams() == 0:
                await asyncio.sleep(0)
            sq.update(s["id"], state="confirmed")
            while not got:
                await asyncio.sleep(0)
            # The stream drops: down, then up again on the reconnect.
            sq.drop_streams()
            while len(status2.seen) < 5:
                await asyncio.sleep(0)
        await right.aclose()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return got[0]

    got = run(recovers())
    assert got.payload["squawk"]["state"] == "confirmed"
    assert status2.seen[:3] == [
        (False, "stream refused: HTTP 503"),
        (False, "stream refused: HTTP 503"),
        (True, ""),
    ]
    assert status2.seen[3] == (False, "stream ended")
    assert status2.seen[4] == (True, "")
