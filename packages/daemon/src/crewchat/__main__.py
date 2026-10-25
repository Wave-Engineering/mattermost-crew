"""The crewchat daemon's entry point.

    crewchat serve [--config /etc/crewchat/servers.toml] [--socket /run/crewchat/crewchat.sock]
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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crewchat.adapters.mattermost import MattermostBackend
from crewchat.config import CONFIG_PATH, load_config
from crewchat.protocol import PROTO
from crewchat.registry import Registry
from crewchat.server import CLIENTS_GROUP, SOCKET_PATH, DaemonServer, build_handlers
from crewchat.tokens import (
    TOKENS_DIR,
    Rotator,
    SwitchCache,
    TokenStore,
    broker_clients,
    retry_delays,
)

ROTATION_INTERVAL_S = 24 * 3600

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


async def serve(config_path: Path, socket_path: Path, tokens_dir: Path, group: str | None) -> int:
    config = load_config(config_path)
    brokers = broker_clients(config)
    store = TokenStore(tokens_dir, brokers)
    store.load()
    # One adapter per configured Mattermost server (R-10). They hold websockets only for
    # agents whose stream is open; rotation reconnects those (§4.8).
    backends = [
        MattermostBackend(s.id, s.url, store.token_for, refresher=store) for s in config.mattermost
    ]
    store.adapters.extend(backends)
    cache = SwitchCache(brokers)
    registry = Registry(config, brokers, store, cache)
    server = DaemonServer(build_handlers(config, registry))
    listener = await server.start(socket_path, group)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    rotation = asyncio.create_task(rotation_loop(Rotator(store), stop))
    log.info(
        "serving %s: crews %s, servers %s",
        socket_path,
        ", ".join(sorted(config.crews)),
        ", ".join(s.id for s in config.mattermost),
    )
    try:
        await stop.wait()
    finally:
        listener.close()
        await listener.wait_closed()
        await rotation
        for backend in backends:
            await backend.aclose()
        socket_path.unlink(missing_ok=True)
    return 0


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crewchat")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="run the daemon")
    p_serve.add_argument("--config", type=Path, default=CONFIG_PATH)
    p_serve.add_argument("--socket", type=Path, default=SOCKET_PATH)
    p_serve.add_argument("--tokens-dir", type=Path, default=TOKENS_DIR)
    p_serve.add_argument(
        "--group", default=CLIENTS_GROUP, help="the socket's group ('' leaves it unchanged)"
    )
    p_call = sub.add_parser("call", help="send one frame to the daemon socket; print the reply")
    p_call.add_argument("socket")
    p_call.add_argument("op")
    p_call.add_argument("args", nargs="?", default="{}")
    ns = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if ns.cmd == "serve":
        return asyncio.run(serve(ns.config, ns.socket, ns.tokens_dir, ns.group or None))
    reply = call(ns.socket, ns.op, json.loads(ns.args))
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
