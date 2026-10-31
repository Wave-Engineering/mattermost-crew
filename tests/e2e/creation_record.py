"""What an E2E run created on the server, for `scripts/smoke-devspace.sh --cleanup` (#157).

Cleanup acts only on what the run says it created, never on a name match alone, so a second
run that drew the same suffix, or a hand-made account whose name matches, is never touched.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

KINDS = ("user", "bot", "team")


class CreationRecord:
    """`{"run": <run>, "created": [{"kind", "id", "username"}]}` at `path`, rewritten whole
    through a temporary file and a rename after every `add`, so it is valid JSON even after a
    crash mid-run. For a team, `username` holds the team name. With `path` None (CI, and local
    runs) nothing is written."""

    def __init__(self, path: Path | None, run: str) -> None:
        self.path = path
        self.run = run
        self.created: list[dict[str, str]] = []
        self._lock = threading.Lock()  # the brokers mint from their own threads

    def add(self, kind: str, id: str, username: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"kind is one of {KINDS}, not {kind!r}")
        with self._lock:
            self.created.append({"kind": kind, "id": id, "username": username})
            if self.path is not None:
                self._write({"run": self.run, "created": self.created})

    def _write(self, body: dict[str, Any]) -> None:
        assert self.path is not None
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
