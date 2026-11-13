"""Record files are written whole."""

from __future__ import annotations

import os
import stat
import threading
from typing import TYPE_CHECKING

from cloudfall import record_files

if TYPE_CHECKING:
    from pathlib import Path

_CONTENT = '{"status": "proposed"}\n'


def test_a_reader_never_finds_a_new_record_empty(tmp_path: Path) -> None:
    """A record is linked into place whole, never opened empty first (#217)."""
    path = tmp_path / "decision.json"
    stop = threading.Event()
    seen: set[str] = set()

    def _read() -> None:
        while not stop.is_set():
            try:
                seen.add(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue

    reader = threading.Thread(target=_read)
    reader.start()
    try:
        for _ in range(2000):
            record_files.create(path, _CONTENT)
            path.unlink()
    finally:
        stop.set()
        reader.join()

    assert seen == {_CONTENT}
    assert [entry.name for entry in tmp_path.iterdir()] == []


class _DirectorySyncs:
    """Records each flush of a directory, and what the record held at that moment."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen: list[str] = []

    def __call__(self, descriptor: int) -> None:
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
        assert os.fstat(descriptor).st_ino == self.path.parent.stat().st_ino
        os.fsync(descriptor)
        self.seen.append(self.path.read_text(encoding="utf-8"))


def test_the_directory_is_flushed_after_a_record_is_written(tmp_path: Path) -> None:
    """A power loss cannot undo a write the store reported done (#244)."""
    path = tmp_path / "decision.json"
    syncs = _DirectorySyncs(path)
    replaced = '{"status": "running"}\n'

    record_files.create(path, _CONTENT, fsync=syncs)
    record_files.replace(path, replaced, fsync=syncs)

    assert syncs.seen == [_CONTENT, replaced]
