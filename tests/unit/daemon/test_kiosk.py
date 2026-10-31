"""Story 3.5: the kiosk (R-31, R-32).

The backend is the real `MattermostBackend` over `FakeMattermost` (conftest), so the posts the
kiosk makes, their props and the threads it reads are the adapter's own. The runner is a fake
that records every prompt it is given.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import io
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import crewchat.kiosk as kiosk_mod
import pytest
from crewchat.adapters.base import InboundMessage
from crewchat.chat import AgentContext
from crewchat.doorbell import DoorbellEvent
from crewchat.filters import KioskQueue
from crewchat.kiosk import (
    KIOSK_HEADER,
    KIOSK_RESEARCH_HEADER,
    NO_BRIEF_REPLY,
    BriefStore,
    Kiosk,
    KioskPoster,
    KioskSession,
    Research,
    ResearchFrame,
    render_research,
    research_header,
    strip_mentions,
)

from tests.unit.daemon.test_mattermost import CHAN, SID, Tokens, backend

MM = f"mattermost:{SID}"
DEV = "babel"
BRIEF = "We build mattermost-crew: a chat daemon and a token broker."


class FakeRunner:
    def __init__(self, answer: str = "We build a chat daemon.") -> None:
        self.prompts: list[str] = []
        self.reply = answer

    async def answer(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


class World:
    """One Mattermost server: the expert, a crewmate, two outsiders, and the kiosk."""

    def __init__(self, mm: Any, tmp_path: Path, runner: FakeRunner | None = None) -> None:
        self.mm = mm
        tokens = Tokens(mm)
        self.babel = tokens.add(DEV)
        self.kessel = tokens.add("kessel")
        self.outsider = mm.add_user("stranger")
        self.other = mm.add_user("passerby")
        self.backend = backend(mm, tokens)
        self.queue = KioskQueue()
        self.briefs = BriefStore()
        self.runner = runner or FakeRunner()
        self.state_dir = tmp_path / "state"
        self.kiosk = Kiosk(
            self.queue,
            self.briefs,
            {MM: self.backend},
            self.runner,
            state_dir=self.state_dir,
            clock=Clock(),
        )

    def post(self, user: str, text: str, root: str = "") -> InboundMessage:
        stored = self.mm.add_post(user, CHAN, text)
        stored["root_id"] = root
        return InboundMessage(
            MM,
            CHAN,
            stored["id"],
            root,
            user,
            "x",
            text,
            ts=stored["create_at"] / 1000,
        )

    def event(self, msg: InboundMessage) -> DoorbellEvent:
        return DoorbellEvent(MM, {"type": "message"}, (msg.post_id,), msg.ts, msg)

    @property
    def transcript(self) -> Path:
        return self.state_dir / "kiosk" / f"{DEV}-{SID}.jsonl"

    def kiosk_posts(self) -> list[dict[str, Any]]:
        return [p for p in self.mm.posts.values() if p["props"].get("crew_kiosk") == 1]


class Clock:
    def __init__(self) -> None:
        self.now = 4_000_000_000.0  # after every fake post's create_at

    def __call__(self) -> float:
        self.now += 1
        return self.now


def test_missing_brief_fixed_reply(mm: Any, run: Any, tmp_path: Path) -> None:
    w = World(mm, tmp_path)
    ask = w.post(w.outsider, "@babel what do you do?")
    posted = run(w.kiosk.handle(DEV, w.event(ask)))
    assert posted is not None and posted.text == NO_BRIEF_REPLY
    assert w.runner.prompts == []  # no brief, no model call
    w.briefs[DEV] = "   "  # an empty section is no brief either
    run(w.kiosk.handle(DEV, w.event(w.post(w.outsider, "@babel hello?", ask.post_id))))
    assert [p["message"] for p in w.kiosk_posts()] == [NO_BRIEF_REPLY, NO_BRIEF_REPLY]
    assert w.runner.prompts == []


def test_lazy_start(mm: Any, run: Any, tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World(mm, tmp_path)
        w.briefs[DEV] = BRIEF
        w.kiosk.start()
        try:
            assert w.kiosk.sessions == {} and not w.transcript.exists()
            await asyncio.sleep(0)
            assert w.kiosk.sessions == {}
            ask = w.post(w.outsider, "@babel what is this?")
            assert w.queue.put(DEV, w.event(ask))
            for _ in range(200):
                if w.kiosk_posts():
                    break
                await asyncio.sleep(0.01)
            assert list(w.kiosk.sessions) == [(DEV, SID)]
            assert len(w.runner.prompts) == 1
        finally:
            await w.kiosk.aclose()
        assert w.queue.listeners == []

    run(scenario())


def test_one_session_per_expert_per_server(mm: Any, run: Any, tmp_path: Path) -> None:
    """D-067: every outsider on a server shares the expert's session; another server's
    outsiders get their own."""
    w = World(mm, tmp_path)
    w.briefs[DEV] = BRIEF
    run(w.kiosk.handle(DEV, w.event(w.post(w.outsider, "@babel one"))))
    run(w.kiosk.handle(DEV, w.event(w.post(w.other, "@babel two"))))
    assert list(w.kiosk.sessions) == [(DEV, SID)]
    other_server = w.kiosk.session(DEV, "prod")
    assert other_server is not w.kiosk.sessions[(DEV, SID)]
    assert other_server.transcript.name == f"{DEV}-prod.jsonl"


def test_new_brief_replaces_old(mm: Any, run: Any, tmp_path: Path) -> None:
    w = World(mm, tmp_path)
    context = AgentContext(briefs=w.briefs)
    w.briefs[DEV] = "OLD-BRIEF"
    run(w.kiosk.handle(DEV, w.event(w.post(w.outsider, "@babel first"))))
    # A new kiosk_brief frame lands in the same store the kiosk reads.
    context.briefs[DEV] = "NEW-BRIEF"
    run(w.kiosk.handle(DEV, w.event(w.post(w.outsider, "@babel second"))))
    assert "OLD-BRIEF" in w.runner.prompts[0]
    assert "NEW-BRIEF" in w.runner.prompts[1] and "OLD-BRIEF" not in w.runner.prompts[1]
    assert w.runner.prompts[1].startswith(KIOSK_HEADER)


def test_history_only_asker_and_kiosk(mm: Any, run: Any, tmp_path: Path) -> None:
    w = World(mm, tmp_path, FakeRunner("ANSWER-ONE"))
    w.briefs[DEV] = BRIEF
    root = w.post(w.outsider, "@babel QUESTION-ONE")
    run(w.kiosk.handle(DEV, w.event(root)))
    # In the same thread: a crewmate, the expert itself, and a second outsider.
    w.post(w.kessel, "CREWMATE-SECRET internal note", root.post_id)
    w.post(w.babel, "EXPERT-OWN-POST not from the kiosk", root.post_id)
    second = w.post(w.other, "@babel OTHER-OUTSIDER-QUESTION", root.post_id)
    w.runner.reply = "ANSWER-TO-OTHER"
    run(w.kiosk.handle(DEV, w.event(second)))
    follow = w.post(w.outsider, "@babel QUESTION-TWO", root.post_id)
    w.runner.reply = "ANSWER-TWO"
    run(w.kiosk.handle(DEV, w.event(follow)))

    prompt = w.runner.prompts[-1]
    for wanted in ("QUESTION-ONE", "ANSWER-ONE", "QUESTION-TWO", BRIEF):
        assert wanted in prompt
    for banned in (
        "CREWMATE-SECRET",
        "EXPERT-OWN-POST",
        "OTHER-OUTSIDER-QUESTION",
        "ANSWER-TO-OTHER",
    ):
        assert banned not in prompt
    # The second outsider's prompt holds only their own post.
    assert "QUESTION-ONE" not in w.runner.prompts[1]
    assert "CREWMATE-SECRET" not in w.runner.prompts[1]
    # Order: the first question, its answer, then the follow-up.
    assert prompt.index("QUESTION-ONE") < prompt.index("ANSWER-ONE") < prompt.index("QUESTION-TWO")


def test_history_survives_a_restart(mm: Any, run: Any, tmp_path: Path) -> None:
    w = World(mm, tmp_path, FakeRunner("ANSWER-ONE"))
    w.briefs[DEV] = BRIEF
    root = w.post(w.outsider, "@babel QUESTION-ONE")
    run(w.kiosk.handle(DEV, w.event(root)))
    again = Kiosk(
        w.queue, w.briefs, {MM: w.backend}, w.runner, state_dir=w.state_dir, clock=Clock()
    )
    run(again.handle(DEV, w.event(w.post(w.outsider, "@babel QUESTION-TWO", root.post_id))))
    assert "ANSWER-ONE" in w.runner.prompts[-1]


def test_replay_after_a_restart_is_not_answered_again(mm: Any, run: Any, tmp_path: Path) -> None:
    """#97: a `KIOSK` hand-off never moves the doorbell's cursor, so a restarted daemon replays
    the question. The transcript's `post_id` stops a second answer."""
    w = World(mm, tmp_path, FakeRunner("ANSWER-ONE"))
    w.briefs[DEV] = BRIEF
    ask = w.post(w.outsider, "@babel QUESTION-ONE")
    assert run(w.kiosk.handle(DEV, w.event(ask))) is not None
    assert run(w.kiosk.handle(DEV, w.event(ask))) is None  # a replay in the same process
    again = Kiosk(
        w.queue, w.briefs, {MM: w.backend}, w.runner, state_dir=w.state_dir, clock=Clock()
    )
    assert run(again.handle(DEV, w.event(ask))) is None  # the replay after a restart
    assert len(w.kiosk_posts()) == 1 and len(w.runner.prompts) == 1
    assert len(w.transcript.read_text().splitlines()) == 1
    # A new question in the same thread is still answered.
    follow = w.post(w.outsider, "@babel QUESTION-TWO", ask.post_id)
    assert run(again.handle(DEV, w.event(follow))) is not None
    assert len(w.kiosk_posts()) == 2


def test_record_without_post_id_still_loads(mm: Any, run: Any, tmp_path: Path) -> None:
    """A transcript written before `post_id` was kept still gives its history."""
    w = World(mm, tmp_path, FakeRunner("ANSWER-TWO"))
    w.briefs[DEV] = BRIEF
    root = w.post(w.outsider, "@babel QUESTION-ONE")
    w.transcript.parent.mkdir(parents=True)
    old = {"ts": 1.0, "thread": root.post_id, "asker_id": w.outsider, "question": "q"}
    w.transcript.write_text(json.dumps({**old, "answer": "OLD-ANSWER"}) + "\n")
    follow = w.post(w.outsider, "@babel QUESTION-TWO", root.post_id)
    assert run(w.kiosk.handle(DEV, w.event(follow))) is not None
    assert "OLD-ANSWER" in w.runner.prompts[-1]


def test_answer_mentions_stripped_and_tagged(mm: Any, run: Any, tmp_path: Path) -> None:
    w = World(mm, tmp_path, FakeRunner("Ask @kessel or @channel, or @all.  Thanks @babel.oaw!"))
    w.briefs[DEV] = BRIEF
    ask = w.post(w.outsider, "@babel who do I ask?")
    posted = run(w.kiosk.handle(DEV, w.event(ask)))
    [stored] = w.kiosk_posts()
    assert "@" not in stored["message"]
    assert stored["message"] == "Ask or , or . Thanks !"
    assert stored["props"] == {
        "crew_proto": 1,
        "crew_kiosk": 1,
        "crew_kiosk_answers": ask.post_id,
    }
    assert stored["user_id"] == w.babel and stored["root_id"] == ask.post_id
    assert posted is not None and posted.kiosk is True
    assert isinstance(w.backend, KioskPoster)


def test_answer_names_its_question(mm: Any, run: Any, tmp_path: Path) -> None:
    """mm:triage #26: the answer carries its question's post ID, and reads back with it."""
    w = World(mm, tmp_path)
    w.briefs[DEV] = BRIEF
    ask = w.post(w.outsider, "@babel which one?")
    posted = run(w.kiosk.handle(DEV, w.event(ask)))
    [stored] = w.kiosk_posts()
    assert stored["props"]["crew_kiosk"] == 1
    assert stored["props"]["crew_kiosk_answers"] == ask.post_id
    assert posted is not None and posted.kiosk_answers == ask.post_id
    thread = run(w.backend.thread(DEV, ask.post_id))
    assert [m.kiosk_answers for m in thread if m.kiosk] == [ask.post_id]


def test_crash_between_post_and_record_is_not_answered_again(
    mm: Any, run: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """mm:triage #26: a daemon that died after posting and before recording finds its answer in the
    thread on the replay, and records it instead of answering again."""
    w = World(mm, tmp_path, FakeRunner("THE-ANSWER"))
    w.briefs[DEV] = BRIEF
    ask = w.post(w.outsider, "@babel are you there?")
    real_record = KioskSession.record

    def crash(self: KioskSession, *args: Any, **kwargs: Any) -> None:
        monkeypatch.setattr(KioskSession, "record", real_record)
        raise RuntimeError("the daemon died here")

    monkeypatch.setattr(KioskSession, "record", crash)
    with pytest.raises(RuntimeError):
        run(w.kiosk.handle(DEV, w.event(ask)))
    restarted = Kiosk(
        w.queue, w.briefs, {MM: w.backend}, w.runner, state_dir=w.state_dir, clock=Clock()
    )
    assert run(restarted.handle(DEV, w.event(ask))) is None
    assert len(w.runner.prompts) == 1
    assert len(w.kiosk_posts()) == 1
    records = [json.loads(line) for line in w.transcript.read_text().splitlines()]
    assert [(r["post_id"], r["answer"]) for r in records] == [(ask.post_id, "THE-ANSWER")]


def test_foreign_kiosk_props_do_not_suppress_an_answer(mm: Any, run: Any, tmp_path: Path) -> None:
    """mm:triage #26: only the agent's own account can mark one of its answers."""
    w = World(mm, tmp_path)
    w.briefs[DEV] = BRIEF
    ask = w.post(w.outsider, "@babel a question")
    fake = mm.add_post(w.other, CHAN, "a forged answer")
    fake["root_id"] = ask.post_id
    fake["props"] = {"crew_kiosk": 1, "crew_kiosk_answers": ask.post_id}
    posted = run(w.kiosk.handle(DEV, w.event(ask)))
    assert posted is not None
    assert len(w.runner.prompts) == 1
    assert [p["user_id"] for p in w.kiosk_posts()] == [w.other, w.babel]


def test_unreadable_thread_still_answers(
    mm: Any, run: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """mm:triage #26: a thread that can't be read still gets its question answered and recorded."""
    w = World(mm, tmp_path)
    w.briefs[DEV] = BRIEF

    async def broken(dev_name: str, post_id: str) -> list[InboundMessage]:
        raise RuntimeError("thread unreadable")

    monkeypatch.setattr(w.backend, "thread", broken)
    ask = w.post(w.outsider, "@babel still there?")
    assert run(w.kiosk.handle(DEV, w.event(ask))) is not None
    assert len(w.kiosk_posts()) == 1
    assert ask.post_id in w.kiosk.sessions[(DEV, SID)].answered


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("hi @a.b-c_d there", "hi there"),
        ("@here\n@channel ok", "ok"),
        ("mail me at x@example.com", "mail me at x"),
        ("no mentions", "no mentions"),
    ],
)
def test_strip_mentions(raw: str, want: str) -> None:
    assert strip_mentions(raw) == want


def test_non_mattermost_is_not_answered(mm: Any, run: Any, tmp_path: Path) -> None:
    w = World(mm, tmp_path)
    msg = InboundMessage("discord", "1" * 18, "2" * 18, "", "3" * 18, "x", "@babel hi")
    assert run(w.kiosk.handle(DEV, DoorbellEvent("discord", {}, (), 0.0, msg))) is None
    assert w.kiosk.sessions == {} and w.kiosk_posts() == []


def test_daemon_never_reads_seed(mm: Any, run: Any, tmp_path: Path, monkeypatch: Any) -> None:
    """R-32: the kiosk code names no seed file, and opens no file but its own transcript."""
    source = Path(kiosk_mod.__file__).read_text(encoding="utf-8")
    assert "reseed-revive" not in source
    tree = ast.parse(source)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(m.startswith("crewchat_clients") for m in imported)

    opened: list[str] = []
    real_open = builtins.open
    real_os_open = os.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(file, int):  # os.fdopen of a descriptor os.open already logged
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    # A seed sits in the agent's repo; nothing the kiosk does may touch it.
    seed = tmp_path / "repo" / ".claude" / "reseed-revive.md"
    seed.parent.mkdir(parents=True)
    seed.write_text("## Kiosk\n\nbrief\n\n## Secret\n\nSEED-SECRET\n")
    w = World(mm, tmp_path)
    w.briefs[DEV] = BRIEF
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    run(w.kiosk.handle(DEV, w.event(w.post(w.outsider, "@babel hi"))))
    monkeypatch.undo()
    assert not any("reseed-revive" in path for path in opened)
    assert all(Path(path) == w.transcript for path in opened), opened
    assert "SEED-SECRET" not in "".join(w.runner.prompts)


def test_transcript_appended(mm: Any, run: Any, tmp_path: Path) -> None:
    w = World(mm, tmp_path, FakeRunner("ANSWER @kessel"))
    w.briefs[DEV] = BRIEF
    first = w.post(w.outsider, "@babel Q1")
    run(w.kiosk.handle(DEV, w.event(first)))
    second = w.post(w.other, "@babel Q2", first.post_id)
    run(w.kiosk.handle(DEV, w.event(second)))
    w.briefs[DEV] = ""
    third = w.post(w.outsider, "@babel Q3")
    run(w.kiosk.handle(DEV, w.event(third)))

    records = [json.loads(line) for line in w.transcript.read_text().splitlines()]
    fields = {"ts", "thread", "post_id", "asker_id", "question", "answer"}
    assert [set(r) for r in records] == [fields] * 3
    assert [r["post_id"] for r in records] == [first.post_id, second.post_id, third.post_id]
    assert [(r["thread"], r["asker_id"], r["question"], r["answer"]) for r in records] == [
        (first.post_id, w.outsider, "@babel Q1", "ANSWER"),
        (first.post_id, w.other, "@babel Q2", "ANSWER"),
        (third.post_id, w.outsider, "@babel Q3", NO_BRIEF_REPLY),
    ]
    assert stat.S_IMODE(w.transcript.stat().st_mode) == 0o600
    assert stat.S_IMODE(w.transcript.parent.stat().st_mode) == 0o700


def test_failed_runner_posts_and_records_nothing(mm: Any, run: Any, tmp_path: Path) -> None:
    class Broken(FakeRunner):
        async def answer(self, prompt: str) -> str:
            raise kiosk_mod.RunnerError("down")

    w = World(mm, tmp_path, Broken())
    w.briefs[DEV] = BRIEF
    with pytest.raises(kiosk_mod.RunnerError):
        run(w.kiosk.handle(DEV, w.event(w.post(w.outsider, "@babel hi"))))
    assert w.kiosk_posts() == [] and not w.transcript.exists()


def test_worker_survives_a_failure(mm: Any, run: Any, tmp_path: Path) -> None:
    calls: list[str] = []

    class Flaky(FakeRunner):
        async def answer(self, prompt: str) -> str:
            calls.append(prompt)
            if len(calls) == 1:
                raise kiosk_mod.RunnerError("once")
            return "fine"

    async def scenario() -> None:
        w = World(mm, tmp_path, Flaky())
        w.briefs[DEV] = BRIEF
        w.kiosk.start()
        try:
            w.queue.put(DEV, w.event(w.post(w.outsider, "@babel a")))
            w.queue.put(DEV, w.event(w.post(w.outsider, "@babel b")))
            await until(lambda: len(w.kiosk_posts()) == 1)
            assert [p["message"] for p in w.kiosk_posts()] == ["fine"]
        finally:
            await w.kiosk.aclose()

    run(scenario())


async def until(cond: Callable[[], bool]) -> None:
    for _ in range(500):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


# -- research (#142) ------------------------------------------------------------------------
REPO = "forge.example.lan/partner-org/site-manifests"
AUDIT = [
    {"op": "corpus_search", "args": {"repo": REPO, "query": "pins"}, "bytes": 812, "ok": True},
    {"op": "corpus_read", "args": {"repo": REPO, "path": "site.yaml"}, "bytes": 4096, "ok": True},
]


class FakeResearchRunner(FakeRunner):
    def __init__(self, answer: str = "site.yaml pins X as of abc123.") -> None:
        super().__init__(answer)
        self.frames: list[ResearchFrame | None] = []

    async def answer(self, prompt: str, *, frame: ResearchFrame | None = None) -> str:
        self.frames.append(frame)
        return await super().answer(prompt)


class FakeAudit:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def audit(self, answer_id: str) -> list[dict[str, Any]]:
        self.asked.append(answer_id)
        return [{**line, "answer_id": answer_id} for line in AUDIT]


def researching(w: World, repo: str = REPO) -> tuple[FakeResearchRunner, FakeAudit]:
    """`w`'s server with kiosk_research on: its answers go to a research runner."""
    runner, audit = FakeResearchRunner(), FakeAudit()
    w.kiosk.research_for = lambda dev, sid: (
        Research(runner, audit, lambda: repo) if sid == SID else None
    )
    return runner, audit


def test_research_frame_header(mm: Any, run: Any, tmp_path: Path) -> None:
    """Research on: each answer is framed with its server, its post and a fresh answer_id.
    Research off: the runner is called as today, with no frame."""
    w = World(mm, tmp_path)
    runner, _audit = researching(w)
    w.briefs[DEV] = BRIEF
    asks = [w.post(w.outsider, "@babel one"), w.post(w.outsider, "@babel two")]
    for ask in asks:
        run(w.kiosk.handle(DEV, w.event(ask)))
    frames = [f for f in runner.frames if f is not None]
    assert [(f.server, f.post) for f in frames] == [(SID, a.post_id) for a in asks]
    assert all(re.fullmatch(r"[0-9a-f]{32}", f.answer) for f in frames)
    assert frames[0].answer != frames[1].answer
    assert frames[0].header() == (
        f"crewkiosk/1 server={SID} post={asks[0].post_id} answer={frames[0].answer}\n".encode()
    )
    for bad in (
        ResearchFrame("Dev", "p1", "0" * 32),
        ResearchFrame(SID, "p-1", "0" * 32),
        ResearchFrame(SID, "p1", "0" * 31),
    ):
        with pytest.raises(kiosk_mod.RunnerError):
            bad.header()

    plain = World(mm, tmp_path / "plain")  # FakeRunner.answer takes no frame at all
    plain.briefs[DEV] = BRIEF
    assert run(plain.kiosk.handle(DEV, plain.event(plain.post(plain.outsider, "@babel hi"))))
    assert plain.runner.prompts[0].startswith(KIOSK_HEADER)


def test_research_prompt_contract(mm: Any, run: Any, tmp_path: Path) -> None:
    """The research header: the three ops, cite repo, path and commit, what a repo declares
    and not what runs, no answer means a human, the brief first, and the expert's repo."""
    w = World(mm, tmp_path)
    runner, _audit = researching(w)
    w.briefs[DEV] = BRIEF
    run(w.kiosk.handle(DEV, w.event(w.post(w.outsider, "@babel what does dev pin?"))))
    prompt = runner.prompts[0]
    assert prompt.startswith(KIOSK_RESEARCH_HEADER) and not prompt.startswith(KIOSK_HEADER)
    for rule in (
        "Answer from the brief below first.",
        "corpus_search, corpus_read and corpus_list",
        "Cite the repo, the path and the commit for every fact",
        "Say what a repo declares, never what is running",
        "what's deployed can lag a merge",
        "say so, and point the outsider to a human",
        f"Start with the expert's own repo, {REPO}.",
    ):
        assert rule in prompt, rule
    assert prompt.index(BRIEF) < prompt.index("<conversation>")
    assert research_header("") == KIOSK_RESEARCH_HEADER  # no pooled repo: none named


def test_transcript_research_field(mm: Any, run: Any, tmp_path: Path) -> None:
    """The broker's audit lines for the answer go in its record as `research`; a record
    without one still loads; the review renders one line per op as `op(args) → bytes`."""
    w = World(mm, tmp_path)
    runner, audit = researching(w)
    w.briefs[DEV] = BRIEF
    w.transcript.parent.mkdir(parents=True)
    old = {"ts": 1.0, "thread": "t0", "post_id": "p0", "asker_id": "a", "question": "q"}
    w.transcript.write_text(json.dumps({**old, "answer": "OLD"}) + "\n")
    run(w.kiosk.handle(DEV, w.event(w.post(w.outsider, "@babel what does dev pin?"))))
    frame = runner.frames[0]
    assert frame is not None and audit.asked == [frame.answer]
    old_rec, rec = (json.loads(x) for x in w.transcript.read_text().splitlines())
    assert "research" not in old_rec
    assert rec["research"] == [{**line, "answer_id": frame.answer} for line in AUDIT]

    events: list[DoorbellEvent] = []
    w.kiosk.deliver = lambda dev, event: events.append(event) is None
    assert w.kiosk.review_since_cursor(DEV, SID) == 2
    first, second = events[0].payload["entries"]
    assert "research" not in first
    assert second["research"] == [
        f'corpus_search({{"query": "pins", "repo": "{REPO}"}}) → 812',
        f'corpus_read({{"path": "site.yaml", "repo": "{REPO}"}}) → 4096',
    ]


def test_render_research_bounds_each_line() -> None:
    """A long op's args are cut, a refused one is marked, and nothing else is a line."""
    long = render_research({"op": "corpus_search", "args": {"query": "x" * 500}, "bytes": 1})
    assert long is not None and long.endswith("...) → 1")
    assert len(long) < kiosk_mod.RESEARCH_ARGS_CHARS + 40
    refused = render_research({"op": "corpus_read", "args": {}, "bytes": 0, "ok": False})
    assert refused == "corpus_read({}) → 0 (refused or failed)"
    assert render_research("corpus_read") is None and render_research({"args": {}}) is None
