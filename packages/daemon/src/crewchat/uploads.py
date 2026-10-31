"""Attachments the shim streams to the daemon (Dev Spec §5.4 `upload`; Story 2.4c; R-15).

The daemon never reads a path on the agent's side (§5.3), and a frame is at most 64 KiB
(`crewchat.protocol.MAX_FRAME_BYTES`). So the shim reads the file as the agent's user and sends
it in chunks: `upload{dev_name, name, seq, data_b64, last}`. Chunk `seq` 0 starts an upload,
each next chunk carries the next `seq`, and the one with `last: true` closes it and answers
`{upload_id, size}`. `send` and `dm` then name it in `attachments`.

- Uploads live under `/run/crewchat/uploads/<upload_id>` (the daemon's `RuntimeDirectory`),
  each file mode `0600` in a directory of mode `0700`, owned by the daemon's user (`crewchat`).
- One upload is at most `MAX_UPLOAD_BYTES` (8 MiB). Past it, `upload` refuses with
  `too_large` and the partial file is deleted.
- An upload belongs to the agent that sent it: no other Dev-Name can attach it.
- A finished upload is deleted once its post is made (`release`), and any upload, finished or
  not, is deleted 10 minutes after it started (`sweep`, on every call).
- Uploads from before a restart are deleted when the daemon starts (clear_stale): the directory
  outlives the daemon (RuntimeDirectoryPreserve=yes), and sweep only knows the uploads in memory.

An unfinished upload is keyed by (Dev-Name, name): a shim sends one call at a time, and a
new `seq` 0 for the same pair replaces whatever was in flight.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from crewchat.adapters.base import Attachment

UPLOADS_DIR = Path("/run/crewchat/uploads")
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
UPLOAD_TTL = timedelta(minutes=10)
MAX_NAME_CHARS = 255
DIR_MODE = 0o700
FILE_MODE = 0o600
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")

log = logging.getLogger("crewchat.uploads")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class UploadError(Exception):
    """An upload refused. `code` (`bad_request`, `too_large`, `not_found`) goes to the caller."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass
class _Upload:
    upload_id: str
    dev_name: str
    name: str
    path: Path
    started: datetime
    size: int = 0
    next_seq: int = 0
    done: bool = False


def safe_name(name: Any) -> str:
    """The file's name as posted: its last path part, with no control characters."""
    if not isinstance(name, str):
        raise UploadError("bad_request", "name is a required string")
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = "".join(ch for ch in base if ch.isprintable())
    if not base or base in (".", ".."):
        raise UploadError("bad_request", f"name {name[:40]!r} is not a file name")
    if len(base) > MAX_NAME_CHARS:
        raise UploadError("bad_request", f"name is longer than {MAX_NAME_CHARS} characters")
    return base


class UploadStore:
    def __init__(
        self,
        directory: Path = UPLOADS_DIR,
        *,
        clock: Clock = utc_now,
        max_bytes: int = MAX_UPLOAD_BYTES,
        ttl: timedelta = UPLOAD_TTL,
    ) -> None:
        self.directory = directory
        self.clock = clock
        self.max_bytes = max_bytes
        self.ttl = ttl
        self._uploads: dict[str, _Upload] = {}  # upload_id -> upload
        self._open: dict[tuple[str, str], str] = {}  # (dev_name, name) -> unfinished upload_id

    def _ensure_dir(self) -> None:
        self.directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(self.directory, DIR_MODE)

    def _drop(self, upload: _Upload) -> None:
        self._uploads.pop(upload.upload_id, None)
        key = (upload.dev_name, upload.name)
        if self._open.get(key) == upload.upload_id:
            del self._open[key]
        try:
            upload.path.unlink(missing_ok=True)
        except OSError as exc:
            log.error("cannot delete upload %s: %s", upload.path, exc)

    def sweep(self) -> int:
        """Delete every upload older than the TTL; how many went."""
        cutoff = self.clock() - self.ttl
        old = [u for u in self._uploads.values() if u.started <= cutoff]
        for upload in old:
            log.info("upload %s for %s expired", upload.upload_id, upload.dev_name)
            self._drop(upload)
        return len(old)

    def clear_stale(self) -> int:
        """Delete everything already in the directory, at daemon start; how many entries went.

        A startup-only step (#173): never from `__init__`, since a store built on the default
        path in a test would wipe a live daemon's uploads, and refused once this store holds
        an upload.
        """
        if self._uploads:
            raise RuntimeError("clear_stale runs only before the first upload")
        try:
            entries = list(self.directory.iterdir())
        except FileNotFoundError:
            return 0
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        if entries:
            log.info("cleared %d stale upload(s) from %s", len(entries), self.directory)
        return len(entries)

    def chunk(self, dev_name: str, name: Any, seq: Any, data_b64: Any, last: Any) -> dict[str, Any]:
        """One `upload` frame. `{seq, size}` for a middle chunk; `{upload_id, size}` on `last`."""
        self.sweep()
        file_name = safe_name(name)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise UploadError("bad_request", "seq is a non-negative integer")
        if not isinstance(last, bool):
            raise UploadError("bad_request", "last is true or false")
        if not isinstance(data_b64, str):
            raise UploadError("bad_request", "data_b64 is a base64 string")
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UploadError("bad_request", "data_b64 is not base64") from exc

        key = (dev_name, file_name)
        if seq == 0:
            stale = self._open.get(key)
            if stale is not None and stale in self._uploads:
                self._drop(self._uploads[stale])
            self._ensure_dir()
            upload_id = uuid.uuid4().hex
            path = self.directory / upload_id
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            os.close(fd)
            os.chmod(path, FILE_MODE)
            upload = _Upload(upload_id, dev_name, file_name, path, self.clock())
            self._uploads[upload_id] = upload
            self._open[key] = upload_id
        else:
            open_id = self._open.get(key)
            found = self._uploads.get(open_id) if open_id is not None else None
            if found is None:
                raise UploadError("bad_request", f"no upload of {file_name!r} in progress")
            upload = found
            if seq != upload.next_seq:
                self._drop(upload)
                raise UploadError(
                    "bad_request", f"chunk {seq} out of order (expected {upload.next_seq})"
                )

        if upload.size + len(data) > self.max_bytes:
            self._drop(upload)
            raise UploadError(
                "too_large", f"{file_name} is over {self.max_bytes // (1024 * 1024)} MiB"
            )
        with open(upload.path, "ab") as fh:
            fh.write(data)
        upload.size += len(data)
        upload.next_seq = seq + 1
        if not last:
            return {"seq": seq, "size": upload.size}
        upload.done = True
        self._open.pop(key, None)
        return {"upload_id": upload.upload_id, "size": upload.size}

    def take(self, dev_name: str, upload_ids: Any) -> list[Attachment]:
        """The finished uploads `upload_ids`, all `dev_name`'s; refused as a whole otherwise."""
        self.sweep()
        if not isinstance(upload_ids, list) or not upload_ids:
            raise UploadError("bad_request", "attachments is a non-empty list of upload ids")
        out: list[Attachment] = []
        for upload_id in upload_ids:
            if not isinstance(upload_id, str) or not UPLOAD_ID_RE.fullmatch(upload_id):
                raise UploadError("bad_request", f"{upload_id!r} is not an upload id")
            upload = self._uploads.get(upload_id)
            if upload is None or upload.dev_name != dev_name or not upload.done:
                raise UploadError("not_found", f"upload {upload_id} (expired or never finished)")
            out.append(Attachment(name=upload.name, path=upload.path, size=upload.size))
        return out

    def release(self, attachments: list[Attachment]) -> None:
        """Delete uploads once used (their post was made, or failed for good)."""
        for att in attachments:
            upload = self._uploads.get(att.path.name)
            if upload is not None:
                self._drop(upload)
            else:
                with contextlib.suppress(OSError):
                    att.path.unlink(missing_ok=True)
