"""Story 3.4: the thin doorbell one-shot (Dev Spec §5.8; R-26, CT-08).

The daemon is `FakeDaemon`: a real Unix socket served from a thread, answering one
`subscribe` frame per connection the way `crewchat serve` does (or not answering at all).
"""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from crewchat_clients import doorbell
from crewchat_clients.doorbell import (
    EXIT_ERROR,
    EXIT_EVENT,
    EXIT_NOT_REGISTERED,
    EXIT_TIMEOUT,
    EXIT_UNREACHABLE,
    parse_timeout,
    run,
)

DEV = "bell"
EVENT = {
    "type": "message",
    "backend": "mattermost:ci",
    "message": {"text": "two\nlines, and ünïcode", "id": "p" * 26},
}

# What the fake does with a frame: return the reply to send (a dict, or raw bytes), or `None`
# to hold the connection open without answering.
Responder = Callable[[dict[str, Any]], dict[str, Any] | bytes | None]


class FakeDaemon:
    def __init__(self, path: Path, respond: Responder) -> None:
        self.path = path
        self.respond = respond
        self.frames: list[dict[str, Any]] = []
        self.hangups = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:  # a client that connected and then failed to send
                    continue
                frame = json.loads(buf)
                self.frames.append(frame)
                reply = self.respond(frame)
                if reply is None:
                    if not conn.recv(1):  # the client hung up (as it does on a timeout)
                        self.hangups += 1
                    continue
                data = reply if isinstance(reply, bytes) else json.dumps(reply).encode() + b"\n"
                conn.sendall(data)

    def close(self) -> None:
        self.sock.close()


def ok(event: Any) -> Responder:
    return lambda frame: {"id": frame["id"], "ok": True, "result": event}


def fail(error: str) -> Responder:
    return lambda frame: {"id": frame["id"], "ok": False, "error": error}


GRACE_S = 0.3  # the restart grace in these tests, for a 60 s RESTART_GRACE_S


@pytest.fixture(autouse=True)
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doorbell, "RESTART_GRACE_S", GRACE_S)
    monkeypatch.setattr(doorbell, "RESTART_BACKOFF_S", (0.02, 0.05))


@pytest.fixture
def short_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="cc-db-", dir="/tmp") as d:
        yield Path(d)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-identity.json").write_text(json.dumps({"dev_name": DEV}))
    return tmp_path


def ring(repo: Path, sock: Path, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    environ = {"XDG_STATE_HOME": str(repo / "state"), **(env or {})}  # never the real home
    code = run(repo, sock, environ, out, err)
    return code, out.getvalue(), err.getvalue()


def in_turn(*events: dict[str, Any]) -> Responder:
    """Answers each `subscribe` with the next event, then holds the connection open."""
    pending = list(events)
    return lambda frame: (
        {"id": frame["id"], "ok": True, "result": pending.pop(0)} if pending else None
    )


def seen_file(repo: Path, dev: str = DEV) -> Path:
    return repo / "state" / "crewchat" / "doorbell-seen" / dev


def with_id(event_id: str) -> dict[str, Any]:
    return {**EVENT, "event_id": event_id}


def test_exit_0_on_event_prints_json(repo: Path, short_dir: Path) -> None:
    """One event: one `subscribe{dev_name}` frame, one JSON line on stdout, exit 0."""
    fake = FakeDaemon(short_dir / "d.sock", ok(EVENT))
    try:
        code, out, err = ring(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_EVENT, err
    assert out.endswith("\n") and out.count("\n") == 1
    assert json.loads(out) == EVENT
    [frame] = fake.frames
    assert frame["op"] == "subscribe"
    assert frame["args"] == {"dev_name": DEV}
    assert frame["proto"] == 1


def test_exit_codes_2_3_4(repo: Path, short_dir: Path) -> None:
    """Not registered is 2, a timeout is 3, and an absent socket is 4; none prints a line."""
    fake = FakeDaemon(short_dir / "nr.sock", fail(f"not_registered: {DEV}"))
    try:
        code, out, _ = ring(repo, fake.path)
    finally:
        fake.close()
    assert (code, out) == (EXIT_NOT_REGISTERED, "")

    fake = FakeDaemon(short_dir / "slow.sock", lambda frame: None)
    try:
        code, out, err = ring(repo, fake.path, {"CREWCHAT_DOORBELL_TIMEOUT": "0.3"})
        fake.thread.join(timeout=5)
    finally:
        fake.close()
    assert (code, out) == (EXIT_TIMEOUT, ""), err
    assert fake.hangups == 1  # it hung up, which cancels the daemon's subscribe

    code, out, _ = ring(repo, short_dir / "absent.sock")
    assert (code, out) == (EXIT_UNREACHABLE, "")


@pytest.mark.parametrize(
    "reply",
    [
        b"",  # closed without a reply
        b"not json\n",
        b'{"id": "someone-else", "ok": true, "result": {"type": "message"}}\n',
    ],
)
def test_cut_short_or_malformed_reply_is_unreachable(
    repo: Path, short_dir: Path, reply: bytes
) -> None:
    fake = FakeDaemon(short_dir / "d.sock", lambda frame: reply)
    try:
        code, out, _ = ring(repo, fake.path)
    finally:
        fake.close()
    assert (code, out) == (EXIT_UNREACHABLE, "")
    # A hang-up is a restart's first sign, retried through the grace (#129); a bad reply is not.
    assert (len(fake.frames) > 1) == (reply == b"")


def test_refused_socket_is_unreachable(repo: Path, short_dir: Path) -> None:
    """A socket file with no daemon behind it (it died) is unreachable, not an error."""
    path = short_dir / "dead.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()
    code, out, _ = ring(repo, path)
    assert (code, out) == (EXIT_UNREACHABLE, "")


@pytest.mark.parametrize("result", [{}, None, [], {"backend": "discord"}])
def test_never_exits_0_without_an_event(repo: Path, short_dir: Path, result: Any) -> None:
    fake = FakeDaemon(short_dir / "d.sock", ok(result))
    try:
        code, out, _ = ring(repo, fake.path)
    finally:
        fake.close()
    assert code != EXIT_EVENT
    assert out == ""


def test_other_daemon_error_is_1(repo: Path, short_dir: Path) -> None:
    fake = FakeDaemon(short_dir / "d.sock", fail("bad_request: dev_name is a required string"))
    try:
        code, out, err = ring(repo, fake.path)
    finally:
        fake.close()
    assert (code, out) == (EXIT_ERROR, "")
    assert "bad_request" in err


def test_no_identity_is_1_and_sends_nothing(tmp_path: Path, short_dir: Path) -> None:
    fake = FakeDaemon(short_dir / "d.sock", ok(EVENT))
    try:
        code, out, err = ring(tmp_path, fake.path)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "agent-identity.json").write_text('{"dev_team": "oaw"}')
        code2, _, err2 = ring(tmp_path, fake.path)
    finally:
        fake.close()
    assert (code, out) == (EXIT_ERROR, "")
    assert "agent-identity.json" in err
    assert code2 == EXIT_ERROR and "dev_name" in err2
    assert fake.frames == []


@pytest.mark.parametrize(
    ("raw", "want"),
    [(None, None), ("", None), ("0", None), (" 0 ", None), ("2.5", 2.5), ("30", 30.0)],
)
def test_timeout_env(raw: str | None, want: float | None) -> None:
    env = {} if raw is None else {"CREWCHAT_DOORBELL_TIMEOUT": raw}
    assert parse_timeout(env) == want


@pytest.mark.parametrize("raw", ["-1", "soon", "nan", "inf"])
def test_bad_timeout_is_1(repo: Path, short_dir: Path, raw: str) -> None:
    code, out, err = ring(repo, short_dir / "absent.sock", {"CREWCHAT_DOORBELL_TIMEOUT": raw})
    assert (code, out) == (EXIT_ERROR, "")
    assert "CREWCHAT_DOORBELL_TIMEOUT" in err


def test_main_reads_socket_and_repo_root(
    repo: Path, short_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeDaemon(short_dir / "d.sock", ok(EVENT))
    try:
        code = doorbell.main(["--socket", str(fake.path), "--repo-root", str(repo)])
    finally:
        fake.close()
    assert code == EXIT_EVENT
    assert json.loads(capsys.readouterr().out) == EVENT


def test_console_script_is_declared() -> None:
    pyproject = Path(__file__).parents[3] / "packages" / "clients" / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
    assert scripts["crewchat-doorbell"] == "crewchat_clients.doorbell:main"


# -- repeats (#109) -------------------------------------------------------------------------
def test_repeat_is_dropped_and_next_event_exits_0(repo: Path, short_dir: Path) -> None:
    """A repeat of a printed event prints nothing: the one-shot subscribes again and exits 0
    on the next new event."""
    fake = FakeDaemon(
        short_dir / "d.sock", in_turn(with_id("mm:A"), with_id("mm:A"), with_id("mm:B"))
    )
    try:
        first = ring(repo, fake.path)
        second = ring(repo, fake.path)
    finally:
        fake.close()
    assert first[0] == EXIT_EVENT and json.loads(first[1])["event_id"] == "mm:A"
    assert second[0] == EXIT_EVENT, second[2]
    assert second[1].count("\n") == 1 and json.loads(second[1])["event_id"] == "mm:B"
    assert len(fake.frames) == 3  # the second one-shot subscribed twice
    assert seen_file(repo).read_text().splitlines() == ["mm:A", "mm:B"]


def test_seen_set_is_per_dev_name_not_checkout(tmp_path: Path, short_dir: Path) -> None:
    """Two checkouts with one Dev-Name share the seen set, as they share the daemon's queue."""
    state = str(tmp_path / "state")
    repos = []
    for name in ("one", "two"):
        root = tmp_path / name / ".claude"
        root.mkdir(parents=True)
        (root / "agent-identity.json").write_text(json.dumps({"dev_name": DEV}))
        repos.append(root.parent)
    fake = FakeDaemon(
        short_dir / "d.sock", in_turn(with_id("mm:A"), with_id("mm:A"), with_id("mm:B"))
    )
    try:
        ring(repos[0], fake.path, {"XDG_STATE_HOME": state})
        code, out, _ = ring(repos[1], fake.path, {"XDG_STATE_HOME": state})
    finally:
        fake.close()
    assert code == EXIT_EVENT and json.loads(out)["event_id"] == "mm:B"


def test_id_recorded_only_after_flush(repo: Path, short_dir: Path) -> None:
    """A one-shot that dies writing its line keeps no ID: the event comes again, not lost."""

    class Broken(io.StringIO):
        def flush(self) -> None:
            raise BrokenPipeError("stdout closed")

    fake = FakeDaemon(short_dir / "d.sock", ok(with_id("mm:A")))
    env = {"XDG_STATE_HOME": str(repo / "state")}
    try:
        with pytest.raises(BrokenPipeError):
            run(repo, fake.path, env, Broken(), io.StringIO())
    finally:
        fake.close()
    assert not seen_file(repo).exists()


def test_concurrent_one_shots_record_both(tmp_path: Path) -> None:
    """Two processes recording at once lose nothing: `record` holds an exclusive lock."""
    path = tmp_path / "seen"
    script = (
        "import io, sys; from pathlib import Path;"
        "from crewchat_clients.doorbell import SeenSet;"
        "s = SeenSet(Path(sys.argv[1]), io.StringIO());"
        "[s.record(f'{sys.argv[2]}:{n}') for n in range(150)]"
    )
    procs = [subprocess.Popen([sys.executable, "-c", script, str(path), tag]) for tag in ("x", "y")]
    assert [p.wait(60) for p in procs] == [0, 0]
    ids = path.read_text().splitlines()
    assert sorted(ids) == sorted([f"x:{n}" for n in range(150)] + [f"y:{n}" for n in range(150)])


def test_seen_set_capped_at_512(tmp_path: Path) -> None:
    seen = doorbell.SeenSet(tmp_path / "seen", io.StringIO())
    for n in range(600):
        seen.record(f"mm:{n}")
    ids = (tmp_path / "seen").read_text().splitlines()
    assert ids == [f"mm:{n}" for n in range(88, 600)]
    assert stat.S_IMODE((tmp_path / "seen").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "garbage", [b"\xff\xfe\x00", b"mm:A\nnot an id\n"], ids=["binary", "spaces"]
)
def test_corrupt_seen_file_warns_and_delivers(repo: Path, short_dir: Path, garbage: bytes) -> None:
    """A seen file that can't be trusted is empty, with a warning. It never stops delivery."""
    seen_file(repo).parent.mkdir(parents=True)
    seen_file(repo).write_bytes(garbage)
    fake = FakeDaemon(short_dir / "d.sock", ok(with_id("mm:A")))
    try:
        code, out, err = ring(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_EVENT and json.loads(out)["event_id"] == "mm:A"
    assert "treated as empty" in err
    assert seen_file(repo).read_text() == "mm:A\n"


def test_timeout_spans_resubscribes(repo: Path, short_dir: Path) -> None:
    """Nothing but repeats: the one timeout ends the whole run, not each subscribe."""
    seen_file(repo).parent.mkdir(parents=True)
    seen_file(repo).write_text("mm:A\n")
    fake = FakeDaemon(short_dir / "d.sock", ok(with_id("mm:A")))
    start = time.monotonic()
    try:
        code, out, err = ring(repo, fake.path, {"CREWCHAT_DOORBELL_TIMEOUT": "1"})
    finally:
        fake.close()
    assert code == EXIT_TIMEOUT, err
    assert out == ""
    assert time.monotonic() - start < 2.5
    assert len(fake.frames) > 1


def test_event_without_id_is_printed_not_recorded(repo: Path, short_dir: Path) -> None:
    """An older daemon's event, with no `event_id`, is delivered as before."""
    fake = FakeDaemon(short_dir / "d.sock", ok(EVENT))
    try:
        code, out, _ = ring(repo, fake.path)
    finally:
        fake.close()
    assert code == EXIT_EVENT and json.loads(out) == EVENT
    assert not seen_file(repo).exists()


# -- a daemon restart (#129) -----------------------------------------------------------------
def scripted(*replies: bytes | str | dict[str, Any]) -> Responder:
    """One reply per `subscribe`, in order: raw bytes, `"nr"` for `not_registered`, or an
    event. After the last, it holds the connection open."""
    pending = list(replies)

    def respond(frame: dict[str, Any]) -> dict[str, Any] | bytes | None:
        if not pending:
            return None
        reply = pending.pop(0)
        if reply == "nr":
            return {"id": frame["id"], "ok": False, "error": f"not_registered: {DEV}"}
        if isinstance(reply, bytes):
            return reply
        return {"id": frame["id"], "ok": True, "result": reply}

    return respond


def test_waits_through_a_daemon_restart(
    repo: Path, short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart as it happens: no socket while systemd restarts the daemon, then a socket
    whose first answer is a hang-up, then `not_registered` until the shim's ping registers
    again, then the event. The one-shot rides it out and exits 0 with the event."""
    monkeypatch.setattr(doorbell, "RESTART_GRACE_S", 5.0)
    path = short_dir / "d.sock"
    fakes: list[FakeDaemon] = []

    def come_back() -> None:
        time.sleep(0.3)
        fakes.append(FakeDaemon(path, scripted(b"", "nr", "nr", with_id("mattermost:p1"))))

    starter = threading.Thread(target=come_back)
    starter.start()
    try:
        code, out, err = ring(repo, path)
    finally:
        starter.join()
        for fake in fakes:
            fake.close()
    assert code == EXIT_EVENT, err
    assert json.loads(out) == with_id("mattermost:p1")
    assert len(fakes[0].frames) == 4


class FakeClock:
    """The doorbell's clock (#156): a `now` that only `sleep` moves, so no assertion races a
    loaded host's real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(doorbell, "_clock", fake)
    monkeypatch.setattr(doorbell, "_sleep", fake.sleep)
    return fake


@pytest.mark.parametrize(
    ("replies", "want"),
    [((), EXIT_UNREACHABLE), (("nr",) * 1000, EXIT_NOT_REGISTERED)],
    ids=["down", "unregistered"],
)
def test_an_outage_past_the_grace_exits(
    repo: Path, short_dir: Path, clock: FakeClock, replies: tuple[str, ...], want: int
) -> None:
    """A daemon still down after the grace is 4, an agent still unregistered is 2, as before
    #129; only the wait is new. The last pause is capped at the grace, so it lasts exactly
    that."""
    fake = FakeDaemon(short_dir / "d.sock", scripted(*replies)) if replies else None
    path = fake.path if fake else short_dir / "absent.sock"
    start = clock.now
    try:
        code, out, err = ring(repo, path)
    finally:
        if fake:
            fake.close()
    assert (code, out) == (want, "")
    assert GRACE_S <= clock.now - start <= GRACE_S + 1e-9
    assert f"for {GRACE_S:.0f} s" in err


@pytest.mark.parametrize("replies", [(), ("nr",) * 1000], ids=["down", "unregistered"])
def test_a_timeout_inside_an_outage_reports_the_outage(
    repo: Path,
    short_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    replies: tuple[str, ...],
) -> None:
    """The timeout ends first, mid-outage: the exit says what is wrong (4 or 2), not 3."""
    monkeypatch.setattr(doorbell, "RESTART_GRACE_S", 30.0)
    fake = FakeDaemon(short_dir / "d.sock", scripted(*replies)) if replies else None
    path = fake.path if fake else short_dir / "absent.sock"
    start, real_start = clock.now, time.monotonic()
    try:
        code, out, _ = ring(repo, path, {"CREWCHAT_DOORBELL_TIMEOUT": "5"})
    finally:
        if fake:
            fake.close()
    assert (code, out) == ((EXIT_NOT_REGISTERED if replies else EXIT_UNREACHABLE), "")
    assert clock.now - start == pytest.approx(5)
    assert time.monotonic() - real_start < 5


@pytest.mark.parametrize(
    ("timeout_s", "want"),
    [
        # 0.25 s left after the first pause: too short for the daemon's answer, so the
        # timeout ends inside the outage (#51).
        (0.5, doorbell.NotRegistered),
        # 9.75 s left: the daemon answered nothing, so it held the agent again. The outage
        # was over, and this is a plain timeout, exit 3.
        (10.0, doorbell.TimedOut),
    ],
    ids=["inside-the-outage", "after-it-ended"],
)
def test_a_socket_timeout_mid_outage_reports_the_outage(
    monkeypatch: pytest.MonkeyPatch, short_dir: Path, timeout_s: float, want: type[Exception]
) -> None:
    """#156: a `subscribe` whose socket times out while an outage is recorded raises that
    outage, as the docstring promises, but only when its budget was too short for the daemon
    to answer `not_registered`. With time for that answer, the outage had ended."""
    replies: list[Exception] = [
        doorbell.NotRegistered("bell is not registered"),
        doorbell.TimedOut("no event"),
    ]

    def subscribe(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise replies.pop(0)

    monkeypatch.setattr(doorbell, "subscribe", subscribe)
    clock = FakeClock()
    with pytest.raises(want):
        doorbell.wait_for_new(
            short_dir / "d.sock", DEV, timeout_s, None, grace_s=30.0, clock=clock, sleep=clock.sleep
        )
    assert replies == []


def broken_pipe_on_send(monkeypatch: pytest.MonkeyPatch, times: int | None) -> None:
    """The one-shot's sockets fail `sendall` with `BrokenPipeError`, as against a daemon that
    closed between our connect and our send: the first `times` sends (every one when None).
    Only the one-shot's `socket` is replaced, so the fake daemon's own sockets are untouched."""
    sends = 0

    class Flaky(socket.socket):
        def sendall(self, data: Any, flags: int = 0) -> None:
            nonlocal sends
            sends += 1
            if times is None or sends <= times:
                raise BrokenPipeError(32, "Broken pipe")
            super().sendall(data, flags)

    fake_socket = SimpleNamespace(
        socket=Flaky, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM
    )
    monkeypatch.setattr(doorbell, "socket", fake_socket)


def test_broken_pipe_on_send_is_a_restart(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#152: a `BrokenPipeError` on send is the daemon's shutdown window, retried in the grace,
    not unreachable at once."""
    broken_pipe_on_send(monkeypatch, times=1)
    fake = FakeDaemon(short_dir / "d.sock", ok(with_id("mattermost:p1")))
    try:
        event, event_id = doorbell.wait_for_new(fake.path, DEV, 5, None, grace_s=5)
    finally:
        fake.close()
    assert (event, event_id) == (with_id("mattermost:p1"), "mattermost:p1")


def test_broken_pipe_past_the_grace_exits_4(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#152: a send that keeps breaking is a daemon down: exit 4, but only after the grace."""
    broken_pipe_on_send(monkeypatch, times=None)
    fake = FakeDaemon(short_dir / "d.sock", ok(EVENT))
    start = time.monotonic()
    try:
        with pytest.raises(doorbell.Down) as raised:
            doorbell.wait_for_new(fake.path, DEV, None, None, grace_s=GRACE_S)
    finally:
        fake.close()
    assert raised.value.code == EXIT_UNREACHABLE
    assert GRACE_S <= time.monotonic() - start < GRACE_S + 2


def test_no_permission_is_unreachable_at_once(repo: Path, short_dir: Path) -> None:
    """A socket the agent may not open is a broken host, not a restart: no grace."""
    if os.geteuid() == 0:
        pytest.skip("root opens any socket")
    fake = FakeDaemon(short_dir / "d.sock", ok(EVENT))
    fake.path.chmod(0)
    start = time.monotonic()
    try:
        code, out, err = ring(repo, fake.path)
    finally:
        fake.close()
    assert (code, out) == (EXIT_UNREACHABLE, ""), err
    assert "PermissionError" in err and fake.frames == []
    assert time.monotonic() - start < GRACE_S
