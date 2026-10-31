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
    rings,
    squawk_answer,
    squawk_key,
)

from tests.fakes.squawkie import URL, FakeSquawkie
from tests.required import missing

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
    vendored `sqtk-answer.jq` is the one installed beside it."""
    candidates = [os.environ.get("SQTK_DOORBELL", ""), "~/.claude/scripts/sqtk-doorbell.sh"]
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
    }
    assert by_id[settled["id"]]["list"] == "other:triage"
    assert live.payload["type"] == "squawk.updated"
    assert live.payload["squawk"]["id"] == mine["id"]
    assert live.keys == (f"{mine['id']}:open|yes",)
    assert status.seen[0] == (True, "")


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
