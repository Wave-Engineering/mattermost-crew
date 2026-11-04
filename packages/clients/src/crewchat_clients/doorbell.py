"""The thin doorbell one-shot: `crewchat-doorbell` (Dev Spec §5.8; Story 3.4; R-26, CT-08).

An agent runs it as a background task. It sends one `subscribe{dev_name}` frame to the
daemon's socket, waits for the one event the daemon hands over, prints it as **one JSON line**
on stdout and exits 0. The agent wakes on that exit, reads through the MCP, and re-arms. There
is no Monitor primitive on Bedrock, so a task's exit is the only wake-up there is (CT-08).

- **Who.** The Dev-Name is read from `.claude/agent-identity.json` at the repo root (the
  `git rev-parse --show-toplevel` of the working directory, or `--repo-root`), the same file
  the MCP shim reads. The one-shot never registers: the shim does that, and a daemon that
  doesn't know the agent answers `not_registered`.
- **How long.** `CREWCHAT_DOORBELL_TIMEOUT` is the wait in seconds; the default `0` waits
  forever. On a timeout the one-shot hangs up, which cancels the daemon's `subscribe`.
- **Outsider events.** An event marked `"outsider": true` is printed with its text prefixed
  `[from outside the crew]` (`mcp_shim.render_event`): the `text` of the kiosk review (Story
  3.6), and `message.text` of an outsider message delivered under social (Story 3.3, #97).
- **Repeats** (#109). Delivery is at least once (§5.8): an overlapping second `subscribe`, a
  re-arm racing a live one-shot or two sessions with one Dev-Name, gets the held event again.
  The agent re-arms on every exit, so a repeat that exited 0 would have it react twice. The
  one-shot keeps the last `SEEN_MAX` `event_id`s it printed for the Dev-Name, in
  `${XDG_STATE_HOME:-~/.local/state}/crewchat/doorbell-seen/<dev_name>`. On a repeat it prints
  nothing and subscribes again, within the same timeout. An ID is kept only once its line is
  flushed, so a crash in between gives a repeat, never a loss. A seen file it can't read is
  warned about and treated as empty: a repeat is tolerable, a one-shot that stops delivering
  is not.

- **A daemon restart** (#129). The restart closes the connection, the socket is missing or
  refuses while the daemon starts, and the new daemon answers `not_registered` until the
  shim's `ping` registers the agent again. Each is retried for `RESTART_GRACE_S` (60 s) from
  the first failure, and only an outage that lasts longer exits, with 4 or 2. A timeout that
  ends inside an outage exits with the outage's code.

Exit codes (§5.8). It **never** exits 0 without printing an event:

| code | meaning |
|---|---|
| 0 | an event, printed as one JSON line |
| 1 | a usage error: no identity, a bad timeout, or a daemon error other than the ones below |
| 2 | not registered, past the restart grace |
| 3 | timeout |
| 4 | daemon unreachable: down past the restart grace; or at once for no permission, a connect |
|   | timeout, or a reply that is oversized, malformed or mismatched |
| 5 | a `crewchat-waiter` is already delivering this Dev-Name's events (#204): nothing to arm |

**Nobody has to arm this.** `crewchat-waiter` (#204) is the same wait, started and restarted by
the harness through an `asyncRewake` hook, one per Dev-Name. While one holds its lock this
one-shot would be a second consumer of the same queue and double every wake, so it exits 5
at once instead, with a line saying why. Agents whose skills still say to arm it lose nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import socket
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from crewchat_clients.mcp_shim import (
    IDENTITY_FILE,
    MAX_FRAME_BYTES,
    PROTO,
    SOCKET_PATH,
    find_repo_root,
    render_event,
)

EXIT_EVENT = 0
EXIT_ERROR = 1
EXIT_NOT_REGISTERED = 2
EXIT_TIMEOUT = 3
EXIT_UNREACHABLE = 4
EXIT_WAITER = 5

TIMEOUT_ENV = "CREWCHAT_DOORBELL_TIMEOUT"
CONNECT_TIMEOUT_S = 10.0
RECV_CHUNK = 65536

SEEN_MAX = 512  # the `event_id`s kept per Dev-Name (#109)
SEEN_DIR = Path("crewchat") / "doorbell-seen"
WAITER_DIR = Path("crewchat") / "waiter"  # one lock file per Dev-Name (#204)
# A Dev-Name safe as a file name, and an `event_id` safe as one line of the seen file.
SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EVENT_ID_RE = re.compile(r"[^\s]{1,512}")
# How long to pause before subscribing again after the same repeat came back twice running:
# the daemon acks a repeat as it writes it, so a third in a row is a daemon fault, not a race.
REPEAT_BACKOFF_S = (0.0, 0.05, 0.2, 1.0)
# How long a daemon restart may last before the one-shot gives up (#129): the daemon going away
# and coming back, then the shim's ping (every 15 s) registering the agent again.
RESTART_GRACE_S = 60.0
RESTART_BACKOFF_S = (0.25, 0.5, 1.0, 2.0)
# The daemon answers a subscribe for an agent it doesn't hold at once (`not_registered`). So a
# socket timeout on a subscribe given this long or more means it held the agent: the outage was
# over. One given less could not tell, and a timeout there still ends inside the outage (#156).
REPLY_S = 1.0
# A connect that fails like this is a daemon that is stopped or starting, not a broken host, or
# a daemon that closed between our connect and our send (its shutdown window).
DOWN_ERRORS = (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError)
# The clock and the pause `run` waits with: tests swap them, so no assertion races real time
# (#156).
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


class DoorbellExit(Exception):
    """Ends the one-shot with `code`; the message goes to stderr."""

    code = EXIT_ERROR


class Restarting(DoorbellExit):
    """What a daemon restart looks like from here (#129): retried for `RESTART_GRACE_S`, and
    only an outage that lasts longer ends the one-shot."""


class NotRegistered(Restarting):
    code = EXIT_NOT_REGISTERED


class TimedOut(DoorbellExit):
    code = EXIT_TIMEOUT


class Unreachable(DoorbellExit):
    code = EXIT_UNREACHABLE


class Down(Unreachable, Restarting):
    """No socket, a refused connect, or a connection closed before the reply."""


def read_dev_name(repo_root: Path) -> str:
    """The `dev_name` in the repo's identity file."""
    path = repo_root / IDENTITY_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DoorbellExit(f"{path} does not exist yet; run /name first") from exc
    except (OSError, ValueError) as exc:
        raise DoorbellExit(f"cannot read {path}: {exc}") from exc
    dev_name = raw.get("dev_name") if isinstance(raw, dict) else None
    if not isinstance(dev_name, str) or not dev_name:
        raise DoorbellExit(f"{path} names no dev_name; run /name first")
    return dev_name


def parse_timeout(environ: Mapping[str, str]) -> float | None:
    """`CREWCHAT_DOORBELL_TIMEOUT` in seconds; `None` (forever) when unset, empty or 0."""
    raw = environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise DoorbellExit(f"{TIMEOUT_ENV}={raw!r} is not a number of seconds") from exc
    if value != value or value < 0 or value == float("inf"):
        raise DoorbellExit(f"{TIMEOUT_ENV}={raw!r} is not a number of seconds >= 0")
    return value or None


def state_home(environ: Mapping[str, str]) -> Path:
    """`$XDG_STATE_HOME`, or `~/.local/state` when it is unset, empty or relative."""
    state = environ.get("XDG_STATE_HOME", "")
    if not os.path.isabs(state):
        home = environ.get("HOME") or str(Path.home())
        state = os.path.join(home, ".local", "state")
    return Path(state)


def waiter_lock_path(environ: Mapping[str, str], dev_name: str) -> Path | None:
    """`dev_name`'s waiter lock file (#204), or None when the name can't be a file name."""
    if not SAFE_NAME_RE.fullmatch(dev_name):
        return None
    return state_home(environ) / WAITER_DIR / f"{dev_name}.lock"


class WaiterLockUnavailable(Exception):
    """The Dev-Name's waiter lock can't be made or taken on this host (a name that can't be a
    file, an unwritable state directory, a filesystem without `flock`). Not "another waiter
    holds it": the waiter then stays out of it and the doorbell, which sees no waiter, works."""


def try_waiter_lock(environ: Mapping[str, str], dev_name: str) -> int | None:
    """Take `dev_name`'s waiter lock for the life of the file descriptor returned: None when
    another waiter holds it; `WaiterLockUnavailable` when no lock can be had here. A waiter
    with no lock would be started again by every `Stop` hook and pile up, so it doesn't run."""
    path = waiter_lock_path(environ, dev_name)
    if path is None:
        raise WaiterLockUnavailable(f"{dev_name!r} can't name a lock file")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise WaiterLockUnavailable(f"{path}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None  # held
        raise WaiterLockUnavailable(f"flock {path}: {exc}") from exc
    return fd


def waiter_running(environ: Mapping[str, str], dev_name: str) -> bool:
    """Whether a `crewchat-waiter` holds `dev_name`'s lock right now (#204). Only a lock that
    is held counts: a host where `flock` fails some other way has no waiter that could be."""
    path = waiter_lock_path(environ, dev_name)
    if path is None:
        return False
    try:
        fd = os.open(path, os.O_RDWR)  # no O_CREAT: a missing file is no waiter
    except OSError:  # missing, or a state directory this user can't read: no waiter to yield to
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        return exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN)
    finally:
        os.close(fd)  # taking it and closing releases it at once
    return False


class SeenSet:
    """The `event_id`s this Dev-Name's one-shots have printed, newest last, at most `SEEN_MAX`
    (#109). Shared by every one-shot with that Dev-Name, in any checkout, as the daemon's
    queue is. Every failure here is a warning: the worst it can cause is a repeat."""

    def __init__(self, path: Path, err: TextIO) -> None:
        self.path = path
        self.err = err

    @classmethod
    def for_dev(cls, environ: Mapping[str, str], dev_name: str, err: TextIO) -> SeenSet | None:
        """The seen set for `dev_name`, or None (no repeat is dropped) when it has no home."""
        if not SAFE_NAME_RE.fullmatch(dev_name):
            print(f"crewchat-doorbell: {dev_name!r} can't name a seen file; repeats pass", file=err)
            return None
        return cls(state_home(environ) / SEEN_DIR / dev_name, err)

    def _warn(self, what: str, exc: BaseException) -> None:
        print(f"crewchat-doorbell: seen file {self.path} {what}: {exc}", file=self.err)

    def _read(self) -> list[str]:
        try:
            ids = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            self._warn("unreadable, treated as empty", exc)
            return []
        if not all(EVENT_ID_RE.fullmatch(i) for i in ids):
            self._warn("corrupt, treated as empty", ValueError("a line is not an event_id"))
            return []
        return ids

    def contains(self, event_id: str) -> bool:
        return event_id in self._read()

    def record(self, event_id: str) -> None:
        """Keep `event_id`, under an exclusive lock: two one-shots may record at once."""
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = os.open(
                self.path.with_name(self.path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600
            )
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                ids = [*(i for i in self._read() if i != event_id), event_id][-SEEN_MAX:]
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write("".join(i + "\n" for i in ids))
                    os.replace(tmp, self.path)  # mkstemp made it 0600
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                    raise
            finally:
                os.close(lock)  # and with it the lock
        except OSError as exc:
            self._warn("not written, so this event may come again", exc)


def subscribe(
    sock_path: Path,
    dev_name: str,
    timeout_s: float | None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """One `subscribe{dev_name}` frame and its reply: the event, or a `DoorbellExit`."""
    deadline = None if timeout_s is None else clock() + timeout_s
    req_id = str(uuid.uuid4())
    frame = {"proto": PROTO, "id": req_id, "op": "subscribe", "args": {"dev_name": dev_name}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.settimeout(CONNECT_TIMEOUT_S)
            sock.connect(str(sock_path))
            sock.sendall(json.dumps(frame).encode() + b"\n")
        except DOWN_ERRORS as exc:  # absent, or refused: stopped or starting
            raise Down(f"crewchat daemon at {sock_path}: {exc!r}") from exc
        except OSError as exc:  # no permission, or a connect timeout
            raise Unreachable(f"crewchat daemon at {sock_path}: {exc!r}") from exc
        buf = b""
        while not buf.endswith(b"\n"):
            if deadline is None:
                sock.settimeout(None)
            else:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise TimedOut(f"no event within {timeout_s:g} s")
                sock.settimeout(remaining)
            try:
                chunk = sock.recv(RECV_CHUNK)
            except TimeoutError as exc:
                raise TimedOut(f"no event within {timeout_s:g} s") from exc
            except DOWN_ERRORS as exc:
                raise Down(f"crewchat daemon at {sock_path}: {exc!r}") from exc
            except OSError as exc:
                raise Unreachable(f"crewchat daemon at {sock_path}: {exc!r}") from exc
            if not chunk:  # a stop closes every connection: the restart's first sign
                raise Down(f"crewchat daemon at {sock_path} closed without a reply")
            buf += chunk
            if len(buf) > MAX_FRAME_BYTES:
                raise Unreachable(f"crewchat daemon at {sock_path} sent an oversized reply")
    try:
        reply = json.loads(buf)
    except ValueError as exc:
        raise Unreachable("crewchat daemon sent a malformed reply to subscribe") from exc
    if not isinstance(reply, dict) or reply.get("id") != req_id:
        raise Unreachable("crewchat daemon sent a mismatched reply to subscribe")
    if not reply.get("ok"):
        error = str(reply.get("error", "error"))
        if error.split(":", 1)[0].strip() == "not_registered":
            raise NotRegistered(f"{dev_name} is not registered with the crewchat daemon")
        raise DoorbellExit(f"crewchat subscribe: {error}")
    event = reply.get("result")
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise DoorbellExit("crewchat daemon answered subscribe without an event")
    return event


def run(
    repo_root: Path,
    sock_path: Path,
    environ: Mapping[str, str],
    out: TextIO,
    err: TextIO,
) -> int:
    """The one-shot, apart from argv: its exit code. Only an event's path returns 0, and
    only for an event this Dev-Name hasn't been given before (#109)."""
    try:
        timeout_s = parse_timeout(environ)
        dev_name = read_dev_name(repo_root)
        if waiter_running(environ, dev_name):
            print(
                f"crewchat-doorbell: crewchat-waiter already delivers events for {dev_name}; "
                "nothing to arm",
                file=err,
            )
            return EXIT_WAITER
        seen = SeenSet.for_dev(environ, dev_name, err)
        event, event_id = wait_for_new(
            sock_path, dev_name, timeout_s, seen, clock=_clock, sleep=_sleep
        )
    except DoorbellExit as exc:
        print(f"crewchat-doorbell: {exc}", file=err)
        return exc.code
    # `json.dumps` without `indent` never emits a newline: the event is exactly one line.
    out.write(json.dumps(render_event(event)) + "\n")
    out.flush()
    if seen is not None and event_id is not None:
        seen.record(event_id)  # only now: a crash before this gives a repeat, never a loss
    return EXIT_EVENT


def wait_for_new(
    sock_path: Path,
    dev_name: str,
    timeout_s: float | None,
    seen: SeenSet | None,
    *,
    grace_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], str | None]:
    """The first event whose `event_id` isn't in `seen`, and that ID (None for an event from
    a daemon that gives none). A repeat's reply was written, so the daemon has acked it, and
    the next `subscribe`, on a fresh connection, waits for the event after it. One timeout
    spans them all.

    A daemon restart (#129) is waited through: while `subscribe` fails as `Restarting` (the
    daemon down, then not yet holding the agent again), it is retried until the outage has
    lasted `grace_s` (default `RESTART_GRACE_S`), and only then raised. A timeout that ends
    inside an outage raises the outage, not `TimedOut`: the agent should hear that the daemon
    is down."""
    grace_s = RESTART_GRACE_S if grace_s is None else grace_s
    deadline = None if timeout_s is None else clock() + timeout_s
    last, again = None, 0
    outage: float | None = None  # when the current run of `Restarting` failures began
    failure: Restarting | None = None  # its latest
    tries = 0
    while True:
        remaining = None if deadline is None else deadline - clock()
        if remaining is not None and remaining <= 0:
            if failure is not None:
                raise failure
            raise TimedOut(f"no new event within {timeout_s:g} s")
        try:
            event = subscribe(sock_path, dev_name, remaining, clock=clock)
        except TimedOut as exc:
            # The socket's own timeout. Inside an outage only when the budget was too short for
            # the daemon's answer; with time for it, the daemon held the agent again (#156).
            if failure is not None and remaining is not None and remaining < REPLY_S:
                raise failure from exc
            raise TimedOut(f"no new event within {timeout_s:g} s") from exc
        except Restarting as exc:
            now = clock()
            outage = now if outage is None else outage
            failure = exc
            # One subtraction decides both the exit and the last pause, so they agree to the
            # last bit: computed two ways, they can disagree, and a pause of 0 never ends.
            left = grace_s - (now - outage)
            if left <= 0:
                raise type(exc)(f"{exc}, for {now - outage:.0f} s") from exc
            pause = RESTART_BACKOFF_S[min(tries, len(RESTART_BACKOFF_S) - 1)]
            pause = min(pause, left)
            if deadline is not None:
                pause = min(pause, deadline - now)
            tries += 1
            sleep(max(pause, 0.0))
            continue
        outage, failure, tries = None, None, 0
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
            return event, None
        if seen is None or not seen.contains(event_id):
            return event, event_id
        again = again + 1 if event_id == last else 0
        last = event_id
        pause = REPEAT_BACKOFF_S[min(again, len(REPEAT_BACKOFF_S) - 1)]
        if pause:
            sleep(pause if remaining is None else min(pause, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crewchat-doorbell", description="Wait for one event from the crewchat daemon."
    )
    parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
    parser.add_argument("--repo-root", type=Path, default=None)
    ns = parser.parse_args(argv)
    root = ns.repo_root or find_repo_root(Path.cwd())
    return run(root, ns.socket, os.environ, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
