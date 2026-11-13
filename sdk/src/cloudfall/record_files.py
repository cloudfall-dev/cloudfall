"""Write record files so a reader never finds half of one.

Decision records and proposal receipts are JSON files in one directory,
read by every command that lists them. A file is written through a
temporary file in the same directory and renamed over the record, so a
crash or a full disk mid-write leaves the old record or the new one, never
a truncated one. A new record is created exclusively, so two writers of
the same id cannot both succeed. A read-modify-write of one record holds
the directory's lock, so two processes updating it take turns.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_RECORD_MODE = 0o644
_TEMPORARY_SUFFIX = ".tmp"


@contextmanager
def locked(directory: Path) -> Iterator[None]:
    """Hold the directory's exclusive lock, creating the directory first.

    The lock is taken on the directory itself, so no lock file lands beside
    records a team commits. It is held per open descriptor: a second
    ``locked`` on the same directory waits, in this process too.
    """
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def create(path: Path, content: str) -> None:
    """Write a new record; raise ``FileExistsError`` when one of its name exists."""
    staged = _staged(path, content)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _RECORD_MODE)
    except FileExistsError:
        staged.unlink()
        raise
    os.close(descriptor)
    staged.replace(path)


def replace(path: Path, content: str) -> None:
    """Replace a record whole: a reader sees the old content or the new one."""
    _staged(path, content).replace(path)


def _staged(path: Path, content: str) -> Path:
    """Write ``content`` to a temporary file beside ``path`` and flush it to disk.

    The name starts with a dot and ends ``.tmp``, so no ``*.json`` reader
    of the directory takes it for a record.
    """
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=_TEMPORARY_SUFFIX
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(_RECORD_MODE)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    return staged
