"""Story 2.4b: the persisted `MyThreads` store (Dev Spec §5.9; R-19; D-104)."""

from __future__ import annotations

import json
import logging
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from crewchat.threads import MyThreads

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_threads_persist_roundtrip(tmp_path: Path) -> None:
    clock = Clock()
    threads = MyThreads.load(tmp_path, 90, clock)
    threads.add("kessel", "root-a")
    clock.now += timedelta(minutes=1)
    threads.add("kessel", "root-b")
    threads.add("babel", "root-c")

    path = tmp_path / "threads" / "kessel.json"
    assert mode(tmp_path / "threads") == 0o700
    assert mode(path) == 0o600
    assert json.loads(path.read_text()) == {
        "roots": {"root-a": START.isoformat(), "root-b": (START + timedelta(minutes=1)).isoformat()}
    }
    # No temp file is left behind by the atomic write.
    assert sorted(p.name for p in (tmp_path / "threads").iterdir()) == ["babel.json", "kessel.json"]

    again = MyThreads.load(tmp_path, 90, clock)
    assert again.roots("kessel") == {"root-a", "root-b"}
    assert again.roots("babel") == {"root-c"}


def test_threads_last_seen_refreshed_on_add(tmp_path: Path) -> None:
    clock = Clock()
    threads = MyThreads.load(tmp_path, 90, clock)
    threads.add("kessel", "root-a")
    clock.now += timedelta(days=10)
    threads.add("kessel", "root-a")
    data = json.loads((tmp_path / "threads" / "kessel.json").read_text())
    assert data["roots"]["root-a"] == clock.now.isoformat()


def test_threads_prune_by_retention(tmp_path: Path) -> None:
    clock = Clock()
    threads = MyThreads.load(tmp_path, 30, clock)
    threads.add("kessel", "old")
    clock.now += timedelta(days=20)
    threads.add("kessel", "mid")

    # On load: 31 days on, "old" is past retention and "mid" is not.
    clock.now = START + timedelta(days=31)
    loaded = MyThreads.load(tmp_path, 30, clock)
    assert loaded.roots("kessel") == {"mid"}

    # On save: another 20 days on, an add drops "mid" from memory and the file.
    clock.now += timedelta(days=20)
    loaded.add("kessel", "new")
    assert loaded.roots("kessel") == {"new"}
    data = json.loads((tmp_path / "threads" / "kessel.json").read_text())
    assert set(data["roots"]) == {"new"}


def test_threads_limit_applies_after_pruning(tmp_path: Path) -> None:
    clock = Clock()
    threads = MyThreads.load(tmp_path, 90, clock, limit=2)
    for root in ("a", "b", "c"):
        clock.now += timedelta(seconds=1)
        threads.add("kessel", root)
    assert threads.roots("kessel") == {"b", "c"}
    assert MyThreads.load(tmp_path, 90, clock, limit=2).roots("kessel") == {"b", "c"}


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"roots": []}',
        '{"roots": {"r": 5}}',
        '{"roots": {"r": "yesterday"}}',
        "",
    ],
)
def test_threads_corrupt_file_is_empty(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="crewchat.threads")
    directory = tmp_path / "threads"
    directory.mkdir(mode=0o700)
    (directory / "kessel.json").write_text(content)
    (directory / "babel.json").write_text(json.dumps({"roots": {"ok": START.isoformat()}}))
    threads = MyThreads.load(tmp_path, 90, Clock())
    assert threads.roots("kessel") == set()
    assert threads.roots("babel") == {"ok"}
    errors = [r for r in caplog.records if "kessel.json" in r.getMessage()]
    assert len(errors) == 1
    # It is still usable, and the next add replaces the bad file.
    threads.add("kessel", "fresh")
    assert MyThreads.load(tmp_path, 90, Clock()).roots("kessel") == {"fresh"}


def test_threads_forget_deletes_file(tmp_path: Path) -> None:
    threads = MyThreads.load(tmp_path, 90, Clock())
    threads.add("kessel", "root-a")
    path = tmp_path / "threads" / "kessel.json"
    assert path.exists()
    threads.forget("kessel")
    assert not path.exists()
    assert threads.roots("kessel") == set()
    threads.forget("kessel")  # forgetting twice is fine
    assert MyThreads.load(tmp_path, 90, Clock()).roots("kessel") == set()


def test_threads_load_tightens_directory_mode(tmp_path: Path) -> None:
    directory = tmp_path / "threads"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    MyThreads.load(tmp_path, 90, Clock())
    assert mode(directory) == 0o700


def test_threads_in_memory_writes_nothing(tmp_path: Path) -> None:
    threads = MyThreads()
    threads.add("kessel", "root-a")
    assert threads.roots("kessel") == {"root-a"}
    assert list(tmp_path.iterdir()) == []


def test_threads_rejects_bad_retention(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MyThreads.load(tmp_path, 0)
