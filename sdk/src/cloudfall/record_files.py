"""Write record files so a reader never finds half of one.

Decision records and proposal receipts are JSON files in one directory,
read by every command that lists them. A file is written through a
temporary file in the same directory and renamed over the record, so a
crash or a full disk mid-write leaves the old record or the new one, never
a truncated one. A new record is created exclusively, so two writers of
the same id cannot both succeed. A read-modify-write of one record holds
the directory's lock, so two processes updating it take turns.

A listing reads every record it can and reports each one it cannot, with
its path and why, rather than stopping at the first: one corrupt file must
not hide the rest of the record. Each record names the schema it is read
with by its ``apiVersion``, so a later version's schema can sit beside the
first one, and carries the Cloudfall version that last wrote it.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_RECORD_MODE = 0o644
_TEMPORARY_SUFFIX = ".tmp"
_RECORD_PATTERN = "*.json"

WRITER_VERSION_FIELD = "writerVersion"
"""The ``spec`` field naming the Cloudfall version that last wrote a record."""


@dataclass(frozen=True, slots=True)
class UnreadableRecord:
    """One record file a listing could not read, and why."""

    path: Path
    reason: str

    def as_dict(self) -> dict[str, object]:
        """Serialize the finding for system boundaries."""
        return {"path": str(self.path), "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Listing[RecordT]:
    """The records a directory holds that could be read, and those that could not."""

    records: tuple[RecordT, ...]
    unreadable: tuple[UnreadableRecord, ...] = ()


def record_paths(directory: Path) -> tuple[Path, ...]:
    """Return every record file in the directory, by name; none when it is absent."""
    if not directory.is_dir():
        return ()
    return tuple(sorted(directory.glob(_RECORD_PATTERN)))


def schema_for(document: Mapping[str, object], schemas: Mapping[str, str]) -> str:
    """Return the schema a record's ``apiVersion`` is read with.

    Raises ``LookupError`` naming the versions this Cloudfall reads when the
    record names none of them.
    """
    api_version = document.get("apiVersion")
    if isinstance(api_version, str) and api_version in schemas:
        return schemas[api_version]
    known = ", ".join(sorted(schemas))
    message = f"apiVersion {api_version!r} is not one this Cloudfall reads ({known})"
    raise LookupError(message)


def writer_version() -> str:
    """Return the installed Cloudfall version, which a record names as its writer."""
    return metadata.version("cloudfall")


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
