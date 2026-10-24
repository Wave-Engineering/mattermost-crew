"""Story 1.4: the daemon's frame schema and protocol version (CP-05, D-056)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from crewchat.protocol import PROTO, ProtocolError, err, ok, parse_frame


def frame(**kw: Any) -> bytes:
    base: dict[str, Any] = {"proto": PROTO, "id": "r1", "op": "health", "args": {}}
    base.update(kw)
    return json.dumps({k: v for k, v in base.items() if v is not ...}).encode()


def test_parse_frame_accepts_older_proto() -> None:
    assert PROTO == 1
    for proto in (0, 1):
        req = parse_frame(frame(proto=proto))
        assert (req.proto, req.id, req.op, req.args) == (proto, "r1", "health", {})
    with pytest.raises(ProtocolError) as info:
        parse_frame(frame(proto=2))
    assert info.value.code == "unsupported"
    assert info.value.req_id == "r1"
    assert str(info.value) == "unsupported"


@pytest.mark.parametrize(
    "line",
    [
        b"not json",
        b"[1, 2]",
        frame(id=...),
        frame(id=7),
        frame(proto=...),
        frame(proto="1"),
        frame(proto=True),
        frame(proto=-1),
        frame(op=...),
        frame(op=""),
        frame(args=[1]),
    ],
)
def test_parse_frame_rejects_malformed(line: bytes) -> None:
    with pytest.raises(ProtocolError) as info:
        parse_frame(line)
    assert info.value.code == "bad_request"


def test_args_default_to_empty() -> None:
    assert parse_frame(frame(args=...)).args == {}
    assert parse_frame(frame(args=None)).args == {}


def test_ok_and_err_shapes() -> None:
    assert ok("r1", {"a": 1}) == {"proto": 1, "id": "r1", "ok": True, "result": {"a": 1}}
    assert err("r1", "unsupported") == {
        "proto": 1,
        "id": "r1",
        "ok": False,
        "error": "unsupported",
    }
