"""#157: the E2E world's creation record, which `smoke-devspace.sh --cleanup` acts on."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tests.e2e.creation_record import CreationRecord


def test_creation_record_is_valid_after_each_append(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    record = CreationRecord(path, "1a2b")
    entries = [("team", "t1", "crew-e2e1a2b"), ("user", "u1", "partner-oaw1a2b")]
    for n, entry in enumerate(entries, 1):
        record.add(*entry)
        body = json.loads(path.read_text())
        assert body["run"] == "1a2b"
        assert [tuple(e.values()) for e in body["created"]] == entries[:n]
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]  # no temp file left


def test_creation_record_loses_no_concurrent_add(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    record = CreationRecord(path, "1a2b")

    def mint(t: int) -> None:
        for n in range(25):
            record.add("bot", f"b{t}-{n}", f"d{t}{n}.oaw1a2b")

    threads = [threading.Thread(target=mint, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = {e["id"] for e in json.loads(path.read_text())["created"]}
    assert len(ids) == 8 * 25


def test_creation_record_without_a_path_writes_nothing(tmp_path: Path) -> None:
    record = CreationRecord(None, "1a2b")
    record.add("user", "u1", "partner-oaw1a2b")
    assert record.created == [{"kind": "user", "id": "u1", "username": "partner-oaw1a2b"}]
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        record.add("channel", "c1", "x")
