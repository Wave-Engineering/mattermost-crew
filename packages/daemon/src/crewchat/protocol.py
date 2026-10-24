"""The daemon's socket protocol: JSON lines with the §5.4 frame shape (D-056).

Requests are `{"proto":1,"id":"<uuid>","op":"<op>","args":{...}}`; responses are
`{"proto":1,"id":"<uuid>","ok":true|false,"result"|"error":...}`.

A frame with a `proto` higher than `PROTO` gets `unsupported`; a lower one is accepted, so an
older client keeps working against a newer daemon (CP-05).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTO = 1
MAX_FRAME_BYTES = 64 * 1024


class ProtocolError(Exception):
    """A frame that can't be handled. `code` goes to the caller as the error."""

    def __init__(self, code: str, detail: str = "", req_id: Any = None) -> None:
        self.code = code
        self.detail = detail
        self.req_id = req_id
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Request:
    proto: int
    id: Any
    op: str
    args: dict[str, Any] = field(default_factory=dict)


def parse_frame(line: bytes) -> Request:
    """Validate one request frame: `proto`, `id`, `op` and `args`.

    Raises `ProtocolError` with `req_id` set whenever the frame got far enough to carry one,
    so the error response can still be matched to its request.
    """
    try:
        frame = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("bad_request", "not JSON") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("bad_request", "a frame is a JSON object")
    req_id = frame.get("id")
    if not isinstance(req_id, str) or not req_id:
        raise ProtocolError("bad_request", "id is a required string", None)
    proto = frame.get("proto")
    # bool is an int in Python; `true` is not a protocol version.
    if not isinstance(proto, int) or isinstance(proto, bool) or proto < 0:
        raise ProtocolError("bad_request", "proto is a required non-negative integer", req_id)
    if proto > PROTO:
        raise ProtocolError("unsupported", "", req_id)
    op = frame.get("op")
    if not isinstance(op, str) or not op:
        raise ProtocolError("bad_request", "op is a required string", req_id)
    args = frame.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ProtocolError("bad_request", "args is an object", req_id)
    return Request(proto=proto, id=req_id, op=op, args=args)


def ok(id: Any, result: Any) -> dict[str, Any]:
    return {"proto": PROTO, "id": id, "ok": True, "result": result}


def err(id: Any, msg: str) -> dict[str, Any]:
    return {"proto": PROTO, "id": id, "ok": False, "error": msg}


def encode(frame: dict[str, Any]) -> bytes:
    return json.dumps(frame).encode() + b"\n"
