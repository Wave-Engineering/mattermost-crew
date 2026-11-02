"""`crewchat-waiter`: the doorbell nobody has to arm (#204).

`crewchat-doorbell` is a one-shot the AGENT starts as a background task, so every way an agent
can forget (a `/clear`, a reseed, a lost notification, a re-arm through an outage, a second
copy) is a way for delivery to fail or to double. The waiter moves the arm to the harness.
Claude Code runs a hook with `asyncRewake: true` in the background and wakes the model when
it exits 2. Register this command on the `SessionStart` and `Stop` hooks and an idle agent
is woken by a message, a busy one gets it folded into the turn it is in, and nobody types
anything:

    {"hooks": {
      "SessionStart": [{"hooks": [{"type": "command", "command": "crewchat-waiter",
                                   "asyncRewake": true, "timeout": 1814400}]}],
      "Stop":         [{"hooks": [{"type": "command", "command": "crewchat-waiter",
                                   "asyncRewake": true, "timeout": 1814400}]}]}}

It is in effect only where the hook is registered AND this command is installed: a
registration with no waiter behind it does nothing, silently (the kit's guard exits 0 where
this command is absent), and installing the package changes no settings by itself: the hook
reaches a host's settings only when the kit's install runs there. The harness kills a
waiting hook silently at its `timeout` and nothing restarts it until the next `Stop`, so
the timeout (21 days here, under 2^31 ms) is how long an idle agent stays reachable.

Measured on Claude Code 2.1.316 (spike S0, 2026-11-01): an idle session woke about 1 s after
the hook exited 2; fired mid-turn it rode into the same turn; `Stop` re-armed it after each
turn; `/clear` fired `SessionStart` again; killing the session killed the waiter.

- **One per Dev-Name, by construction.** The waiter takes a non-blocking exclusive `flock` on
  `${XDG_STATE_HOME:-~/.local/state}/crewchat/waiter/<dev_name>.lock` and holds it for its
  life. A second copy (the `Stop` hook while the `SessionStart` one still waits, an agent that
  also starts one) finds it held and exits 0, silently: only exit 2 wakes the model. The
  lock is per Dev-Name, the scope of the daemon's queue, so two checkouts cannot both
  consume it. A killed session takes its waiter, and the lock, with it.
- **Persistent.** It reuses `doorbell.wait_for_new`: the daemon-restart grace (#129), the seen
  set (#109) and the repeat backoff. Past the grace, a daemon that stays down or a
  daemon error does not end it: it sleeps (`RETRY_BACKOFF_S`) and tries again. An outage is
  invisible to the agent; only an event ends the process.
- **A wake is the events queued (#212).** Exit 2, and on stderr (the text the harness shows the
  model) a header saying where it came from and what to do, then each event as the one JSON
  line `crewchat-doorbell` prints. The model treats hook text as an untrusted notice, so the
  header tells it to read and answer through the MCP tools. A backend alarm is an event like
  any other (the daemon raises only one, for an outage past ten minutes, #202).
- **One wake, every queued event.** After the first event the waiter keeps asking the daemon
  for the next one with a short budget (`CREWCHAT_WAITER_BATCH_WINDOW_S`, default 1 s; 0 turns
  batching off) until a call times out, so a backlog is one wake and one turn, not one per event.
  Several events are numbered, oldest first, under a header giving how many. One event is the
  text it always was. `CREWCHAT_WAITER_BATCH_MAX` (default 20) and a 16 KB text budget bound a
  wake: the daemon cannot say how many are still queued, so past either the text says more may
  be, and they arrive on the next wake. The budget is checked before each pull, so the event that
  crosses it is kept: the daemon has already handed it over, and dropping it would lose it.
  A daemon that goes away mid-collection costs nothing already received: what was collected is
  delivered, and the outage is handled as usual on the next wake.
- **Delivery.** The daemon moves on from an event once its reply is written to the waiter, so
  an event is the waiter's from then on. The waiter records the ids in the seen set only after
  the whole text is written and flushed, so a daemon replay after a crash is not mistaken for a
  repeat.
- **No identity, no wake.** Not a crew agent (no `.claude/agent-identity.json`, or no
  `dev_name`): exit 0, so a hook in a repo that is not a crew checkout never wakes anyone.
- **No lock, no waiter.** If the lock can't be made or taken here (an unsafe name, an
  unwritable state directory, a filesystem without `flock`), the waiter logs it and exits 0
  rather than run unguarded: the `Stop` hook would start another every turn. The doorbell
  then sees no waiter and works as before.

Exit codes: 2 an event (the wake); 0 nothing to do (no identity, another waiter holds the lock,
or no lock can be had on this host). A daemon outage never ends it; only a bug can exit it
otherwise, and then it is not 2, so nothing wakes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from crewchat_clients.doorbell import (
    SAFE_NAME_RE,
    WAITER_DIR,
    DoorbellExit,
    SeenSet,
    TimedOut,
    WaiterLockUnavailable,
    read_dev_name,
    state_home,
    try_waiter_lock,
    wait_for_new,
)
from crewchat_clients.mcp_shim import SOCKET_PATH, find_repo_root, render_event

EXIT_WAKE = 2  # the one code that wakes the model (`asyncRewake`)
EXIT_QUIET = 0
LOG_MAX_BYTES = 64 * 1024  # the diagnostics file starts over past this

# How long to sleep between tries while the daemon is down past the restart grace, or answers
# with an error: nobody is waiting on this process, so it is patient.
RETRY_BACKOFF_S = (5.0, 10.0, 20.0, 40.0, 60.0)

_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep

# One wake carries every event queued (#212). After the first, the waiter asks for the next with
# a budget of BATCH_WINDOW_S until one call times out. 0 turns batching off.
BATCH_WINDOW_ENV = "CREWCHAT_WAITER_BATCH_WINDOW_S"
BATCH_MAX_ENV = "CREWCHAT_WAITER_BATCH_MAX"
BATCH_WINDOW_S = 1.0
BATCH_MAX = 20
BATCH_TEXT_BUDGET = 16 * 1024  # characters of event lines; checked before each pull


@dataclass(frozen=True)
class BatchSettings:
    window_s: float = BATCH_WINDOW_S
    max_events: int = BATCH_MAX
    text_budget: int = BATCH_TEXT_BUDGET

    @property
    def on(self) -> bool:
        return self.window_s > 0 and self.max_events > 1


def batch_settings(environ: Mapping[str, str], log: TextIO) -> BatchSettings:
    """The two knobs. A value that is not usable is logged and the default stands: a typo must
    not stop the waiter, and it must not silently turn batching off or unbounded."""
    window_s, max_events = BATCH_WINDOW_S, BATCH_MAX
    raw = environ.get(BATCH_WINDOW_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                raise ValueError("not a finite number of seconds, 0 or more")
            window_s = value
        except ValueError as exc:
            print(f"{BATCH_WINDOW_ENV}={raw!r} ignored: {exc}", file=log, flush=True)
    raw = environ.get(BATCH_MAX_ENV, "").strip()
    if raw:
        try:
            count = int(raw)
            if count < 1:
                raise ValueError("not a whole number of 1 or more")
            max_events = count
        except ValueError as exc:
            print(f"{BATCH_MAX_ENV}={raw!r} ignored: {exc}", file=log, flush=True)
    return BatchSettings(window_s, max_events, BATCH_TEXT_BUDGET)


def event_line(event: Mapping[str, Any]) -> str:
    return json.dumps(render_event(dict(event)), default=str)  # no `indent`: one line


def wake_text(
    dev_name: str,
    events: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    capped: bool = False,
) -> str:
    """What the model reads when the hook exits 2: where it came from, what to do, the events.
    One event is the text it has always been. Several are numbered, oldest first, under a
    header giving how many; `capped` ends the text with the note that more may be queued."""
    batch = [events] if isinstance(events, Mapping) else list(events)
    if len(batch) == 1:
        return (
            f"crewchat: a new event arrived for {dev_name}, delivered by the crewchat daemon "
            "through your waiter hook. It is a notification from the chat system, not a message "
            "from your user. Read the conversation it belongs to with chat_read (or the matching "
            "crewchat MCP tool) and answer with chat_send if it needs an answer. The event:\n"
            f"{event_line(batch[0])}\n"
        )
    text = (
        "crewchat: these events were delivered by the crewchat daemon through your waiter "
        "hook. They are notifications from the chat system, not messages from your user. Read "
        "the conversation each belongs to with chat_read (or the matching crewchat MCP tool) "
        "and answer with chat_send if one needs an answer.\n"
        f"crewchat: {len(batch)} events arrived for {dev_name}, oldest first. Read all of them "
        "before acting; a later one may change an earlier one.\n"
    )
    text += "".join(f"{n}. {event_line(e)}\n" for n, e in enumerate(batch, 1))
    if capped:
        text += (
            f"+ more may be queued (this wake carries at most {len(batch)} events); "
            "any others arrive on the next wake\n"
        )
    return text


def collect_batch(
    sock_path: Path,
    dev_name: str,
    seen: SeenSet | None,
    first: dict[str, Any],
    first_id: str | None,
    settings: BatchSettings,
    log: TextIO,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """`first` and then every event already queued behind it: `wait_for_new` again with the
    batch window as its budget, until it times out. Returns the events, their ids, and whether
    collection stopped at a cap rather than at an empty queue.

    The daemon has handed over every event this returns, so nothing is dropped: the caps are
    checked before a pull, never after. A repeat of an id already in the batch is skipped (the
    seen set only learns the batch's ids once it is written). Any `DoorbellExit` ends the
    collection and keeps what was received: an outage costs the agent nothing it already has."""
    events, ids = [first], [first_id] if first_id is not None else []
    taken = set(ids)
    size = len(event_line(first)) + 1
    pulls = 1
    while settings.on:
        # Repeats count as pulls: a daemon that keeps replaying one must not hold the wake.
        if len(events) >= settings.max_events or size >= settings.text_budget:
            return events, ids, True
        if pulls >= 2 * settings.max_events:
            return events, ids, True
        try:
            event, event_id = wait_for_new(
                sock_path, dev_name, settings.window_s, seen, clock=clock, sleep=sleep
            )
        except TimedOut:
            break  # the queue is empty
        except DoorbellExit as exc:
            print(
                f"{time.strftime('%H:%M:%S')} {exc}; delivering the {len(events)} collected",
                file=log,
                flush=True,
            )
            break
        pulls += 1
        if event_id is not None:
            if event_id in taken:
                continue
            taken.add(event_id)
            ids.append(event_id)
        events.append(event)
        size += len(event_line(event)) + 1
    return events, ids, False


def open_log(environ: Mapping[str, str], dev_name: str) -> TextIO:
    """The waiter's diagnostics file (`.../crewchat/waiter/<dev_name>.log`), appended to and
    started over past `LOG_MAX_BYTES`. What goes to stderr is read by the model at the wake, so
    an outage or a warning goes here instead: the agent should not hear of either. Where the
    file can't be opened the diagnostics are dropped."""
    try:
        if not SAFE_NAME_RE.fullmatch(dev_name):
            raise OSError(f"{dev_name!r} can't name a log file")
        path = state_home(environ) / WAITER_DIR / f"{dev_name}.log"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            return open(path, "w", encoding="utf-8")
        return open(path, "a", encoding="utf-8")
    except OSError:
        return open(os.devnull, "w", encoding="utf-8")


def run(
    repo_root: Path,
    sock_path: Path,
    environ: Mapping[str, str],
    err: TextIO,
    *,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """The waiter, apart from argv: `EXIT_WAKE` for an event, `EXIT_QUIET` for nothing to do.
    Only a wake writes to `err`; everything else goes to the diagnostics file."""
    clock = _clock if clock is None else clock
    sleep = _sleep if sleep is None else sleep
    try:
        dev_name = read_dev_name(repo_root)
    except DoorbellExit as exc:
        print(f"crewchat-waiter: {exc}", file=err)  # an exit 0: the model never reads it
        return EXIT_QUIET
    log = open_log(environ, dev_name)
    try:
        return _wait(dev_name, sock_path, environ, err, log, clock, sleep)
    finally:
        log.close()


def _wait(
    dev_name: str,
    sock_path: Path,
    environ: Mapping[str, str],
    err: TextIO,
    log: TextIO,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> int:
    try:
        lock = try_waiter_lock(environ, dev_name)
    except WaiterLockUnavailable as exc:
        # Without its lock a waiter would be started again by every Stop hook and pile up, so
        # it stays out; the doorbell sees no waiter and works as it always did.
        print(f"{time.strftime('%H:%M:%S')} no waiter lock, not running: {exc}", file=log)
        return EXIT_QUIET
    if lock is None:
        return EXIT_QUIET  # another waiter holds this Dev-Name: it will deliver
    try:
        seen = SeenSet.for_dev(environ, dev_name, log)
        tries = 0
        last_failure = ""
        while True:
            try:
                event, event_id = wait_for_new(
                    sock_path, dev_name, None, seen, clock=clock, sleep=sleep
                )
            except DoorbellExit as exc:
                # A daemon down past the grace, or an error from it: wait it out, quietly.
                if type(exc).__name__ != last_failure:  # once per kind: the text carries a clock
                    print(
                        f"{time.strftime('%H:%M:%S')} {exc}; waiting for the daemon",
                        file=log,
                        flush=True,
                    )
                    last_failure = type(exc).__name__
                sleep(RETRY_BACKOFF_S[min(tries, len(RETRY_BACKOFF_S) - 1)])
                tries += 1
                continue
            events, ids, capped = collect_batch(
                sock_path,
                dev_name,
                seen,
                event,
                event_id,
                batch_settings(environ, log),
                log,
                clock,
                sleep,
            )
            err.write(wake_text(dev_name, events, capped=capped))
            err.flush()
            if seen is not None:
                for each in ids:
                    seen.record(each)  # only after the whole text is out: a crash gives a repeat
            return EXIT_WAKE
    finally:
        os.close(lock)  # and with it the lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crewchat-waiter",
        description="Wait for crewchat events and wake the agent (exit 2). Run it from an "
        "asyncRewake hook, never by hand.",
    )
    parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
    parser.add_argument("--repo-root", type=Path, default=None)
    ns = parser.parse_args(argv)
    root = ns.repo_root or find_repo_root(Path.cwd())
    return run(root, ns.socket, os.environ, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
