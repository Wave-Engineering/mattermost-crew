"""Retry a partial `register` in the background (#186).

A `register` that minted on some servers and not others succeeds, so the shim won't register
again for a day, and the agent stays off the failed servers until then. A daemon restart
can't fix that, because every shim re-registers within one ping interval of a restart, and
that burst is what overruns the brokers' Mattermost rate budget in the first place.

`RegisterRetry` keeps at most one task per Dev-Name. It sleeps `DELAYS_S[attempt]`, capped at
the last entry and scaled by a jitter factor from `JITTER`, so a restarted fleet's retries
spread out instead of landing together. Then it calls `Registry.retry_unavailable`, which
mints on only the failed servers. Each backend that comes back goes to `on_recovered`, the
same step a fresh register takes (the doorbell attaches it, and the kiosk reviews it). The
task ends when nothing is left to retry, after `ATTEMPTS` tries, or when a newer `register`
replaces the registration.

A newer `register` calls `cancel` first. A task cancelled while it sleeps simply ends. A mint
already in flight runs to completion under `asyncio.shield`, because cancelling one midway
could leave a token minted but not installed. The store's per-agent lock queues the newer
register behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable

from crewchat.registry import Registration, Registry

DELAYS_S = (10.0, 20.0, 40.0, 80.0, 160.0, 300.0)
ATTEMPTS = 8  # about 20 minutes; after that, the shim's next register tries again
JITTER = (0.5, 1.5)

log = logging.getLogger("crewchat.retry")

Sleep = Callable[[float], Awaitable[object]]
Uniform = Callable[[float, float], float]


class RegisterRetry:
    def __init__(
        self,
        registry: Registry,
        *,
        delays: tuple[float, ...] = DELAYS_S,
        attempts: int = ATTEMPTS,
        sleep: Sleep = asyncio.sleep,
        uniform: Uniform = random.uniform,
    ) -> None:
        self.registry = registry
        self.delays = delays
        self.attempts = attempts
        self.sleep = sleep
        self.uniform = uniform
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def delay(self, attempt: int) -> float:
        """The wait before retry `attempt` (from 0): its capped base, jittered."""
        return self.delays[min(attempt, len(self.delays) - 1)] * self.uniform(*JITTER)

    def pending(self, dev_name: str) -> bool:
        task = self._tasks.get(dev_name)
        return task is not None and not task.done()

    def cancel(self, dev_name: str) -> None:
        """Stop `dev_name`'s retry: a newer `register` decides from its own outcome."""
        task = self._tasks.pop(dev_name, None)
        if task is not None:
            task.cancel()

    def schedule(self, reg: Registration, on_recovered: Callable[[Registration], object]) -> None:
        """Retry `reg`'s failed servers in the background, if there are any."""
        self.cancel(reg.dev_name)
        if not reg.retryable:
            return
        self._tasks[reg.dev_name] = asyncio.get_running_loop().create_task(
            self._run(reg, on_recovered), name=f"register retry {reg.dev_name}"
        )

    async def _run(self, reg: Registration, on_recovered: Callable[[Registration], object]) -> None:
        try:
            for attempt in range(self.attempts):
                await self.sleep(self.delay(attempt))
                if self.registry.get(reg.dev_name) is not reg:
                    return
                try:
                    gained = await asyncio.shield(self.registry.retry_unavailable(reg))
                except Exception:
                    log.exception("register retry for %s failed", reg.account)
                    continue
                if gained:
                    try:
                        on_recovered(reg)
                    except Exception:
                        log.exception("after registering %s on a retry", reg.account)
                if not reg.retryable:
                    return
            log.warning(
                "register %s: still unavailable on %s after %d retries; the next register "
                "tries again",
                reg.account,
                ", ".join(reg.retryable),
                self.attempts,
            )
        finally:
            if self._tasks.get(reg.dev_name) is asyncio.current_task():
                del self._tasks[reg.dev_name]

    async def aclose(self) -> None:
        """Cancel every pending retry (the daemon is stopping)."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
