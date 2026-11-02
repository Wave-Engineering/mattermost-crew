"""#204: `crewchat-waiter`, the doorbell the harness arms (no agent does).

The daemon is the doorbell tests' `FakeDaemon`: a real Unix socket answering one `subscribe`
frame per connection. Time is real but short: the restart grace is shrunk, and the waiter's
own retry pause is a hook that either skips the wait or brings the daemon back.
"""

from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from crewchat_clients import doorbell, waiter
from crewchat_clients.doorbell import (
    EXIT_WAITER,
    SeenSet,
    WaiterLockUnavailable,
    try_waiter_lock,
    waiter_lock_path,
    waiter_running,
)
from crewchat_clients.waiter import EXIT_QUIET, EXIT_WAKE, RETRY_BACKOFF_S, run, wake_text

from tests.unit.clients.test_doorbell import DEV, EVENT, FakeDaemon, Responder, in_turn, ok, with_id


@pytest.fixture(autouse=True)
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doorbell, "RESTART_GRACE_S", 0.2)
    monkeypatch.setattr(doorbell, "RESTART_BACKOFF_S", (0.02, 0.05))


@pytest.fixture
def short_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="cc-wt-", dir="/tmp") as d:
        yield Path(d)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-identity.json").write_text(json.dumps({"dev_name": DEV}))
    return tmp_path


def env_for(repo: Path, **knobs: str) -> dict[str, str]:
    """The waiter's environment. Batching (#212) is OFF unless a test turns it on: a wake that
    waits out the batch window after its event would make every one-event test slow, and the
    tests from before #212 are about one event per wake."""
    env = {
        "XDG_STATE_HOME": str(repo / "state"),
        waiter.BATCH_WINDOW_ENV: "0",
    }  # never the real home
    return {**env, **knobs}


def wait(repo: Path, sock: Path, sleep: Callable[[float], None] | None = None) -> tuple[int, str]:
    err = io.StringIO()
    code = run(repo, sock, env_for(repo), err, sleep=sleep)
    return code, err.getvalue()


def log_of(repo: Path) -> str:
    return (repo / "state" / "crewchat" / "waiter" / f"{DEV}.log").read_text()


def test_an_event_wakes_with_a_pointer_and_the_event(repo: Path, short_dir: Path) -> None:
    """Exit 2 is the wake; stderr is what the model reads: where it came from, what to do, and
    the event as the one JSON line the doorbell prints. The event is recorded as seen and the
    lock is gone with the process."""
    fake = FakeDaemon(short_dir / "d.sock", ok(with_id("e1")))
    try:
        code, err = wait(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_WAKE
    assert err == wake_text(DEV, with_id("e1"))
    assert f"event arrived for {DEV}" in err and "chat_read" in err and "not a message" in err
    assert json.loads(err.splitlines()[-1]) == with_id("e1")
    assert SeenSet.for_dev(env_for(repo), DEV, io.StringIO()).contains("e1")  # type: ignore[union-attr]
    assert not waiter_running(env_for(repo), DEV)


def test_an_outsider_event_keeps_its_marking(repo: Path, short_dir: Path) -> None:
    """The waiter renders an event as the doorbell does (`render_event`)."""
    event = {**EVENT, "event_id": "o1", "outsider": True}
    fake = FakeDaemon(short_dir / "d.sock", ok(event))
    try:
        code, err = wait(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_WAKE
    assert "[from outside the crew]" in json.loads(err.splitlines()[-1])["message"]["text"]


def test_a_second_waiter_is_silent_and_consumes_nothing(repo: Path, short_dir: Path) -> None:
    """One waiter per Dev-Name: a copy that finds the lock held exits 0 with no output and
    never subscribes, so it can neither wake the agent nor eat the event."""
    held = try_waiter_lock(env_for(repo), DEV)
    assert held is not None
    fake = FakeDaemon(short_dir / "d.sock", ok(with_id("e1")))
    try:
        assert waiter_running(env_for(repo), DEV)
        code, err = wait(repo, fake.path)
    finally:
        fake.close()
        os.close(held)
    assert (code, err) == (EXIT_QUIET, "")
    assert fake.frames == []
    assert not waiter_running(env_for(repo), DEV)  # closing the descriptor released it


def test_a_killed_waiter_frees_the_lock(repo: Path) -> None:
    """A session that dies takes its waiter, and the kernel drops the lock with the process:
    the next session's waiter is not locked out."""
    path = waiter_lock_path(env_for(repo), DEV)
    assert path is not None
    holder = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import fcntl, os, sys, time\n"
            "p = sys.argv[1]\n"
            "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        assert waiter_running(env_for(repo), DEV)
    finally:
        holder.kill()
        holder.wait(timeout=10)
    assert not waiter_running(env_for(repo), DEV)
    again = try_waiter_lock(env_for(repo), DEV)
    assert again is not None
    os.close(again)


def test_a_repeat_is_dropped_and_the_waiter_keeps_waiting(repo: Path, short_dir: Path) -> None:
    """An event already seen (a redelivery, a twin) wakes nobody; the next new one does."""
    seen = SeenSet.for_dev(env_for(repo), DEV, io.StringIO())
    assert seen is not None
    seen.record("e1")
    fake = FakeDaemon(short_dir / "d.sock", in_turn(with_id("e1"), with_id("e2")))
    try:
        code, err = wait(repo, fake.path, sleep=lambda s: None)
    finally:
        fake.close()
    assert code == EXIT_WAKE
    assert json.loads(err.splitlines()[-1]) == with_id("e2")
    assert len(fake.frames) == 2  # it subscribed again after the repeat


def test_a_daemon_outage_past_the_grace_does_not_end_it(repo: Path, short_dir: Path) -> None:
    """The restart grace (#129) is 0.2 s here. Past it the one-shot would exit 4; the waiter
    sleeps its retry pause and tries again, and the first event after the daemon is back
    wakes the agent. The agent never sees the outage."""
    sock = short_dir / "d.sock"
    daemon: list[FakeDaemon] = []
    pauses: list[float] = []

    def sleep(seconds: float) -> None:
        pauses.append(seconds)
        if seconds == RETRY_BACKOFF_S[0] and not daemon:
            daemon.append(FakeDaemon(sock, ok(with_id("after"))))  # the daemon is back
        elif seconds != RETRY_BACKOFF_S[0]:
            time.sleep(seconds)  # the restart backoff inside wait_for_new, for real

    try:
        code, err = wait(repo, sock, sleep=sleep)
    finally:
        for d in daemon:
            d.close()
    assert code == EXIT_WAKE
    assert RETRY_BACKOFF_S[0] in pauses
    assert err == wake_text(DEV, with_id("after"))  # the agent never hears of the outage
    assert log_of(repo).count("waiting for the daemon") == 1  # logged once, not on every try


def test_not_registered_past_the_grace_does_not_end_it(repo: Path, short_dir: Path) -> None:
    """A restarted daemon that has not yet re-registered the agent answers `not_registered`;
    past the grace the waiter keeps waiting for the shim's ping to register it again."""
    registered = False

    def respond(frame: dict[str, Any]) -> dict[str, Any]:
        if registered:
            return {"id": frame["id"], "ok": True, "result": with_id("now")}
        return {"id": frame["id"], "ok": False, "error": f"not_registered: {DEV}"}

    fake = FakeDaemon(short_dir / "d.sock", respond)

    def sleep(seconds: float) -> None:
        nonlocal registered
        if seconds == RETRY_BACKOFF_S[0]:
            registered = True
        else:
            time.sleep(seconds)

    try:
        code, err = wait(repo, fake.path, sleep=sleep)
    finally:
        fake.close()
    assert code == EXIT_WAKE
    assert json.loads(err.splitlines()[-1]) == with_id("now")


def test_retry_pause_grows_and_is_capped(repo: Path, short_dir: Path) -> None:
    """A daemon that stays down: 5, 10, 20, 40, then 60 s each time, never an exit."""
    pauses: list[float] = []

    class Stop(Exception):
        pass

    def sleep(seconds: float) -> None:
        if seconds in RETRY_BACKOFF_S:
            pauses.append(seconds)
            if len(pauses) == 7:
                raise Stop
        else:
            time.sleep(seconds)

    with pytest.raises(Stop):
        wait(repo, short_dir / "never.sock", sleep=sleep)
    assert pauses == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0]


def test_no_identity_means_no_wake(tmp_path: Path, short_dir: Path) -> None:
    """A hook in a repo that is not a crew checkout must never wake anyone, nor take a lock."""
    code, err = wait(tmp_path, short_dir / "d.sock")
    assert code == EXIT_QUIET
    assert "agent-identity.json" in err
    assert not (tmp_path / "state").exists()


def test_a_flock_error_is_not_a_held_lock(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only EWOULDBLOCK/EAGAIN mean another waiter holds the lock. A filesystem without flock
    (ENOLCK, EOPNOTSUPP) means no waiter can be running: the doorbell must not yield to a ghost
    and the waiter must not read it as someone else delivering."""
    path = waiter_lock_path(env_for(repo), DEV)
    assert path is not None
    path.parent.mkdir(parents=True)
    path.touch()

    def broken(fd: int, op: int) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(doorbell.fcntl, "flock", broken)
    assert not waiter_running(env_for(repo), DEV)
    with pytest.raises(WaiterLockUnavailable):
        try_waiter_lock(env_for(repo), DEV)


def test_an_unreadable_state_dir_is_no_waiter_not_a_crash(repo: Path) -> None:
    """`waiter_running` is called by the doorbell before anything else: a state directory the
    user can't traverse (EACCES) must read as "no waiter", never raise out of the doorbell."""
    path = waiter_lock_path(env_for(repo), DEV)
    assert path is not None
    path.parent.mkdir(parents=True)
    path.touch()
    path.parent.chmod(0)
    try:
        if os.access(path, os.R_OK):  # running as root: the mode proves nothing
            pytest.skip("permission bits are not enforced for this user")
        assert not waiter_running(env_for(repo), DEV)
    finally:
        path.parent.chmod(0o700)


def test_no_lock_means_no_waiter(tmp_path: Path, short_dir: Path) -> None:
    """A waiter that cannot take a lock does not run unguarded (every Stop hook would start
    another): it exits 0, delivers nothing, and writes nothing the model could read."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-identity.json").write_text(json.dumps({"dev_name": DEV}))
    blocked = tmp_path / "state-is-a-file"
    blocked.write_text("not a directory")
    fake = FakeDaemon(short_dir / "d.sock", ok(with_id("e1")))
    err = io.StringIO()
    try:
        code = run(tmp_path, fake.path, {"XDG_STATE_HOME": str(blocked)}, err)
    finally:
        fake.close()
    assert (code, err.getvalue()) == (EXIT_QUIET, "")
    assert fake.frames == []


def test_an_unsafe_dev_name_touches_nothing_outside_the_state_dir(
    tmp_path: Path, short_dir: Path
) -> None:
    """The lock, seen and log paths all refuse a Dev-Name that is not a plain file name."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-identity.json").write_text(json.dumps({"dev_name": "../x"}))
    state = tmp_path / "state"
    code, err = wait(tmp_path, short_dir / "d.sock")
    assert (code, err) == (EXIT_QUIET, "")
    assert not state.exists() and not (tmp_path / "x.log").exists()
    assert not (tmp_path / "state" / "crewchat" / "x.log").exists()


def test_the_doorbell_yields_to_a_live_waiter(repo: Path, short_dir: Path) -> None:
    """While a waiter holds the Dev-Name's lock the one-shot exits 5 at once and never
    subscribes (a second consumer would double every wake); with none, it is unchanged."""
    fake = FakeDaemon(short_dir / "d.sock", ok(with_id("e1")))
    held = try_waiter_lock(env_for(repo), DEV)
    assert held is not None
    out, err = io.StringIO(), io.StringIO()
    try:
        code = doorbell.run(repo, fake.path, env_for(repo), out, err)
        assert (code, out.getvalue()) == (EXIT_WAITER, "")
        assert "already delivers" in err.getvalue() and fake.frames == []
        os.close(held)
        out, err = io.StringIO(), io.StringIO()
        code = doorbell.run(repo, fake.path, env_for(repo), out, err)
    finally:
        fake.close()
    assert code == doorbell.EXIT_EVENT
    assert json.loads(out.getvalue()) == with_id("e1")


def test_the_entry_point_is_declared() -> None:
    import tomllib

    toml = Path(waiter.__file__).parents[2].joinpath("pyproject.toml")
    scripts = tomllib.loads(toml.read_text())["project"]["scripts"]
    assert scripts["crewchat-waiter"] == "crewchat_clients.waiter:main"


# -- #212: one wake delivers every queued event ---------------------------------------------

WINDOW = "0.3"  # the batch window here: long enough that a loaded runner still gets the reply


class CountingErr(io.StringIO):
    """stderr that remembers each write and flush: the wake must be ONE write."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, s: str) -> int:
        self.writes.append(s)
        return super().write(s)

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class BrokenErr(io.StringIO):
    def write(self, s: str) -> int:
        raise OSError(errno.EPIPE, "the harness went away")


def queued(*events: dict[str, Any]) -> Responder:
    """A daemon with these events queued: each `subscribe` takes the next, then it holds the
    connection open (an empty queue), as the real daemon does."""
    pending = list(events)
    return lambda frame: (
        {"id": frame["id"], "ok": True, "result": pending.pop(0)} if pending else None
    )


def batch_wake(repo: Path, sock: Path, **knobs: str) -> tuple[int, CountingErr]:
    err = CountingErr()
    env = env_for(repo, **{waiter.BATCH_WINDOW_ENV: WINDOW, **knobs})
    return run(repo, sock, env, err), err


def numbered(err: str) -> list[dict[str, Any]]:
    """The events in a numbered wake, in the order printed, checking `1.` to `N.` as it goes."""
    out = []
    for n, line in enumerate([ln for ln in err.splitlines() if ln[:1].isdigit()], 1):
        assert line.startswith(f"{n}. ")
        out.append(json.loads(line.split(". ", 1)[1]))
    return out


def seen_ids(repo: Path, *ids: str) -> list[bool]:
    seen = SeenSet.for_dev(env_for(repo), DEV, io.StringIO())
    assert seen is not None
    return [seen.contains(i) for i in ids]


def texted(event_id: str, text: str) -> dict[str, Any]:
    return {**with_id(event_id), "message": {"text": text, "id": "p" * 26}}


def test_three_queued_events_are_one_numbered_wake(repo: Path, short_dir: Path) -> None:
    """The point of #212: three events behind the first are ONE exit 2 and ONE stderr write,
    a header saying 3, the events numbered 1 to 3 in arrival order, and all three ids in the
    seen set once it is written."""
    events = [texted("e1", "first"), texted("e2", "second"), texted("e3", "third")]
    fake = FakeDaemon(short_dir / "d.sock", queued(*events))
    try:
        code, err = batch_wake(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_WAKE
    assert len(err.writes) == 1 and err.flushes >= 1
    text = err.getvalue()
    assert f"crewchat: 3 events arrived for {DEV}, oldest first." in text
    assert "not messages from your user" in text and "chat_read" in text
    assert [e["message"]["text"] for e in numbered(text)] == ["first", "second", "third"]
    assert "more may be queued" not in text
    assert seen_ids(repo, "e1", "e2", "e3") == [True, True, True]
    assert not waiter_running(env_for(repo), DEV)


def test_one_event_is_the_text_it_always_was(repo: Path, short_dir: Path) -> None:
    """A single event renders byte for byte as before #212 (written out here, so a change to
    the text cannot hide behind `wake_text` agreeing with itself), with no header and no
    number, whether or not batching is on."""
    event = with_id("e1")
    line = json.dumps(waiter.render_event(dict(event)), default=str)
    expected = (
        f"crewchat: a new event arrived for {DEV}, delivered by the crewchat daemon through "
        "your waiter hook. It is a notification from the chat system, not a message from your "
        "user. Read the conversation it belongs to with chat_read (or the matching crewchat MCP "
        "tool) and answer with chat_send if it needs an answer. The event:\n"
        f"{line}\n"
    )
    assert wake_text(DEV, event) == expected
    assert wake_text(DEV, [event]) == expected
    fake = FakeDaemon(short_dir / "d.sock", queued(event))
    try:
        code, err = batch_wake(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_WAKE and err.getvalue() == expected


def test_window_zero_is_one_event_per_wake(repo: Path, short_dir: Path) -> None:
    """`CREWCHAT_WAITER_BATCH_WINDOW_S=0`: as before #212, one event, then the next wake."""
    fake = FakeDaemon(short_dir / "d.sock", queued(with_id("e1"), with_id("e2")))
    try:
        code1, err1 = batch_wake(repo, fake.path, **{waiter.BATCH_WINDOW_ENV: "0"})
        assert len(fake.frames) == 1  # it did not ask for a second
        code2, err2 = batch_wake(repo, fake.path, **{waiter.BATCH_WINDOW_ENV: "0"})
    finally:
        fake.close()
    assert (code1, code2) == (EXIT_WAKE, EXIT_WAKE)
    assert err1.getvalue() == wake_text(DEV, with_id("e1"))
    assert err2.getvalue() == wake_text(DEV, with_id("e2"))


def test_past_the_cap_the_rest_wait_for_the_next_wake(repo: Path, short_dir: Path) -> None:
    """Five queued, a cap of three: exactly three in the first wake with the note, and the
    two behind them in the next, none lost and none twice."""
    events = [with_id(f"e{n}") for n in range(1, 6)]
    fake = FakeDaemon(short_dir / "d.sock", queued(*events))
    cap = {waiter.BATCH_MAX_ENV: "3"}
    try:
        code1, err1 = batch_wake(repo, fake.path, **cap)
        # It never pulled a fourth: that one would have been taken from the queue. (Asserted
        # before the next wake, which would block on an emptied queue if it had.)
        assert len(fake.frames) == 3
        code2, err2 = batch_wake(repo, fake.path, **cap)
    finally:
        fake.close()
    assert (code1, code2) == (EXIT_WAKE, EXIT_WAKE)
    first, second = numbered(err1.getvalue()), numbered(err2.getvalue())
    assert [e["event_id"] for e in first] == ["e1", "e2", "e3"]
    assert "more may be queued" in err1.getvalue()
    assert f"crewchat: 2 events arrived for {DEV}" in err2.getvalue()
    assert [e["event_id"] for e in second] == ["e4", "e5"]
    assert "more may be queued" not in err2.getvalue()


def test_past_the_text_budget_the_rest_wait_for_the_next_wake(
    repo: Path, short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same for the text budget. It is checked before each pull, so the event that crosses
    it is kept (the daemon has already handed it over); the next one is left in the queue."""
    events = [texted(f"e{n}", "x" * 200) for n in range(1, 6)]
    one = len(waiter.event_line(events[0])) + 1
    monkeypatch.setattr(waiter, "BATCH_TEXT_BUDGET", one + one // 2)  # one fits, two cross it
    fake = FakeDaemon(short_dir / "d.sock", queued(*events))
    try:
        code1, err1 = batch_wake(repo, fake.path)
        assert len(fake.frames) == 2  # before the next wake, which would block on an empty queue
        code2, err2 = batch_wake(repo, fake.path)
        code3, err3 = batch_wake(repo, fake.path)
    finally:
        fake.close()
    assert (code1, code2, code3) == (EXIT_WAKE, EXIT_WAKE, EXIT_WAKE)
    assert "more may be queued" in err1.getvalue()
    delivered = [e["event_id"] for err in (err1, err2, err3) for e in _events_of(err.getvalue())]
    assert delivered == ["e1", "e2", "e3", "e4", "e5"]


def _events_of(text: str) -> list[dict[str, Any]]:
    """The events in a wake of one or many."""
    if "events arrived for" in text:
        return numbered(text)
    return [json.loads(text.splitlines()[-1])]


def test_a_repeated_id_inside_the_batch_is_delivered_once(repo: Path, short_dir: Path) -> None:
    """The seen set learns a batch's ids only after the write, so the waiter drops a repeat of
    an id already in the batch itself."""
    fake = FakeDaemon(short_dir / "d.sock", queued(with_id("e1"), with_id("e1"), with_id("e2")))
    try:
        code, err = batch_wake(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_WAKE
    assert [e["event_id"] for e in numbered(err.getvalue())] == ["e1", "e2"]
    assert f"crewchat: 2 events arrived for {DEV}" in err.getvalue()


def test_a_failed_write_records_nothing(repo: Path, short_dir: Path) -> None:
    """Ids are recorded only after the whole text is out. A write that fails records none, so
    events a daemon later hands out again (it does after a restart) are not read as repeats:
    the next wake carries all three. This does NOT make the lost write recoverable: the daemon
    has already acked the events the failed wake held, so only a restart's replay gives them
    back (see the runbook's Delivery note)."""
    events = [with_id("e1"), with_id("e2"), with_id("e3")]
    fake = FakeDaemon(short_dir / "d.sock", queued(*events))
    env = env_for(repo, **{waiter.BATCH_WINDOW_ENV: WINDOW})
    try:
        with pytest.raises(OSError):
            run(repo, fake.path, env, BrokenErr())
    finally:
        fake.close()
    assert seen_ids(repo, "e1", "e2", "e3") == [False, False, False]
    assert not waiter_running(env_for(repo), DEV)
    replay = FakeDaemon(short_dir / "r.sock", queued(*events))
    try:
        code, err = batch_wake(repo, replay.path)
    finally:
        replay.close()
    assert code == EXIT_WAKE
    assert [e["event_id"] for e in numbered(err.getvalue())] == ["e1", "e2", "e3"]
    assert seen_ids(repo, "e1", "e2", "e3") == [True, True, True]


def test_an_outage_after_the_first_events_still_delivers_them(repo: Path, short_dir: Path) -> None:
    """The daemon closes on the third call (a restart): the two events already received are
    delivered, not discarded, and the outage goes to the log, never to the model."""
    pending = [with_id("e1"), with_id("e2")]

    def respond(frame: dict[str, Any]) -> dict[str, Any] | bytes | None:
        if pending:
            return {"id": frame["id"], "ok": True, "result": pending.pop(0)}
        return b""  # closed without a reply: `Down`

    fake = FakeDaemon(short_dir / "d.sock", respond)
    try:
        code, err = batch_wake(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_WAKE
    assert [e["event_id"] for e in numbered(err.getvalue())] == ["e1", "e2"]
    assert "daemon" not in err.getvalue().split("oldest first.", 1)[1]  # nothing of the outage
    assert "delivering the 2 collected" in log_of(repo)
    assert seen_ids(repo, "e1", "e2") == [True, True]


def test_a_backlog_of_repeats_is_not_a_wake_of_its_own(repo: Path, short_dir: Path) -> None:
    """Events seen before are dropped as ever; the first new one starts the wake and the ones
    behind it join it."""
    seen = SeenSet.for_dev(env_for(repo), DEV, io.StringIO())
    assert seen is not None
    seen.record("old")
    fake = FakeDaemon(short_dir / "d.sock", queued(with_id("old"), with_id("n1"), with_id("n2")))
    try:
        code, err = batch_wake(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_WAKE
    assert [e["event_id"] for e in numbered(err.getvalue())] == ["n1", "n2"]


def test_the_knobs_fall_back_to_their_defaults_when_unusable() -> None:
    """A typo in either knob is logged and ignored: it must not stop the waiter, nor turn
    batching off or unbounded by accident."""
    log = io.StringIO()
    default = waiter.BatchSettings(1.0, 20, 16 * 1024)
    for bad in ("", "soon", "-1", "nan", "inf"):
        assert waiter.batch_settings({waiter.BATCH_WINDOW_ENV: bad}, log) == default
    for bad in ("", "many", "0", "-3", "2.5"):
        assert waiter.batch_settings({waiter.BATCH_MAX_ENV: bad}, log) == default
    assert waiter.BATCH_WINDOW_ENV in log.getvalue() and waiter.BATCH_MAX_ENV in log.getvalue()
    assert waiter.batch_settings({}, log) == default and default.on
    assert not waiter.batch_settings({waiter.BATCH_WINDOW_ENV: "0"}, log).on
    assert not waiter.batch_settings({waiter.BATCH_MAX_ENV: "1"}, log).on
    assert waiter.batch_settings(
        {waiter.BATCH_WINDOW_ENV: "2.5", waiter.BATCH_MAX_ENV: "7"}, log
    ) == (waiter.BatchSettings(2.5, 7, 16 * 1024))
