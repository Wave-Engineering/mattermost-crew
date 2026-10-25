"""A hung test's coroutines (#105). pytest-timeout's dump shows threads, and a thread parked in
the event loop's `select` names no coroutine: every hang so far has been an `await`. So, some
seconds before the timeout, each running loop prints its tasks' stacks to stderr, which
pytest-timeout prints as the test's captured stderr when it fires. A loop that is blocked, not
waiting, can't run the dump; the thread dump covers that case."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest

DUMP_LEAD_S = 10  # how long before the timeout the tasks are dumped

_running: set[asyncio.AbstractEventLoop] = set()
_lock = threading.Lock()
_run_forever = asyncio.BaseEventLoop.run_forever


def _tracked_run_forever(self: asyncio.BaseEventLoop) -> None:
    # Every way of running a loop (`asyncio.run`, `run_until_complete`, a fake's thread) ends
    # up here.
    with _lock:
        _running.add(self)
    try:
        _run_forever(self)
    finally:
        with _lock:
            _running.discard(self)


asyncio.BaseEventLoop.run_forever = _tracked_run_forever  # type: ignore[method-assign]


def _await_chain(task: asyncio.Task[Any]) -> list[str]:
    """Where the task waits, outermost first. `Task.print_stack` shows only the outermost
    frame on 3.12, which would name `scenario()` and none of the awaits under it."""
    lines = []
    awaited: Any = task.get_coro()
    while awaited is not None:
        frame = getattr(awaited, "cr_frame", None) or getattr(awaited, "gi_frame", None)
        if frame is None:  # a Future, or a coroutine that has finished
            lines.append(f"  waiting on {awaited!r}")
            break
        code = frame.f_code
        lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
        awaited = getattr(awaited, "cr_await", None) or getattr(awaited, "gi_yieldfrom", None)
    return lines


def _dump_tasks(loop: asyncio.AbstractEventLoop, nodeid: str) -> None:
    tasks = asyncio.all_tasks(loop)
    print(f"\n==== {nodeid}: {len(tasks)} task(s) on a loop, near the timeout", file=sys.stderr)
    for task in tasks:
        print(f"---- {task.get_name()}", file=sys.stderr)
        print("\n".join(_await_chain(task)), file=sys.stderr)
    sys.stderr.flush()


def _dump_all(nodeid: str) -> None:
    with _lock:
        loops = list(_running)
    if not loops:
        print(f"\n==== {nodeid}: no event loop running, near the timeout", file=sys.stderr)
    for loop in loops:
        with contextlib.suppress(RuntimeError):  # it closed in the meantime
            loop.call_soon_threadsafe(_dump_tasks, loop, nodeid)


def _timeout_s(item: pytest.Item) -> float:
    marker = item.get_closest_marker("timeout")
    if marker is not None and marker.args:
        return float(marker.args[0])
    value: Any = item.config.getoption("timeout", None) or item.config.getini("timeout")
    return float(value or 0)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Iterator[None]:
    timeout = _timeout_s(item)
    timer = None
    if timeout > DUMP_LEAD_S:
        timer = threading.Timer(timeout - DUMP_LEAD_S, _dump_all, args=(item.nodeid,))
        timer.daemon = True
        timer.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.cancel()
