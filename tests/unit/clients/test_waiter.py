"""#204: `crewchat-waiter`, the doorbell the harness arms (no agent does).

The daemon is the doorbell tests' `FakeDaemon`: a real Unix socket answering one `subscribe`
frame per connection. Time is real but short: the restart grace is shrunk, and the waiter's
own retry pause is a hook that either skips the wait or brings the daemon back.
"""

from __future__ import annotations

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
    try_waiter_lock,
    waiter_lock_path,
    waiter_running,
)
from crewchat_clients.waiter import EXIT_QUIET, EXIT_WAKE, RETRY_BACKOFF_S, run, wake_text

from tests.unit.clients.test_doorbell import DEV, EVENT, FakeDaemon, in_turn, ok, with_id


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


def env_for(repo: Path) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(repo / "state")}  # never the real home


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
    held = try_waiter_lock(env_for(repo), DEV, io.StringIO())
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
    again = try_waiter_lock(env_for(repo), DEV, io.StringIO())
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


def test_the_doorbell_yields_to_a_live_waiter(repo: Path, short_dir: Path) -> None:
    """While a waiter holds the Dev-Name's lock the one-shot exits 5 at once and never
    subscribes (a second consumer would double every wake); with none, it is unchanged."""
    fake = FakeDaemon(short_dir / "d.sock", ok(with_id("e1")))
    held = try_waiter_lock(env_for(repo), DEV, io.StringIO())
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
