"""One run at a time per component on the management host (#221).

A deploy, rollback or restart of a component, and a build of its release,
races any other such run of the same component: on the ``current`` symlink,
the managed unit, the release command's transient unit and the deployment
receipt. Each run holds the component's lock, a holder record at
``tmp/locks/<component>.json``, from before its first step until it ends,
passed, failed or interrupted. A second run fails at once, before any step,
naming who holds the lock and since when.

The record is written to a private file first and hard-linked into place,
which fails when the lock is already held, so taking it is atomic and the
record is whole as soon as it exists. A lock whose run died without
releasing it (a killed process, a lost machine) stays until a person clears
it with ``cloudfall lock clear``: nothing removes it by age alone.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import socket
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from cloudfall.lifecycle_error import LifecycleError
from cloudfall.project import RUNTIME_DIRECTORY

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from cloudfall.domain import ResourceId

ERROR_COMPONENT_LOCKED = "component_locked"
ERROR_LOCK_RECORD_INVALID = "component_lock_record_invalid"
ERROR_LOCK_ABSENT = "component_lock_absent"
ERROR_LOCK_CHANGED = "component_lock_changed"
ERROR_LOCK_IN_USE = "component_lock_in_use"

LOCK_DIRECTORY = f"{RUNTIME_DIRECTORY}/locks"
"""Where each component's lock lives, relative to the project."""

_TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"
_FIELDS = ("component", "action", "user", "host", "pid", "since", "token")
_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR


@dataclass(frozen=True, slots=True)
class LockHolder:
    """The run that holds a component's lock, as its record says."""

    component: str
    action: str
    user: str
    host: str
    pid: int
    since: datetime
    token: str
    """Random per run, so a run releases only the lock it took."""

    @classmethod
    def for_this_process(cls, component: ResourceId, action: str) -> LockHolder:
        """Describe a run of ``action`` this process is about to start."""
        return cls(
            component=component.value,
            action=action,
            user=getpass.getuser(),
            host=socket.gethostname(),
            pid=os.getpid(),
            since=datetime.now(UTC).replace(microsecond=0),
            token=secrets.token_hex(16),
        )

    @classmethod
    def from_file(cls, path: Path) -> LockHolder:
        """Read a holder record strictly, refusing anything but its fields."""
        try:
            raw = cast("object", json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as error:
            raise _invalid(path, "it is not JSON") from error
        if not isinstance(raw, dict) or set(raw) != set(_FIELDS):
            raise _invalid(path, f"it must hold exactly {', '.join(_FIELDS)}")
        record = cast("dict[str, object]", raw)
        pid = record["pid"]
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise _invalid(path, "pid must be a positive integer")
        texts = {name: record[name] for name in _FIELDS if name != "pid"}
        if not all(isinstance(value, str) and value for value in texts.values()):
            raise _invalid(path, "every field but pid must be a non-empty string")
        strings = cast("dict[str, str]", texts)
        try:
            since = datetime.strptime(strings["since"], _TIMESTAMP).replace(tzinfo=UTC)
        except ValueError as error:
            raise _invalid(path, f"since must read like {_TIMESTAMP}") from error
        return cls(
            component=strings["component"],
            action=strings["action"],
            user=strings["user"],
            host=strings["host"],
            pid=pid,
            since=since,
            token=strings["token"],
        )

    def as_dict(self) -> dict[str, object]:
        """Return the record as it is written, and as answers name it."""
        return {
            "component": self.component,
            "action": self.action,
            "user": self.user,
            "host": self.host,
            "pid": self.pid,
            "since": self.since.strftime(_TIMESTAMP),
            "token": self.token,
        }

    @property
    def stale(self) -> bool | None:
        """Whether the holding process is gone; ``None`` when it ran elsewhere."""
        if self.host != socket.gethostname():
            return None
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # The process exists and belongs to another user.
            return False
        return False


@dataclass(frozen=True, slots=True)
class HeldLock:
    """A component's lock as found: its holder, its age, and where it is."""

    holder: LockHolder
    path: Path
    age_seconds: int

    @property
    def stale(self) -> bool | None:
        """Whether the holder is known to be gone (see ``LockHolder.stale``)."""
        return self.holder.stale

    def describe(self) -> str:
        """Name who holds the lock, since when, and whether it still runs."""
        holder = self.holder
        if self.stale is None:
            state = f"whether it still runs on {holder.host} is not known from here"
        elif self.stale:
            state = "that process is no longer running, so the lock is stale"
        else:
            state = "that process is still running"
        return (
            f"component {holder.component} is locked by a {holder.action} that "
            f"{holder.user} started on {holder.host} (process {holder.pid}) at "
            f"{holder.since.strftime(_TIMESTAMP)}, {_age(self.age_seconds)} ago; "
            f"{state}"
        )

    def as_dict(self) -> dict[str, object]:
        """Return the lock as an answer's context names it."""
        return {
            "holder": self.holder.as_dict(),
            "ageSeconds": self.age_seconds,
            "stale": self.stale,
            "path": str(self.path),
        }


def lock_path(project_directory: Path, component: ResourceId) -> Path:
    """Return where the component's lock lives in the project."""
    return project_directory / LOCK_DIRECTORY / f"{component.value}.json"


def find_lock(project_directory: Path, component: ResourceId) -> HeldLock | None:
    """Return the component's lock, or ``None`` when no run holds it."""
    path = lock_path(project_directory, component)
    try:
        holder = LockHolder.from_file(path)
    except FileNotFoundError:
        return None
    age = int((datetime.now(UTC) - holder.since).total_seconds())
    return HeldLock(holder=holder, path=path, age_seconds=max(0, age))


@contextmanager
def component_lock(
    project_directory: Path, component: ResourceId, action: str
) -> Iterator[LockHolder]:
    """Hold the component's lock for one run, released however the run ends.

    Raises ``LifecycleError`` with code ``component_locked`` when another run
    holds it, naming that run and how to clear a lock it left behind.
    """
    path = lock_path(project_directory, component)
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = LockHolder.for_this_process(component, action)
    staged = path.with_name(f".{path.name}.{holder.token}")
    staged.write_text(json.dumps(holder.as_dict(), indent=2) + "\n", encoding="utf-8")
    try:
        os.link(staged, path)
    except FileExistsError:
        held = find_lock(project_directory, component)
        if held is None:
            # Released between the link and the read: report it, don't retry.
            detail = (
                f"component {component} was locked by another run a moment ago; "
                "run the command again"
            )
            raise LifecycleError(ERROR_COMPONENT_LOCKED, detail) from None
        raise _locked(held) from None
    finally:
        staged.unlink()
    try:
        yield holder
    finally:
        _release(path, holder)


def clear_lock(held: HeldLock) -> None:
    """Remove the lock a person looked at, if it is still that lock.

    Refuses a lock whose process still runs on this host, and one that was
    released or taken again since it was shown.
    """
    if held.stale is False:
        detail = (
            f"{held.describe()}. Stop that {held.holder.action} first, or wait "
            "for it to end; a running process's lock is never cleared"
        )
        raise LifecycleError(ERROR_LOCK_IN_USE, detail, held.as_dict())
    try:
        current = LockHolder.from_file(held.path)
    except FileNotFoundError:
        detail = f"the lock of component {held.holder.component} is already gone"
        raise LifecycleError(ERROR_LOCK_ABSENT, detail) from None
    if current.token != held.holder.token:
        detail = (
            f"the lock of component {held.holder.component} was taken again since "
            "it was shown; look at it again before clearing it"
        )
        raise LifecycleError(ERROR_LOCK_CHANGED, detail, {"holder": current.as_dict()})
    held.path.unlink()


def _release(path: Path, holder: LockHolder) -> None:
    """Remove the lock if this run still holds it; leave anyone else's."""
    try:
        current = LockHolder.from_file(path)
    except FileNotFoundError:
        return
    if current.token == holder.token:
        with suppress(FileNotFoundError):
            path.unlink()


def _locked(held: HeldLock) -> LifecycleError:
    detail = (
        f"{held.describe()}. If no {held.holder.action} of it is running, a "
        f"person clears the lock with `cloudfall lock clear {held.holder.component}`"
    )
    return LifecycleError(ERROR_COMPONENT_LOCKED, detail, held.as_dict())


def _invalid(path: Path, reason: str) -> LifecycleError:
    detail = (
        f"the component lock record {path} is malformed: {reason}; a person "
        "reads it and removes it by hand"
    )
    return LifecycleError(ERROR_LOCK_RECORD_INVALID, detail)


def _age(seconds: int) -> str:
    for unit, size in (("day", _DAY), ("hour", _HOUR), ("minute", _MINUTE)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"
