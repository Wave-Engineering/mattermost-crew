"""The daemon's client for crewkiosk-broker's control socket (#141; D-111).

`audit(answer_id)` returns the broker's audit lines for one research answer, which the
broker forgets once read, and `metrics()` each research server's daily overhead summary.
The broker serves only the daemon's uid on that socket (`SO_PEERCRED`). A broker that is
missing, slow or answers nonsense is logged at WARNING and reads as no lines and no metrics:
an answer is never held up by its audit. Wiring this into the kiosk flow is #142.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# Its own directory, not the broker's runtime one (packaging/crewkiosk-broker-control.socket).
CONTROL_SOCKET = "/run/crewkiosk-broker-control/control.sock"
TIMEOUT_S = 2.0
MAX_REPLY_BYTES = 4 * 1024 * 1024


class KioskBrokerClient:
    def __init__(self, control_socket: str = CONTROL_SOCKET, *, timeout_s: float = TIMEOUT_S):
        self.control_socket = control_socket
        self.timeout_s = timeout_s

    async def audit(self, answer_id: str) -> list[dict[str, Any]]:
        reply = await self._ask({"op": "audit", "answer_id": answer_id})
        lines = reply.get("lines") if reply is not None else None
        if not isinstance(lines, list):
            return []
        return [line for line in lines if isinstance(line, dict)]

    async def metrics(self) -> dict[str, Any] | None:
        reply = await self._ask({"op": "metrics"})
        metrics = reply.get("metrics") if reply is not None else None
        return metrics if isinstance(metrics, dict) else None

    async def _ask(self, request: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._exchange(request), self.timeout_s)
        except TimeoutError:
            log.warning("kiosk broker: no answer within %.0fs", self.timeout_s)
        except (OSError, ValueError) as exc:
            log.warning("kiosk broker: unavailable at %s: %s", self.control_socket, exc)
        return None

    async def _exchange(self, request: dict[str, Any]) -> dict[str, Any] | None:
        reader, writer = await asyncio.open_unix_connection(
            self.control_socket, limit=MAX_REPLY_BYTES
        )
        try:
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        reply = json.loads(line)
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            error = reply.get("error") if isinstance(reply, dict) else None
            log.warning("kiosk broker: refused %s: %s", request.get("op"), error)
            return None
        return reply
