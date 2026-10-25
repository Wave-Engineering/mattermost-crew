"""Story 3.3: the loop guard (R-25; IT-10's unit side).

Messages from bot accounts need an opted-in conversation (the agent started it or posted in
it, it was mentioned into it, or it is a DM) and an inline mention, and are dropped past
`loop_guard_window` bot messages in the conversation's window. Human messages are admitted
when addressed (R-16 to R-19), with no mention needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from crewchat.adapters.base import InboundMessage
from crewchat.filters import LoopGuard
from crewchat.registry import Registration
from crewchat.threads import MyThreads

MM = "mattermost:dev"
MINE = "root-mine"  # a thread babel started


class Mono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


REG = Registration(
    dev_name="babel",
    crew="oaw",
    session_id="s",
    account="babel.oaw",
    backends=[MM, "discord"],
    needs_list="bj:needs",
    registered_at=datetime(2026, 10, 24, tzinfo=UTC),
    dev_team="wave",
)

_n = iter(range(1, 1_000_000))


def msg(
    text: str,
    *,
    bot: bool = True,
    root: str = "",
    dm: bool = False,
    backend: str = MM,
) -> InboundMessage:
    return InboundMessage(
        backend=backend,
        channel="chan",
        post_id=f"p{next(_n)}",
        root_id=root,
        author_id="bot-kessel" if bot else "h-oaw",
        author_name="kessel.oaw" if bot else "bj",
        text=text,
        is_bot=bot,
        dm=dm,
    )


@pytest.fixture
def guard() -> tuple[LoopGuard, Mono]:
    threads = MyThreads()
    threads.add("babel", MINE, MM)
    mono = Mono()
    return LoopGuard(threads, window=20, monotonic=mono), mono


def test_loop_guard_window(guard: tuple[LoopGuard, Mono]) -> None:
    g, mono = guard
    verdicts = []
    for _ in range(21):
        mono.t += 1
        verdicts.append(g.admit(msg("@babel your turn", root=MINE), REG))
    assert verdicts == [True] * 20 + [False]  # the 21st bot message in the window
    # Another conversation has its own window.
    assert g.admit(msg("@babel elsewhere", dm=True), REG) is True
    # The window slides: an hour on, the conversation is admitted again.
    mono.t += 3601
    assert g.admit(msg("@babel later", root=MINE), REG) is True


def test_loop_guard_window_configurable() -> None:
    threads = MyThreads()
    threads.add("babel", MINE, MM)
    g = LoopGuard(threads, window=3, monotonic=Mono())
    got = [g.admit(msg("@agents go", root=MINE), REG) for _ in range(4)]
    assert got == [True, True, True, False]


def test_loop_guard_requires_mention_and_optin(guard: tuple[LoopGuard, Mono]) -> None:
    g, _mono = guard
    # Opted in (babel's own thread) but no mention: dropped.
    assert g.admit(msg("thanks, done", root=MINE), REG) is False
    # A DM is opted in, but still needs the mention.
    assert g.admit(msg("hello", dm=True), REG) is False
    assert g.admit(msg("@babel hello", dm=True), REG) is True
    # A mention but no opt-in: a broadcast in a thread babel never joined.
    assert g.admit(msg("@agents roll call", root="root-other"), REG) is False
    assert g.admit(msg("@agents roll call"), REG) is False  # a new root post, likewise
    # Both: admitted.
    assert g.admit(msg("@agents roll call", root=MINE), REG) is True
    # Mentioned into it: once someone names babel in a thread, the thread is opted in.
    assert g.admit(msg("@babel take a look", bot=False, root="root-new"), REG) is True
    assert g.admit(msg("@oaw anyone?", root="root-new"), REG) is True
    # A direct mention opts the conversation in by itself.
    assert g.admit(msg("@babel.oaw can you check?", root="root-third"), REG) is True


@pytest.mark.parametrize(
    "token", ["@babel", "@babel.oaw", "@agents", "@oaw", "@wave", "@BABEL", "@Babel.OAW"]
)
def test_loop_guard_mention_set(guard: tuple[LoopGuard, Mono], token: str) -> None:
    g, _mono = guard
    # On Mattermost the team token counts for an author of our own crew (D-103).
    assert g.admit(msg(f"{token} ping", root=MINE), REG, author_crew="oaw") is True


def test_loop_guard_not_a_mention(guard: tuple[LoopGuard, Mono]) -> None:
    g, _mono = guard
    for text in ("babel ping", "@kessel ping", "mail babel@example.com", "@all ping"):
        assert g.admit(msg(text, root=MINE), REG) is False, text
    # The team token from an author of no known crew is not a mention (D-103).
    assert g.admit(msg("@wave ping", root=MINE), REG, author_crew=None) is False
    # On Discord, `@wave` and `@all` (the transition broadcast) are mentions.
    assert g.admit(msg("@wave ping", root="d-root", backend="discord"), REG) is False  # not yet
    g.threads.add("babel", "d-root", "discord")
    assert g.admit(msg("@wave ping", root="d-root", backend="discord"), REG) is True
    assert g.admit(msg("@all ping", root="d-root", backend="discord"), REG) is True


def test_human_addressed_admitted_without_mention(guard: tuple[LoopGuard, Mono]) -> None:
    g, mono = guard
    # The human partner's first DM, with no mention.
    assert g.admit(msg("are you there?", bot=False, dm=True), REG) is True
    # A human reply in babel's thread, with no mention.
    assert g.admit(msg("looks good", bot=False, root=MINE), REG) is True
    # Not addressed at all: not admitted.
    assert g.admit(msg("lunch?", bot=False), REG) is False
    # Humans are never counted against the window.
    for _ in range(50):
        mono.t += 1
        assert g.admit(msg("more", bot=False, root=MINE), REG) is True
