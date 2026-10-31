"""The crewchat daemon's entry point.

    crewchat serve [--config /etc/crewchat/servers.toml] [--socket /run/crewchat/crewchat.sock]
                   [--state-dir /var/lib/crewchat]
    crewchat call <socket> <op> ['<args json>']

`serve` loads `servers.toml`, reads the token store, and serves the socket; the daily rotation
runs at startup and then every 24 hours. `call` sends one frame and prints the reply: the
runbook's and `verify-install`'s way to reach the socket as the agents' user.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import socket
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crewchat.adapters.base import ChatBackend
from crewchat.adapters.discord import DiscordBackend, read_credential
from crewchat.adapters.mattermost import MattermostBackend
from crewchat.adapters.squawkie import SQUAWKIE, SquawkieBackend
from crewchat.chat import AgentContext, build_chat_handlers
from crewchat.config import CONFIG_PATH, ConfigError, load_config
from crewchat.corpus_pool import PoolManifest, expert_repo
from crewchat.doorbell import (
    DETACH_TIMEOUT_S,
    CursorStore,
    Doorbell,
    Source,
    build_doorbell_handlers,
)
from crewchat.filters import Filters, KioskQueue, SwitchCache
from crewchat.kiosk import ClaudeCliRunner, Kiosk, Research
from crewchat.kiosk_broker import KioskBrokerClient
from crewchat.protocol import PROTO
from crewchat.registry import Registry
from crewchat.server import (
    CLIENTS_GROUP,
    SOCKET_PATH,
    DaemonServer,
    HealthSource,
    build_handlers,
)
from crewchat.threads import STATE_DIR, MyThreads
from crewchat.tokens import (
    TOKENS_DIR,
    Rotator,
    TokenStore,
    broker_clients,
    retry_delays,
)
from crewchat.uploads import UploadStore

ROTATION_INTERVAL_S = 24 * 3600
UPLOADS_SUBDIR = "uploads"  # /run/crewchat/uploads, beside the socket
# One INFO line per request, every poll, from every agent's stream, rotates the journal within
# hours (#157). They log nothing above INFO, so WARNING silences them; our adapters log their
# own failures and retries.
QUIET_LOGGERS = ("httpx", "httpcore")

log = logging.getLogger("crewchat")


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Wait `seconds`, or until `stop` is set. True when stopping."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), max(seconds, 0.0))
    return stop.is_set()


async def rotation_loop(
    rotator: Rotator,
    stop: asyncio.Event,
    *,
    interval_s: float = ROTATION_INTERVAL_S,
    wait: Callable[[asyncio.Event, float], Awaitable[bool]] = wait_or_stop,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """The daily rotation. A rotation that fails is retried on a backoff (5 min, doubling,
    capped at 1 h) until it succeeds or the next daily run, well inside the 26 h native
    expiry (R-05, R-07)."""
    while not stop.is_set():
        started = monotonic()
        failed: list[tuple[str, str, str]] = []
        try:
            report = await rotator.run_daily(now())
            failed = report.failed
            log.info(
                "rotation: %d rotated, %d dropped, %d failed",
                len(report.rotated),
                len(report.dropped),
                len(failed),
            )
        except Exception:
            log.exception("rotation failed; retrying at the next interval")
        delays = retry_delays()
        while failed:
            delay = next(delays)
            if monotonic() - started + delay >= interval_s:
                break  # the next daily run picks them up
            if await wait(stop, delay):
                return
            try:
                failed = await rotator.retry(failed)
            except Exception:
                log.exception("rotation retry failed")
        if await wait(stop, interval_s - (monotonic() - started)):
            return


async def serve(
    config_path: Path,
    socket_path: Path,
    tokens_dir: Path,
    group: str | None,
    state_dir: Path = STATE_DIR,
) -> int:
    config = load_config(config_path)
    brokers = broker_clients(config)
    store = TokenStore(tokens_dir, brokers)
    store.load()
    # Revokes owed by agents dropped before the last stop (#151). A broker still down keeps
    # them for the next daily rotation; it never stops the daemon starting.
    try:
        if owed := await store.retry_retired():
            log.warning("%d retired tokens still owed a revoke", owed)
    except Exception:
        log.exception("retrying retired revokes failed")
    # One adapter per configured Mattermost server (R-10). They hold websockets only for
    # agents whose stream is open; rotation reconnects those (§4.8).
    backends = [
        MattermostBackend(s.id, s.url, store.token_for, refresher=store) for s in config.mattermost
    ]
    store.adapters.extend(backends)
    chat_backends: dict[str, ChatBackend] = {b.name: b for b in backends}
    discord: DiscordBackend | None = None
    if config.discord_enabled:
        if config.discord_screamhole_url is None:
            raise ConfigError(f"{config_path}: discord is enabled but has no screamhole_url")
        discord = DiscordBackend(
            config.discord_screamhole_url,
            config.discord_credential,
            guild_id=config.discord_guild_id,
        )
        chat_backends[discord.name] = discord
    cache = SwitchCache.from_config(brokers, config)
    # The kiosk's corpus pool manifests (#139): written now, and after each register.
    pool = PoolManifest(config, store.agents)
    pool.refresh()
    registry = Registry(config, brokers, store, cache, identity_sink=discord, pool=pool)
    # The one persisted "my threads" set (D-104): every send and dm records into it, and
    # the filters' addressing and loop guard read the same instance, so replies survive a
    # restart.
    threads = MyThreads.load(state_dir, config.my_threads_retention_days)
    # Membership, addressing, the loop guard and the switch, on push and pull (Story 3.3).
    filters = Filters.from_config(registry, cache, threads, config, state_dir=state_dir)
    kiosk = KioskQueue()  # push KIOSK verdicts, for Story 3.5's kiosk
    # Attachments the shims stream in (`upload`), beside the socket under /run/crewchat.
    uploads = UploadStore(socket_path.parent / UPLOADS_SUBDIR)
    uploads.clear_stale()  # the directory outlives a restart; its old uploads are untracked
    health_sources: dict[str, HealthSource] = {discord.name: discord} if discord else {}
    # What the shims push (the kiosk brief, the squawk ids): squawkie filters by the ids.
    context = AgentContext()
    squawkie: SquawkieBackend | None = None
    if config.squawkie_url is not None:
        token = (
            read_credential(config.squawkie_credential)
            if config.squawkie_credential is not None
            else None
        )
        squawkie = SquawkieBackend(
            config.squawkie_url,
            token,
            ids_for=lambda dev: context.squawk_ids.get(dev, ()),
            texts_for=lambda dev: context.squawk_texts.get(dev, {}),
        )
    # The doorbell (Story 3.2): one task per (agent, backend), one queue per agent. It goes
    # first among the store's adapters, so an agent leaving rotation has its doorbell tasks
    # stopped before its streams close.
    sources: dict[str, Source] = dict(chat_backends)
    if squawkie is not None:
        sources[SQUAWKIE] = squawkie
    doorbell = Doorbell(sources, CursorStore(state_dir), admit=filters.admit_event, kiosk=kiosk)
    store.adapters.insert(0, doorbell)
    # The kiosk (Story 3.5): answers the KIOSK queue from the briefs the shims push, through
    # the `crewkiosk` CLI runner (D-096). Sessions start on each expert's first outsider.
    # A research server's answers go framed through the research socket, and the broker's
    # audit of each comes back into its transcript (#142).
    runner = ClaudeCliRunner()
    kiosk_broker = KioskBrokerClient()
    research_servers = {s.id for s in config.mattermost if s.kiosk_research}

    def research_for(dev_name: str, server_id: str) -> Research | None:
        if server_id not in research_servers:
            return None
        return Research(
            runner,
            kiosk_broker,
            lambda: expert_repo(config, store.agents(), server_id, dev_name),
        )

    kiosk_answers = Kiosk(
        kiosk,
        context.briefs,
        chat_backends,
        runner,
        state_dir=state_dir,
        deliver=doorbell.deliver,
        research_for=research_for,
    )
    # Social engaged on a server (Story 3.6, R-33): each of that crew's agents there gets its
    # kiosk review on its doorbell queue.
    cache.social_listeners.append(
        lambda crew, server_id: kiosk_answers.on_social(
            server_id, registry.agents_on(crew, server_id)
        )
    )
    handlers = build_handlers(
        config,
        registry,
        health_sources,
        kiosk_metrics=kiosk_broker.metrics if research_servers else None,
    )
    handlers.update(
        build_chat_handlers(registry, chat_backends, threads, context, uploads, filters=filters)
    )
    handlers.update(
        build_doorbell_handlers(
            registry,
            doorbell,
            handlers["register"],
            extra_backends=[SQUAWKIE] if squawkie is not None else [],
            # Social already on when an agent registers (after a daemon start, or mid-social):
            # its kiosk review, which `social_listeners` alone would never send (R-33, #99).
            on_registered=lambda reg: kiosk_answers.on_registered(reg, cache),
        )
    )
    server = DaemonServer(handlers, hangup_cancels=frozenset({"subscribe"}))
    await server.start(socket_path, group)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    rotation = asyncio.create_task(rotation_loop(Rotator(store), stop))
    kiosk_answers.start()
    # `switch_state` and `crew_info` from every local broker, now and every 30 s (§5.9).
    switch_refresh = asyncio.create_task(cache.run(stop))
    log.info(
        "serving %s: crews %s, servers %s",
        socket_path,
        ", ".join(sorted(config.crews)),
        ", ".join(s.id for s in config.mattermost),
    )
    try:
        await stop.wait()
    finally:
        await server.aclose()
        await rotation
        await switch_refresh
        await doorbell.aclose()
        await kiosk_answers.aclose()
        if squawkie is not None:
            await squawkie.aclose()
        for backend in backends:
            await backend.aclose()
        if discord is not None:
            await discord.aclose()
        socket_path.unlink(missing_ok=True)
    return 0


def run_bounded[R](main: Coroutine[Any, Any, R], timeout_s: float = DETACH_TIMEOUT_S) -> R:
    """`asyncio.run`, but with an exit that can't hang (#152). `asyncio.run` cancels each task
    still pending once and then waits for all of them with no bound, so a task that absorbed
    its cancels (one `Doorbell.detach` abandoned) would hold the process forever. Here the
    leftovers get `timeout_s`; one still running after it is logged and left behind."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(main)
    finally:
        try:
            leftover = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in leftover:
                task.cancel()
            if leftover:
                _, stuck = loop.run_until_complete(asyncio.wait(leftover, timeout=timeout_s))
                for task in stuck:
                    log.error("task %s did not stop on exit; left behind", task.get_name())
            for shutdown in (loop.shutdown_asyncgens, loop.shutdown_default_executor):
                with contextlib.suppress(TimeoutError):
                    loop.run_until_complete(asyncio.wait_for(shutdown(), timeout_s))
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def call(sock_path: str, op: str, args: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """Send one request frame and return the response frame."""
    frame = {"proto": PROTO, "id": str(uuid.uuid4()), "op": op, "args": args}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(sock_path)
        sock.sendall(json.dumps(frame).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    result: dict[str, Any] = json.loads(buf)
    return result


def configure_logging() -> None:
    """Our own events at INFO, to stderr (the journal); `QUIET_LOGGERS` at WARNING only."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crewchat")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="run the daemon")
    p_serve.add_argument("--config", type=Path, default=CONFIG_PATH)
    p_serve.add_argument("--socket", type=Path, default=SOCKET_PATH)
    p_serve.add_argument("--tokens-dir", type=Path, default=TOKENS_DIR)
    p_serve.add_argument("--state-dir", type=Path, default=STATE_DIR)
    p_serve.add_argument(
        "--group", default=CLIENTS_GROUP, help="the socket's group ('' leaves it unchanged)"
    )
    p_call = sub.add_parser("call", help="send one frame to the daemon socket; print the reply")
    p_call.add_argument("socket")
    p_call.add_argument("op")
    p_call.add_argument("args", nargs="?", default="{}")
    ns = parser.parse_args(argv)

    configure_logging()
    if ns.cmd == "serve":
        return run_bounded(
            serve(ns.config, ns.socket, ns.tokens_dir, ns.group or None, ns.state_dir)
        )
    reply = call(ns.socket, ns.op, json.loads(ns.args))
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
