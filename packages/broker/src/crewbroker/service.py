"""The broker service: one per (crew, Mattermost server), on a unix socket (Dev Spec §5.6).

    crewbroker serve <crew>-<server_id>
    crewbroker call <socket> <op> ['<args json>']

Frames are JSON lines with the §5.4 shape. Requests are
`{"proto":1,"id":"<uuid>","op":"<op>","args":{...}}`; responses are
`{"proto":1,"id":"<uuid>","ok":true|false,"result"|"error":...}`.

Every op is refused unless the peer's `SO_PEERCRED` UID is the daemon's, `crewchat` (R-08).
No broker ever calls another broker: the handler touches only its own state and the
Mattermost server.

The broker also holds its account's websocket (`DMListener`): a DM from the crew's human
partner is a switch command (`crewbroker.switch`), answered in the same DM (§4.6).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pwd
import re
import signal
import socket
import socketserver
import struct
import sys
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import EllipsisType
from typing import Any, Protocol, cast
from urllib.parse import urlencode

import websockets.sync.client
from websockets.exceptions import ConnectionClosed, WebSocketException

from crewbroker import config as cfg
from crewbroker.avatar import identicon_png
from crewbroker.config import CrewConfig
from crewbroker.mattermost import MattermostAPI, MattermostClient, MattermostError, NotFound
from crewbroker.membership import DEADLINE_RESERVE_S, Membership
from crewbroker.mint import (
    SquattedAccount,
    account_name,
    ensure_bot,
    mint_token,
    native_expiry_supported,
    record_register,
    revoke_superseded,
)
from crewbroker.prune import prune_idle
from crewbroker.state import AccountStore, DMCursorStore, TokenStore
from crewbroker.switch import (
    Mode,
    SwitchState,
    SwitchStore,
    apply_command,
    effective_mode,
    settle,
)

PROTO = 1
DAEMON_USER = "crewchat"
SOCKET_MODE = 0o660
PRUNE_INTERVAL_S = 24 * 3600
MAX_FRAME_BYTES = 64 * 1024
# Every post the broker makes carries this prop, and the listener never answers a post that
# does: two brokers (or a broker and itself) can't refuse each other in a loop.
BROKER_PROP = "crew_broker"
WS_PING_S = 30.0
WS_RECV_POLL_S = 1.0
WS_BACKOFF_FIRST_S = 1.0
WS_BACKOFF_MAX_S = 60.0
# The DM backfill (#159): a command older than this, sent while the broker was offline, is
# refused rather than applied, unless it only narrows who gets through.
BACKFILL_MAX_AGE_S = 600
NARROWING_COMMANDS = {("crew",), ("kiosk", "off")}
HANDLED_DMS_MAX = 500

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
        self.switch = SwitchStore(state_dir)
        self.dm_cursor = DMCursorStore(state_dir)
        self.server_key = server_key or config.server_url
        self.clock = clock
        self._lock = threading.Lock()
        # Membership on mint (#132): the configured names resolve now, at startup. One that
        # doesn't is logged and reported on each mint, and never stops the broker.
        self.membership: Membership | None = None
        if config.team:
            self.membership = Membership(
                client,
                config.team,
                config.default_channels,
                tag=config.channel_tag or f"crew-{config.crew}",
                broker_user_id=config.broker_user_id,
            )
            self.membership.resolve()

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

    def _held(self, args: dict[str, Any]) -> str | EllipsisType | None:
        """`current_token_id`: the token the daemon holds for this account, or null for none.

        Absent (an older daemon) returns `...`, and nothing is revoked.
        """
        if "current_token_id" not in args:
            return ...
        held = args["current_token_id"]
        if held is not None and (not isinstance(held, str) or not held):
            raise OpError("bad_request", "current_token_id is a token id or null")
        return held

    def _deadline(self, args: dict[str, Any]) -> float | None:
        deadline = args.get("deadline")
        if deadline is not None and (
            isinstance(deadline, bool) or not isinstance(deadline, int | float)
        ):
            raise OpError("bad_request", "deadline is epoch seconds")
        return float(deadline) if deadline is not None else None

    def _check_deadline(self, deadline: float | None) -> None:
        """Called once the lock is held: the caller has given up on a request past its deadline.

        Minting for it anyway would leave an orphan, and its supersede could revoke the token
        a newer call has since swapped in (#41).
        """
        if deadline is not None and self.clock().timestamp() > deadline:
            raise OpError("expired", "the caller's deadline passed before the broker got to it")

    def _supersede(self, bot_id: str, held: str | EllipsisType | None, new_id: str) -> None:
        """Revoke the account's tokens other than `held` and `new_id` (#41).

        A best-effort cleanup after the mint: its errors are logged and never fail the op.
        """
        if held is ...:
            return
        keep = {new_id} | ({held} if held else set())
        try:
            gone = revoke_superseded(self.client, bot_id, self.tokens, keep)
        except Exception:
            log.exception("revoking superseded tokens of %s failed", bot_id)
            return
        if gone:
            log.info("revoked superseded tokens of %s: %s", bot_id, ", ".join(gone))

    def mint(self, args: dict[str, Any]) -> dict[str, Any]:
        """Only the daemon's `register` causes a mint; it is the one op that records it."""
        name = self._name(args)
        held = self._held(args)
        deadline = self._deadline(args)
        with self._lock:
            self._check_deadline(deadline)
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
            self._supersede(bot_id, held, token.token_id)
        log.info("mint: %s token %s", name, token.token_id)
        answer: dict[str, Any] = {"account": name, "token": token.token, "token_id": token.token_id}
        if self.membership is not None:
            # Ensure the bot's team and default channels (#132, D-109); never raises. Only a
            # bot this broker owns gets here: `ensure_bot` created it, or read its `owner_id`
            # as this broker, earlier in this mint.
            answer["membership"] = self.membership.ensure(
                name, bot_id, deadline, lambda: self.clock().timestamp()
            )
        self._ensure_avatar(name, bot_id, deadline)
        return answer

    def _ensure_avatar(self, name: str, bot_id: str, deadline: float | None) -> None:
        """Set the bot's avatar once, an identicon of its name (#151; D-098: only its owner,
        this broker, can). Skipped while the deadline is near; a failure is logged and never
        fails the mint. Either way the flag stays unset, so the next `mint` tries again."""
        if self.accounts.avatar_set(name):
            return
        if deadline is not None and deadline - self.clock().timestamp() < DEADLINE_RESERVE_S:
            return
        try:
            self.client.set_user_image(bot_id, identicon_png(name))
        except MattermostError as exc:
            log.warning("avatar for %s not set; the next mint retries: %s", name, exc)
            return
        with self._lock:  # `accounts.json` is written whole: never beside another write
            self.accounts.mark_avatar_set(name)

    def remint(self, args: dict[str, Any]) -> dict[str, Any]:
        """A fresh token for an existing account; the held one stays valid until `revoke`."""
        name = self._name(args)
        held = self._held(args)
        deadline = self._deadline(args)
        with self._lock:
            self._check_deadline(deadline)
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
            self._supersede(acct.bot_user_id, held, token.token_id)
        log.info("remint: %s token %s", name, token.token_id)
        return {"token": token.token, "token_id": token.token_id}

    def revoke(self, args: dict[str, Any]) -> dict[str, Any]:
        token_id = args.get("token_id")
        if not isinstance(token_id, str) or not token_id:
            raise OpError("bad_request", "token_id is a required string")
        with self._lock:
            if self.tokens.get(token_id) is None:
                raise OpError("not_found", "no token with that id was minted by this broker")
            try:
                self.client.revoke_token(token_id)
            except NotFound:
                # Already gone on the server: done, or the daemon would retry it every day.
                log.warning("revoke: token %s is already gone upstream", token_id)
            self.tokens.remove(token_id)
        log.info("revoke: token %s", token_id)
        return {}

    def deactivate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Disable an account's bot now, as the prune does after 30 idle days (#127).

        `verify-install` retires its canary this way, so a check against a live server leaves no
        active bot behind. The answer's `delete_at` is read back from the server after the
        disable, never assumed. The next `mint` for the name re-enables the bot, as for any
        pruned account.
        """
        name = self._name(args)
        squatted = OpError("squatted", f"{name} is no longer owned by this broker")
        with self._lock:
            acct = self.accounts.get(name)
            if acct is None:
                raise OpError("not_found", f"{name} has never registered")
            try:
                if self.client.get_bot(acct.bot_user_id).get("owner_id") != (
                    self.config.broker_user_id
                ):
                    raise squatted
                self.client.disable_bot(acct.bot_user_id)
                # Disabled: record it before the read-back, which may fail on its own.
                self.accounts.mark_pruned(name, self.clock())
                delete_at = self.client.get_bot(acct.bot_user_id).get("delete_at")
            except NotFound as exc:
                raise squatted from exc
            except MattermostError as exc:
                raise OpError("upstream", f"deactivating {name} failed: {exc}") from exc
        log.info("deactivate: %s (%s), delete_at %s", name, acct.bot_user_id, delete_at)
        return {"account": name, "delete_at": delete_at}

    def crew_info(self, args: dict[str, Any]) -> dict[str, Any]:
        """This broker's own fields only. `minted_bot_ids` is what it minted and still owns.

        A bot is kept only if it is on `accounts.json`'s `minted_bot_ids`, comes back from the
        broker's own `GET /bots` (active bots only), and has `owner_id == broker_user_id`. If
        `GET /bots` fails, so does `crew_info`: it never falls back to the unfiltered list.
        """
        with self._lock:
            try:
                owned = self._owned_minted()
            except MattermostError as exc:
                raise OpError("upstream", f"GET /bots failed: {exc}") from exc
        return {
            "broker_user_id": self.config.broker_user_id,
            "human_user_ids": list(self.config.human_user_ids),
            "trusted": list(self.config.trusted),
            "minted_bot_ids": owned,
        }

    def _owned_minted(self) -> list[str]:
        """The bots on `minted_bot_ids` that `GET /bots` lists as this broker's. Call with the
        lock held; a failed `GET /bots` raises."""
        minted = self.accounts.minted_bot_ids()
        owned = {
            str(b.get("user_id"))
            for b in self.client.list_bots()
            if b.get("owner_id") == self.config.broker_user_id
        }
        return [bot_id for bot_id in minted if bot_id in owned]

    def reconcile_membership(self) -> dict[str, list[Any]] | None:
        """One reconcile pass over the bots this broker owns (#167); None with no `team`.

        Every channel a mint can see was published before the bots are read: the scan
        publishes only tagged channels the broker is in, and a public one it joins is
        published before the bots are read again. So a bot minted during the pass is in the
        list, or was minted after it and its own mint adds it to every channel published. The
        broker joins nothing when it owns no bots, or when reading them fails (the pass then
        raises).
        """
        if self.membership is None:
            return None
        self.membership.refresh()
        with self._lock:
            bot_ids = self._owned_minted()
        if bot_ids and self.membership.join_tagged():
            with self._lock:
                bot_ids = self._owned_minted()
        with self._lock:
            names = {a.bot_user_id: a.name for a in self.accounts.all()}
        return self.membership.reconcile(bot_ids, names, refresh=False)

    def prune(self) -> list[str]:
        with self._lock:
            return prune_idle(self.client, self.accounts, self.clock())

    # -- the social switch (Story 3.1) ------------------------------------------------------
    def _settle(self, now: datetime) -> tuple[SwitchState, Mode | None]:
        """Load the switch and persist any lapse. Call with the lock held.

        Returns the settled state and, when a lapse was just recorded, the mode it reverted to.
        """
        stored = self.switch.load()
        mode, lapsed = effective_mode(stored, now)
        settled = settle(stored, now)
        if settled != stored:
            self.switch.save(settled)
        return settled, (mode if lapsed else None)

    def _notify_lapse(self, mode: Mode) -> None:
        """DM each human partner that social lapsed (R-23). Best effort: errors are logged."""
        for human in self.config.human_user_ids:
            try:
                channel = self.client.direct_channel(self.config.broker_user_id, human)
                self.client.create_post(channel, f"social lapsed → {mode}", props={BROKER_PROP: 1})
            except MattermostError as exc:
                log.error("lapse notice to %s failed: %s", human, exc)

    def switch_state(self, args: dict[str, Any]) -> dict[str, Any]:
        """The stored switch, settled first, plus the mode in force (§5.6)."""
        now = self.clock()
        with self._lock:
            state, lapsed_to = self._settle(now)
        if lapsed_to is not None:
            self._notify_lapse(lapsed_to)
        mode, _ = effective_mode(state, now)
        return {
            "mode": state.mode,
            "expires_at": state.expires_at.isoformat() if state.expires_at else None,
            "revert_to": state.revert_to,
            "kiosk_on": state.kiosk_on,
            "effective_mode": mode,
        }

    def handle_dm(self, sender_user_id: str, text: str) -> str:
        """A DM to the broker account: apply it as a switch command and return the reply.

        Only the crew's human partners change anything (R-24); anyone else is refused and
        the stored state is not touched.
        """
        now = self.clock()
        lapsed_to: Mode | None = None
        with self._lock:
            if sender_user_id in self.config.human_user_ids:
                state, lapsed_to = self._settle(now)
            else:
                state = self.switch.load()
            new, reply = apply_command(state, sender_user_id, text, self.config, now)
            if new != state:
                self.switch.save(new)
        if lapsed_to is not None:
            self._notify_lapse(lapsed_to)
        if new != state:
            log.info("switch: %s by %s", reply, sender_user_id)
        elif sender_user_id not in self.config.human_user_ids:
            log.warning("switch command refused from %s", sender_user_id)
        return reply

    def ops(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "mint": self.mint,
            "remint": self.remint,
            "revoke": self.revoke,
            "deactivate": self.deactivate,
            "crew_info": self.crew_info,
            "switch_state": self.switch_state,
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


def start_membership_reconciler(
    broker: Broker,
    stop: threading.Event,
    interval_s: float,
    wait: Callable[[float], bool] | None = None,
) -> threading.Thread | None:
    """Run a reconcile pass now and then every `interval_s`, until `stop` is set (#167).

    None, and no pass at all, with an interval of 0 or no `team`. A pass that raises is
    logged and retried at the next interval. `wait` defaults to `stop.wait`.
    """
    if interval_s <= 0 or broker.membership is None:
        return None
    pause = wait or stop.wait

    def loop() -> None:
        while not stop.is_set():
            try:
                broker.reconcile_membership()
            except Exception:
                log.exception("membership reconcile failed; retrying at the next interval")
            if pause(interval_s):
                return

    thread = threading.Thread(target=loop, name="crewbroker-membership", daemon=True)
    thread.start()
    return thread


@dataclass(frozen=True)
class DirectMessage:
    channel_id: str
    sender_user_id: str
    message: str
    post_id: str = ""
    create_at: int = 0  # ms; 0 when the post didn't carry one


def dm_from_event(event: Any, broker_user_id: str) -> DirectMessage | None:
    """A websocket event, as a DM to the broker it should answer, or None.

    Only a user's own `posted` post in a DM channel counts: not the broker's own posts, not
    system posts, and never a post carrying `props.crew_broker` (another broker's reply).
    """
    if not isinstance(event, dict) or event.get("event") != "posted":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or data.get("channel_type") != "D":
        return None
    try:
        post = json.loads(str(data.get("post", "")))
    except ValueError:
        return None
    members = str(data.get("channel_name", "")).split("__")
    if broker_user_id not in members:
        return None
    return dm_from_post(post, broker_user_id)


def dm_from_post(post: Any, broker_user_id: str) -> DirectMessage | None:
    """A post, as a DM it should answer, or None: the checks `dm_from_event` makes on the post
    itself, which the backfill makes on each post it reads (#159)."""
    if not isinstance(post, dict):
        return None
    sender = post.get("user_id")
    channel = post.get("channel_id")
    message = post.get("message")
    if not (isinstance(sender, str) and sender and isinstance(channel, str) and channel):
        return None
    if sender == broker_user_id or post.get("type") or not isinstance(message, str):
        return None
    props = post.get("props")
    if isinstance(props, dict) and props.get(BROKER_PROP):
        return None
    post_id = post.get("id")
    create_at = post.get("create_at")
    if isinstance(create_at, bool) or not isinstance(create_at, int):
        create_at = 0
    return DirectMessage(
        channel_id=channel,
        sender_user_id=sender,
        message=message,
        post_id=post_id if isinstance(post_id, str) else "",
        create_at=create_at,
    )


class WebSocketLike(Protocol):
    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> None: ...


WsConnect = Callable[[str, dict[str, str]], WebSocketLike]


def ws_connect(uri: str, headers: dict[str, str]) -> WebSocketLike:
    conn = websockets.sync.client.connect(
        uri,
        additional_headers=headers,
        ping_interval=WS_PING_S,
        ping_timeout=WS_PING_S,
        proxy=None,
    )
    return cast(WebSocketLike, conn)


class DMListener:
    """The broker account's websocket: switch commands from DMs, and their replies (§4.6).

    Reconnects with a capped backoff, resuming from the last event it saw when the server
    still has it.
    """

    def __init__(
        self,
        broker: Broker,
        server_url: str,
        token: str,
        *,
        connect: WsConnect = ws_connect,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self.broker = broker
        self.base_uri = re.sub(r"^http", "ws", server_url.rstrip("/")) + "/api/v4/websocket"
        self.token = token
        self._connect = connect
        self._sleep = sleep
        self.connection_id = ""
        self.next_seq = 0
        # Post ids already handled, newest last: a DM the backfill applied and the websocket
        # then replays (or the reverse) is handled once (#159).
        self._handled: OrderedDict[str, None] = OrderedDict()
        # A backfill not yet completed: the cursor stays where it is until one is (#159).
        self._owed = False
        self._newest_dm = 0  # ms: the newest DM handled, from anyone
        self._newest_partner_dm = 0  # ms: the newest from a human partner

    def uri(self) -> str:
        if not self.connection_id:
            return self.base_uri
        query = {"connection_id": self.connection_id, "sequence_number": self.next_seq}
        return f"{self.base_uri}?{urlencode(query)}"

    def handle(self, raw: str | bytes) -> None:
        """One websocket frame. Never raises for a malformed frame or a failed reply."""
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        seq = event.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and "event" in event:
            self.next_seq = seq + 1
        data = event.get("data")
        if event.get("event") == "hello" and isinstance(data, dict):
            conn_id = data.get("connection_id")
            if isinstance(conn_id, str):
                self.connection_id = conn_id
            self._owed = True
            self.backfill()
            return
        dm = dm_from_event(event, self.broker.config.broker_user_id)
        if dm is not None:
            if self._owed:
                self.backfill()  # what was missed goes first, once the server answers
            self._apply(dm)

    def backfill(self) -> None:
        """Apply the human partners' DMs sent since the cursor, owed at every `hello` (#159).

        The websocket resumes only while the server still holds its `connection_id`, which a
        broker restart always loses. Every partner's channel is read first, and the posts are
        applied in `create_at` order: the partners share one switch, so the latest instruction
        must land last. A DM older than `BACKFILL_MAX_AGE_S`, or older than a partner DM
        already handled, is answered "not applied" unless it only narrows
        (`NARROWING_COMMANDS`): applied late, a `social 2h` would open the crew when the
        partner believes it closed. If any read fails, nothing is applied, the cursor stays,
        and the backfill stays owed: it is tried again before the next DM and at the next
        `hello`. With no usable cursor, as on a fresh install, it starts the cursor now and
        replays nothing. Never raises.
        """
        cursor = self.broker.dm_cursor.load()
        now = self.broker.clock()
        if cursor <= 0:
            self._owed = False
            self._save_cursor(int(now.timestamp() * 1000))
            return
        broker_id = self.broker.config.broker_user_id
        missed: list[DirectMessage] = []
        for human in self.broker.config.human_user_ids:
            try:
                channel = self.broker.client.direct_channel(broker_id, human)
                posts = self.broker.client.posts_since(channel, cursor)
            except Exception:
                log.exception("DM backfill from %s failed; retried before the next DM", human)
                return
            for post in posts:
                dm = dm_from_post(post, broker_id)
                # `since` also returns older posts edited since: only new ones count.
                if dm is None or dm.sender_user_id != human or dm.create_at <= cursor:
                    continue
                if not post.get("delete_at"):
                    missed.append(dm)
        for dm in sorted(missed, key=lambda m: (m.create_at, m.post_id)):
            stale = now.timestamp() - dm.create_at / 1000 > BACKFILL_MAX_AGE_S
            superseded = dm.create_at < self._newest_partner_dm
            words = tuple(dm.message.strip().lower().split())
            if (stale or superseded) and words not in NARROWING_COMMANDS:
                sent = datetime.fromtimestamp(dm.create_at / 1000, UTC)
                self._apply(dm, refuse=f"sent {sent.isoformat(timespec='seconds')}")
            else:
                self._apply(dm)
        self._owed = False
        self._save_cursor(self._newest_dm)

    def _apply(self, dm: DirectMessage, *, refuse: str = "") -> None:
        """Handle one DM once: apply it and reply, or with `refuse`, reply that it was not
        applied. Then advance the cursor past it, unless a backfill is still owed."""
        if dm.post_id:
            if dm.post_id in self._handled:
                return
            self._handled[dm.post_id] = None
            if len(self._handled) > HANDLED_DMS_MAX:
                self._handled.popitem(last=False)
        try:
            if refuse:
                reply = f"not applied: {refuse} while the broker was offline; send it again"
            else:
                reply = self.broker.handle_dm(dm.sender_user_id, dm.message)
            self.broker.client.create_post(dm.channel_id, reply, props={BROKER_PROP: 1})
        except Exception:
            log.exception("switch DM from %s failed", dm.sender_user_id)
        self._newest_dm = max(self._newest_dm, dm.create_at)
        if dm.sender_user_id in self.broker.config.human_user_ids:
            self._newest_partner_dm = max(self._newest_partner_dm, dm.create_at)
        if not self._owed:
            self._save_cursor(dm.create_at)

    def _save_cursor(self, ms: int) -> None:
        """Move the cursor forward to `ms`; never back."""
        try:
            if ms > self.broker.dm_cursor.load():
                self.broker.dm_cursor.save(ms)
        except OSError:
            log.exception("DM cursor not saved")

    def run(self, stop: threading.Event) -> None:
        sleep = self._sleep or stop.wait
        delay = WS_BACKOFF_FIRST_S
        while not stop.is_set():
            try:
                ws = self._connect(self.uri(), {"Authorization": f"Bearer {self.token}"})
            except (OSError, TimeoutError, WebSocketException) as exc:
                log.warning("broker websocket: %s", exc)
                if sleep(delay):
                    return
                delay = min(delay * 2, WS_BACKOFF_MAX_S)
                continue
            try:
                while not stop.is_set():
                    try:
                        raw = ws.recv(timeout=WS_RECV_POLL_S)
                    except TimeoutError:
                        continue
                    delay = WS_BACKOFF_FIRST_S  # connected: the next loss starts again at 1 s
                    self.handle(raw)
            except (ConnectionClosed, OSError) as exc:
                if not stop.is_set():
                    log.warning("broker websocket lost: %s", exc)
            finally:
                with suppress(Exception):
                    ws.close()
            if not stop.is_set() and sleep(delay):
                return
            delay = min(delay * 2, WS_BACKOFF_MAX_S)


def start_dm_listener(listener: DMListener, stop: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=listener.run, args=(stop,), name="crewbroker-dm", daemon=True)
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
    start_membership_reconciler(broker, stop, config.membership_reconcile_s)
    start_dm_listener(DMListener(broker, config.server_url, credential), stop)

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
