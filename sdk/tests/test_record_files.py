"""Record files are written whole."""

from __future__ import annotations

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
