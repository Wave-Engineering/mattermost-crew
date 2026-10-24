"""The broker service: one per (crew, Mattermost server), on a unix socket (Dev Spec §5.6).

    crewbroker serve <crew>-<server_id>
    crewbroker call <socket> <op> ['<args json>']

Frames are JSON lines with the §5.4 shape. Requests are
`{"proto":1,"id":"<uuid>","op":"<op>","args":{...}}`; responses are
`{"proto":1,"id":"<uuid>","ok":true|false,"result"|"error":...}`.

Every op is refused unless the peer's `SO_PEERCRED` UID is the daemon's, `crewchat` (R-08).
No broker ever calls another broker: the handler touches only its own state and the
Mattermost server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pwd
import signal
import socket
import socketserver
import struct
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crewbroker import config as cfg
from crewbroker.config import CrewConfig
from crewbroker.mattermost import MattermostAPI, MattermostClient, MattermostError, NotFound
from crewbroker.mint import (
    SquattedAccount,
    account_name,
    ensure_bot,
    mint_token,
    native_expiry_supported,
    record_register,
)
from crewbroker.prune import prune_idle
from crewbroker.state import AccountStore, TokenStore

PROTO = 1
DAEMON_USER = "crewchat"
SOCKET_MODE = 0o660
PRUNE_INTERVAL_S = 24 * 3600
MAX_FRAME_BYTES = 64 * 1024

log = logging.getLogger("crewbroker")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class OpError(Exception):
    """An op failed; `code` goes to the caller as the error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class Broker:
    """The ops, apart from the socket. One instance per (crew, server)."""

    def __init__(
        self,
        config: CrewConfig,
        client: MattermostAPI,
        state_dir: Path,
        *,
        server_key: str | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if not config.broker_user_id:
            raise ValueError("config.broker_user_id is unset; load the config with a client")
        self.config = config
        self.client = client
        self.accounts = AccountStore(state_dir)
        self.tokens = TokenStore(state_dir)
        self.server_key = server_key or config.server_url
        self.clock = clock
        self._lock = threading.Lock()

    def _name(self, args: dict[str, Any]) -> str:
        dev_name = args.get("dev_name")
        crew = args.get("crew")
        if not isinstance(dev_name, str) or not isinstance(crew, str):
            raise OpError("bad_request", "dev_name and crew are required strings")
        if crew != self.config.crew:
            raise OpError("bad_request", f"this broker serves crew {self.config.crew!r}")
        try:
            return account_name(dev_name, crew)
        except ValueError as exc:
            raise OpError("bad_request", str(exc)) from exc

    def _expiry(self) -> bool:
        return native_expiry_supported(self.client, self.server_key, self.config.broker_user_id)

    def mint(self, args: dict[str, Any]) -> dict[str, Any]:
        """Only the daemon's `register` causes a mint; it is the one op that records it."""
        name = self._name(args)
        with self._lock:
            try:
                bot_id = ensure_bot(self.client, name, self.accounts, self.config.broker_user_id)
            except SquattedAccount as exc:
                log.error("mint refused: %s", exc)
                raise OpError("squatted", str(exc)) from exc
            now = self.clock()
            record_register(self.accounts, name, now)
            token = mint_token(
                self.client, bot_id, self.tokens, expiry_supported=self._expiry(), now=now
            )
        log.info("mint: %s token %s", name, token.token_id)
        return {"account": name, "token": token.token, "token_id": token.token_id}

    def remint(self, args: dict[str, Any]) -> dict[str, Any]:
        """A fresh token for an existing account; the old one stays valid until `revoke`."""
        name = self._name(args)
        with self._lock:
            acct = self.accounts.get(name)
            if acct is None:
                raise OpError("not_found", f"{name} has never registered")
            try:
                bot = self.client.get_bot(acct.bot_user_id)
            except NotFound as exc:
                raise OpError("squatted", f"{name} is no longer owned by this broker") from exc
            if bot.get("owner_id") != self.config.broker_user_id:
                raise OpError("squatted", f"{name} is no longer owned by this broker")
            if bot.get("delete_at"):
                raise OpError("deactivated", f"{name} was pruned; it must register again")
            token = mint_token(
                self.client,
                acct.bot_user_id,
                self.tokens,
                expiry_supported=self._expiry(),
                now=self.clock(),
            )
        log.info("remint: %s token %s", name, token.token_id)
        return {"token": token.token, "token_id": token.token_id}

    def revoke(self, args: dict[str, Any]) -> dict[str, Any]:
        token_id = args.get("token_id")
        if not isinstance(token_id, str) or not token_id:
            raise OpError("bad_request", "token_id is a required string")
        with self._lock:
            if self.tokens.get(token_id) is None:
                raise OpError("not_found", "no token with that id was minted by this broker")
            self.client.revoke_token(token_id)
            self.tokens.remove(token_id)
        log.info("revoke: token %s", token_id)
        return {}

    def crew_info(self, args: dict[str, Any]) -> dict[str, Any]:
        """This broker's own fields only. `minted_bot_ids` is what it minted and still owns.

        A bot is kept only if it is on `accounts.json`'s `minted_bot_ids`, comes back from the
        broker's own `GET /bots` (active bots only), and has `owner_id == broker_user_id`. If
        `GET /bots` fails, so does `crew_info`: it never falls back to the unfiltered list.
        """
        with self._lock:
            minted = self.accounts.minted_bot_ids()
            try:
                bots = self.client.list_bots()
            except MattermostError as exc:
                raise OpError("upstream", f"GET /bots failed: {exc}") from exc
        owned = {
            str(b.get("user_id"))
            for b in bots
            if b.get("owner_id") == self.config.broker_user_id
        }
        return {
            "broker_user_id": self.config.broker_user_id,
            "human_user_ids": list(self.config.human_user_ids),
            "trusted": list(self.config.trusted),
            "minted_bot_ids": [bot_id for bot_id in minted if bot_id in owned],
        }

    def prune(self) -> list[str]:
        with self._lock:
            return prune_idle(self.client, self.accounts, self.clock())

    def ops(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "mint": self.mint,
            "remint": self.remint,
            "revoke": self.revoke,
            "crew_info": self.crew_info,
        }


def response(req_id: Any, ok: bool, payload: Any) -> dict[str, Any]:
    return {"proto": PROTO, "id": req_id, "ok": ok, ("result" if ok else "error"): payload}


def handle_frame(broker: Broker, line: bytes, peer_allowed: bool) -> dict[str, Any]:
    """One request frame in, one response frame out. Never raises."""
    try:
        frame = json.loads(line)
    except ValueError:
        return response(None, False, "bad_request: not JSON")
    if not isinstance(frame, dict):
        return response(None, False, "bad_request: a frame is a JSON object")
    req_id = frame.get("id")
    if not peer_allowed:
        return response(req_id, False, "forbidden")
    proto = frame.get("proto")
    if not isinstance(proto, int) or proto > PROTO:
        return response(req_id, False, "unsupported")
    op = broker.ops().get(str(frame.get("op")))
    if op is None:
        return response(req_id, False, "unsupported")
    args = frame.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return response(req_id, False, "bad_request: args is an object")
    try:
        return response(req_id, True, op(args))
    except OpError as exc:
        return response(req_id, False, str(exc))
    except MattermostError as exc:
        log.error("%s failed: %s", frame.get("op"), exc)
        return response(req_id, False, f"upstream: {exc}")
    except Exception:
        log.exception("%s failed", frame.get("op"))
        return response(req_id, False, "internal")


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


class _Handler(socketserver.StreamRequestHandler):
    server: BrokerServer

    def handle(self) -> None:
        allowed = peer_uid(self.request) == self.server.allowed_uid
        if not allowed:
            log.warning("refusing a peer that is not uid %d", self.server.allowed_uid)
        while True:
            line = self.rfile.readline(MAX_FRAME_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_FRAME_BYTES:
                self._send(response(None, False, "bad_request: frame too large"))
                return
            if not line.strip():
                continue
            self._send(handle_frame(self.server.broker, line, allowed))

    def _send(self, frame: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(frame).encode() + b"\n")
        self.wfile.flush()


class BrokerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, broker: Broker, allowed_uid: int) -> None:
        self.broker = broker
        self.allowed_uid = allowed_uid
        path.unlink(missing_ok=True)
        super().__init__(str(path), _Handler)
        # The group is crewchat's, from the setgid /run/crewbroker (tmpfiles.d).
        os.chmod(path, SOCKET_MODE)


def start_prune_scheduler(
    broker: Broker, stop: threading.Event, interval_s: float = PRUNE_INTERVAL_S
) -> threading.Thread:
    """Run `prune_idle` now and then daily, until `stop` is set."""

    def loop() -> None:
        while not stop.is_set():
            try:
                pruned = broker.prune()
                if pruned:
                    log.info("prune: deactivated %s", ", ".join(pruned))
            except Exception:
                log.exception("prune failed; retrying at the next interval")
            stop.wait(interval_s)

    thread = threading.Thread(target=loop, name="crewbroker-prune", daemon=True)
    thread.start()
    return thread


def serve(instance: str, *, config_dir: Path, state_root: Path, run_dir: Path) -> int:
    credential = cfg.credential_path(instance, config_dir).read_text(encoding="utf-8").strip()
    if not credential:
        raise SystemExit(f"{cfg.credential_path(instance, config_dir)} is empty")
    client = MattermostClient(cfg.read_server_url(instance, config_dir), credential)
    config = cfg.load_config(instance, config_dir=config_dir, client=client)
    allowed_uid = pwd.getpwnam(DAEMON_USER).pw_uid
    broker = Broker(config, client, cfg.state_dir(instance, state_root))
    server = BrokerServer(cfg.socket_path(instance, run_dir), broker, allowed_uid)
    stop = threading.Event()
    start_prune_scheduler(broker, stop)

    def shutdown(_signum: int, _frame: object) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info(
        "serving crew %s on %s (%s) as broker %s",
        config.crew,
        config.server_id,
        config.server_url,
        config.broker_user_id,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        cfg.socket_path(instance, run_dir).unlink(missing_ok=True)
        client.close()
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
    parser = argparse.ArgumentParser(prog="crewbroker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="serve one (crew, server) instance")
    p_serve.add_argument("instance", help="<crew>-<server_id>")
    p_serve.add_argument("--config-dir", type=Path, default=cfg.CONFIG_DIR)
    p_serve.add_argument("--state-root", type=Path, default=cfg.STATE_ROOT)
    p_serve.add_argument("--run-dir", type=Path, default=cfg.RUN_DIR)
    p_call = sub.add_parser("call", help="send one frame to a broker socket; print the reply")
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
        return serve(
            ns.instance, config_dir=ns.config_dir, state_root=ns.state_root, run_dir=ns.run_dir
        )
    reply = call(ns.socket, ns.op, json.loads(ns.args))
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
