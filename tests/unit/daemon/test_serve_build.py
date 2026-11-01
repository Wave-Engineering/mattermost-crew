"""#157: `crewchat serve` is `build` plus `run`, so the E2E world runs the same wiring."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crewchat.__main__ import DaemonParts, build, run
from crewchat.adapters.mattermost import MattermostBackend
from crewchat.config import load_config

CONFIG = """
default_crew = "oaw"

[[mattermost]]
id = "dev"
url = "http://mm.invalid"
kiosk = true
kiosk_research = true

[[mattermost]]
id = "ext"
url = "http://ext.invalid"

[crews.oaw]
needs_list = "bj:needs"

[crews.oaw.dev]
broker_socket = "/run/crewbroker/oaw-dev.sock"
broker_user_id = "b-oaw"
agents_allow = ["name:babel"]
"""


class Runner:
    async def answer(self, prompt: str, *, frame: Any = None) -> str:
        return "answer"


def build_parts(tmp_path: Path, sock: Path, **kwargs: Any) -> DaemonParts:
    config_path = tmp_path / "servers.toml"
    config_path.write_text(CONFIG)
    kwargs.setdefault("pool_dir", tmp_path / "pool")
    return build(load_config(config_path), sock, tmp_path / "tokens", tmp_path / "state", **kwargs)


def test_build_wires_the_doorbell_first(tmp_path: Path, short_dir: Path) -> None:
    parts = build_parts(tmp_path, short_dir / "crewchat.sock")
    assert [b.name for b in parts.backends] == ["mattermost:dev", "mattermost:ext"]
    assert all(isinstance(b, MattermostBackend) for b in parts.backends)
    assert parts.store.adapters[0] is parts.doorbell
    assert parts.store.adapters[1:] == parts.backends
    assert parts.discord is None and parts.squawkie is None
    assert parts.register_retry.registry is parts.registry  # #186's retry, built here


def test_build_takes_the_injected_clock_and_runner(tmp_path: Path, short_dir: Path) -> None:
    def clock() -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    runner = Runner()
    parts = build_parts(tmp_path, short_dir / "crewchat.sock", clock=clock, kiosk_runner=runner)
    assert parts.cache.clock is clock and parts.filters.clock is clock
    assert parts.kiosk.runner is runner


def test_build_writes_the_pool_under_pool_dir(tmp_path: Path, short_dir: Path) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    build_parts(tmp_path, short_dir / "crewchat.sock", pool_dir=pool)
    manifest = json.loads((pool / "dev.json").read_text())
    assert manifest["server"] == "dev"
    assert not (pool / "ext.json").exists()  # research is off on ext


def test_run_stops_with_a_hung_switch_refresh(tmp_path: Path, short_dir: Path) -> None:
    """A switch refresh blocked on a broker that never answers doesn't hold up the stop:
    `run` cancels it."""
    sock = short_dir / "crewchat.sock"
    parts = build_parts(tmp_path, sock)

    async def hung(stop: asyncio.Event) -> None:
        await asyncio.Event().wait()  # a `switch_state` call that never returns

    parts.cache.run = hung  # type: ignore[method-assign]
    closed: list[bool] = []
    retry_aclose = parts.register_retry.aclose

    async def aclose() -> None:
        closed.append(True)
        await retry_aclose()

    parts.register_retry.aclose = aclose  # type: ignore[method-assign]

    async def scenario() -> int:
        stop = asyncio.Event()
        daemon = asyncio.create_task(run(parts, sock, None, stop))
        async with asyncio.timeout(5):
            while not sock.exists():
                await asyncio.sleep(0.01)
        stop.set()
        # Not `wait_for`: its cancel on timeout is one `run` would swallow, and pass.
        done, _ = await asyncio.wait({daemon}, timeout=2)
        assert daemon in done, "run() did not return within 2 s of stop"
        daemon.cancel()
        return daemon.result()

    assert asyncio.run(scenario()) == 0
    assert not sock.exists()
    assert closed == [True]  # the register retries stop with the daemon
