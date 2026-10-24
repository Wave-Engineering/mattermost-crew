"""Story 1.3: the broker's ops and its socket (R-05, R-08, R-22; D-100)."""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from crewbroker.service import Broker, BrokerServer, OpError, call, handle_frame

ALL_OPS: dict[str, dict[str, Any]] = {
    "mint": {"dev_name": "babel", "crew": "oaw"},
    "remint": {"dev_name": "babel", "crew": "oaw"},
    "revoke": {"token_id": "t1"},
    "crew_info": {},
}


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # A unix socket path is limited to 108 bytes; pytest's tmp_path can be longer.
    with tempfile.TemporaryDirectory(prefix="cb-", dir="/tmp") as d:
        yield Path(d)


def serving(broker: Broker, path: Path, allowed_uid: int) -> tuple[BrokerServer, threading.Thread]:
    server = BrokerServer(path, broker, allowed_uid)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_remint_keeps_old_token_until_revoke(broker: Broker, fake: Any) -> None:
    first = broker.mint({"dev_name": "babel", "crew": "oaw"})
    second = broker.remint({"dev_name": "babel", "crew": "oaw"})
    assert second["token_id"] != first["token_id"]
    assert set(second) == {"token", "token_id"}
    assert fake.token_works(first["token"]) and fake.token_works(second["token"])

    assert broker.revoke({"token_id": first["token_id"]}) == {}
    assert not fake.token_works(first["token"])
    assert fake.token_works(second["token"])
    assert broker.tokens.get(first["token_id"]) is None
    # Only tokens this broker minted can be revoked through it.
    with pytest.raises(OpError, match="not_found"):
        broker.revoke({"token_id": first["token_id"]})
    with pytest.raises(OpError, match="not_found"):
        broker.remint({"dev_name": "never", "crew": "oaw"})


def test_revoke_notfound_upstream_is_success(broker: Broker, fake: Any) -> None:
    minted = broker.mint({"dev_name": "babel", "crew": "oaw"})
    del fake.tokens[minted["token_id"]]  # already revoked on the server
    assert broker.revoke({"token_id": minted["token_id"]}) == {}
    assert broker.tokens.get(minted["token_id"]) is None


def test_every_op_refused_to_non_daemon_peer(broker: Broker, fake: Any, short_dir: Path) -> None:
    path = short_dir / "oaw-dev.sock"
    server, _ = serving(broker, path, allowed_uid=os.getuid() + 1)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o660
        for op, args in ALL_OPS.items():
            reply = call(str(path), op, args)
            assert reply["ok"] is False and reply["error"] == "forbidden", op
            assert "result" not in reply and "token" not in json.dumps(reply)
        assert fake.count("create_bot") == 0 and fake.tokens == {}
    finally:
        server.shutdown()
        server.server_close()

    # The control: the same socket, with this test's UID as the daemon's, does mint.
    server, _ = serving(broker, path, allowed_uid=os.getuid())
    try:
        reply = call(str(path), "mint", ALL_OPS["mint"])
        assert reply["ok"] is True and reply["result"]["account"] == "babel.oaw"
        assert reply["proto"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_crew_info_lists_minted_bots(broker: Broker, fake: Any) -> None:
    a = broker.mint({"dev_name": "a", "crew": "oaw"})["account"]
    b = broker.mint({"dev_name": "b", "crew": "oaw"})["account"]
    # A bot the broker owns but never minted through ensure_bot is not listed.
    fake.add_user("stray.oaw", is_bot=True, owner_id="broker-user-id")
    info = broker.crew_info({})
    ids = {u["username"]: u["id"] for u in fake.users.values()}
    assert info == {
        "broker_user_id": "broker-user-id",
        "human_user_ids": ["human-user-id"],
        "trusted": ["otter"],
        "minted_bot_ids": [ids[a], ids[b]],
    }


def test_crew_info_drops_reassigned_bot(broker: Broker, fake: Any) -> None:
    broker.mint({"dev_name": "a", "crew": "oaw"})
    broker.mint({"dev_name": "b", "crew": "oaw"})
    ids = {u["username"]: u["id"] for u in fake.users.values()}
    a, b = ids["a.oaw"], ids["b.oaw"]

    # Reassigned: missing from the broker's GET /bots, so dropped.
    fake.reassign(a, "someone-else")
    assert broker.crew_info({})["minted_bot_ids"] == [b]

    # With read_others_bots, GET /bots lists it again, but with another owner_id: dropped.
    fake.read_others_bots = True
    assert a in [bot["user_id"] for bot in fake.list_bots()]
    assert broker.crew_info({})["minted_bot_ids"] == [b]

    # Assigned back: listed again on the next GET /bots.
    fake.read_others_bots = False
    fake.reassign(a, "broker-user-id")
    assert broker.crew_info({})["minted_bot_ids"] == [a, b]

    # GET /bots fails: an error, never the unfiltered list.
    fake.list_bots_fails = True
    with pytest.raises(OpError, match="upstream"):
        broker.crew_info({})
    reply = handle_frame(broker, b'{"proto":1,"id":"x","op":"crew_info","args":{}}', True)
    assert reply["ok"] is False and "result" not in reply


def test_crew_info_leaves_out_pruned_bot(broker: Broker, fake: Any, clock: Any) -> None:
    from datetime import timedelta

    broker.mint({"dev_name": "a", "crew": "oaw"})
    clock.now += timedelta(days=31)
    broker.prune()
    assert broker.crew_info({})["minted_bot_ids"] == []
    broker.mint({"dev_name": "a", "crew": "oaw"})
    assert len(broker.crew_info({})["minted_bot_ids"]) == 1


def test_crew_info_own_fields_only(
    broker: Broker, fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker.mint({"dev_name": "a", "crew": "oaw"})
    connects: list[Any] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def spy_connect(self: socket.socket, address: Any) -> None:
        connects.append(address)
        real_connect(self, address)

    def spy_connect_ex(self: socket.socket, address: Any) -> int:
        connects.append(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", spy_connect_ex)
    info = broker.crew_info({})
    assert connects == []
    # Trusted crews by name only: no other crew's membership data.
    assert info["trusted"] == ["otter"]
    assert set(info) == {"broker_user_id", "human_user_ids", "trusted", "minted_bot_ids"}
    assert all(isinstance(name, str) for name in info["trusted"])


@pytest.mark.parametrize(
    ("line", "error"),
    [
        (b'{"proto":2,"id":"x","op":"crew_info"}', "unsupported"),
        (b'{"proto":1,"id":"x","op":"switch_state"}', "unsupported"),
        (b"not json", "bad_request: not JSON"),
        (b'{"proto":1,"id":"x","op":"mint","args":[]}', "bad_request: args is an object"),
        (b'{"proto":1,"id":"x","op":"mint","args":{"dev_name":"a","crew":"other"}}', None),
    ],
)
def test_frame_errors(broker: Broker, line: bytes, error: str | None) -> None:
    reply = handle_frame(broker, line, True)
    assert reply["ok"] is False and reply["proto"] == 1
    if error is not None:
        assert reply["error"] == error
    else:
        assert reply["error"].startswith("bad_request")


def test_older_proto_accepted(broker: Broker) -> None:
    reply = handle_frame(broker, b'{"proto":0,"id":"x","op":"crew_info"}', True)
    assert reply["ok"] is True and reply["id"] == "x"
