"""The component's lock on each of its servers, read and cleared (#221).

Deploy, rollback and restart take a lock per component on each server before
they change anything there, and release it however the run ends
(``tasks/component-lock-take.yml`` in the engine). A run that died on the way
leaves its lock behind; nothing removes it by age. ``cloudfall lock clear``
reads the locks through the engine's ``lock.yml``, shows them to a person,
and once they typed the component id removes exactly the locks it showed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from cloudfall.component_lock import (
    ERROR_LOCK_CHANGED,
    ERROR_LOCK_RECORD_INVALID,
    LOCK_DIRECTORY,
    TIMESTAMP_FORMAT,
    age_text,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import LifecycleError, run_engine_playbook
from cloudfall.validation import validate_config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from cloudfall.domain import ResourceId
    from cloudfall.lifecycle import EngineContext

ERROR_LOCK_NOT_CLEARED = "component_lock_not_cleared"
LOCK_PLAYBOOK = "lock.yml"
_FIELDS = ("component", "action", "user", "host", "since", "token")
_READING_FIELDS = {"server", "lock", "cleared"}


@dataclass(frozen=True, slots=True)
class ServerLockHolder:
    """The run that holds a component's lock on one server, as its record says."""

    component: str
    action: str
    user: str
    host: str
    """The management host the run was started on."""
    since: datetime
    token: str
    """Random per run and server, so a run releases only the lock it took."""

    @classmethod
    def from_text(cls, text: str, where: str) -> ServerLockHolder:
        """Read a holder record strictly, refusing anything but its fields."""
        try:
            raw = cast("object", json.loads(text))
        except json.JSONDecodeError as error:
            raise _invalid(where, "it is not JSON") from error
        if not isinstance(raw, dict) or set(raw) != set(_FIELDS):
            raise _invalid(where, f"it must hold exactly {', '.join(_FIELDS)}")
        record = cast("dict[str, object]", raw)
        if not all(isinstance(value, str) and value for value in record.values()):
            raise _invalid(where, "every field must be a non-empty string")
        strings = cast("dict[str, str]", record)
        try:
            since = datetime.strptime(strings["since"], TIMESTAMP_FORMAT).replace(
                tzinfo=UTC
            )
        except ValueError as error:
            raise _invalid(where, f"since must read like {TIMESTAMP_FORMAT}") from error
        return cls(
            component=strings["component"],
            action=strings["action"],
            user=strings["user"],
            host=strings["host"],
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
            "since": self.since.strftime(TIMESTAMP_FORMAT),
            "token": self.token,
        }


@dataclass(frozen=True, slots=True)
class ServerLock:
    """A component's lock as found on one server: its holder and its age."""

    server: ResourceId
    holder: ServerLockHolder
    age_seconds: int

    def describe(self) -> str:
        """Name the server, who holds the lock there, and since when."""
        holder = self.holder
        return (
            f"component {holder.component} is locked on server {self.server} by a "
            f"{holder.action} that {holder.user} started on {holder.host} at "
            f"{holder.since.strftime(TIMESTAMP_FORMAT)}, "
            f"{age_text(self.age_seconds)} ago"
        )

    def as_dict(self) -> dict[str, object]:
        """Return the lock as an answer names it."""
        return {
            "server": self.server.value,
            "holder": self.holder.as_dict(),
            "ageSeconds": self.age_seconds,
        }


@dataclass(frozen=True, slots=True)
class ServerLocks:
    """What one reading of a component's servers found."""

    held: tuple[ServerLock, ...]
    unread: tuple[ResourceId, ...]
    """Servers that could not be reached, so their lock is not known."""

    def describe(self) -> str:
        """Every lock found, and every server that could not be read."""
        parts = [lock.describe() for lock in self.held]
        if self.unread:
            servers = ", ".join(server.value for server in self.unread)
            parts.append(
                f"server {servers} could not be reached, so a lock there, if "
                "any, stays"
            )
        return "; ".join(parts)


def read_server_locks(context: EngineContext, component_id: ResourceId) -> ServerLocks:
    """Read the component's lock on each of its declared servers.

    A component the config no longer declares has no servers to look on, so
    nothing is read; its lock on the management host is still clearable.
    """
    servers = _declared_servers(context, component_id)
    if not servers:
        return ServerLocks(held=(), unread=())
    readings = _run(context, component_id, servers, {})
    now = datetime.now(UTC)
    held: list[ServerLock] = []
    for server in servers:
        reading = readings.get(server)
        if reading is None or reading.lock is None:
            continue
        holder = ServerLockHolder.from_text(reading.lock, f"on server {server}")
        age = int((now - holder.since).total_seconds())
        held.append(ServerLock(server=server, holder=holder, age_seconds=max(0, age)))
    return ServerLocks(
        held=tuple(held),
        unread=tuple(server for server in servers if server not in readings),
    )


def clear_server_locks(
    context: EngineContext, component_id: ResourceId, shown: ServerLocks
) -> None:
    """Remove exactly the locks a person was shown, or say which stayed.

    A lock taken again since it was shown, or on a server no longer reached,
    is left as it is and the call fails naming it.
    """
    if not shown.held:
        return
    tokens = {lock.server.value: lock.holder.token for lock in shown.held}
    servers = tuple(lock.server for lock in shown.held)
    readings = _run(context, component_id, servers, tokens)
    changed = [
        server.value
        for server in servers
        if server in readings and not readings[server].cleared
    ]
    if changed:
        detail = (
            f"the lock of component {component_id} on server {', '.join(changed)} "
            "was released or taken again since it was shown; look at it again "
            "before clearing it"
        )
        raise LifecycleError(ERROR_LOCK_CHANGED, detail)
    missed = [server.value for server in servers if server not in readings]
    if missed:
        detail = (
            f"server {', '.join(missed)} could not be reached, so the lock of "
            f"component {component_id} there stays; run the command again once "
            "it is reachable"
        )
        raise LifecycleError(ERROR_LOCK_NOT_CLEARED, detail)


@dataclass(frozen=True, slots=True)
class _Reading:
    lock: str | None
    cleared: bool


def _declared_servers(
    context: EngineContext, component_id: ResourceId
) -> tuple[ResourceId, ...]:
    state = validate_config(context.project_directory, context.schema_directory)
    inventory = PlatformInventory.from_state(state)
    for component in inventory.components:
        if component.resource_id == component_id:
            return component.server_ids
    return ()


def _run(
    context: EngineContext,
    component_id: ResourceId,
    servers: tuple[ResourceId, ...],
    clear: Mapping[str, str],
) -> dict[ResourceId, _Reading]:
    directory = (
        context.project_directory / LOCK_DIRECTORY / "servers" / component_id.value
    )
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    extra_vars: dict[str, object] = {
        "cloudfall_deploy_component_id": component_id.value,
        "cloudfall_component_lock_reading": str(directory.resolve()),
    }
    if clear:
        extra_vars["cloudfall_component_lock_clear"] = dict(clear)
    run_engine_playbook(context, LOCK_PLAYBOOK, extra_vars)
    readings: dict[ResourceId, _Reading] = {}
    for server in servers:
        path = directory / f"{server.value}.json"
        if path.is_file():
            readings[server] = _reading(path, server)
    return readings


def _reading(path: Path, server: ResourceId) -> _Reading:
    raw = cast("object", json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict) or set(raw) != _READING_FIELDS:
        detail = f"{LOCK_PLAYBOOK} wrote a reading without {sorted(_READING_FIELDS)}"
        raise LifecycleError(ERROR_LOCK_RECORD_INVALID, f"{detail}: {path}")
    reading = cast("dict[str, object]", raw)
    lock, cleared = reading["lock"], reading["cleared"]
    if (
        reading["server"] != server.value
        or not (lock is None or isinstance(lock, str))
        or not isinstance(cleared, bool)
    ):
        detail = f"{LOCK_PLAYBOOK} wrote a malformed reading for {server}: {path}"
        raise LifecycleError(ERROR_LOCK_RECORD_INVALID, detail)
    return _Reading(lock=lock, cleared=cleared)


def _invalid(where: str, reason: str) -> LifecycleError:
    detail = (
        f"the component lock record {where} is malformed: {reason}; a person "
        "reads it and removes it by hand"
    )
    return LifecycleError(ERROR_LOCK_RECORD_INVALID, detail)
