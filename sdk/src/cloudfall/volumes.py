"""The volume provider of S-014: decide on the controller, act on the host.

``logging-volumes.yml`` only reads a host: its volume groups, physical
volumes, logical volumes and the mount paths. This module turns that report
and the volume requirements into actions, or into a refusal that names the
server, the pool and the reason. Every refusal is decided before anything
changes on any server; the role that applies the actions only runs what is
decided here: ``lvcreate`` and ``mkfs`` for a missing volume, ``lvextend -r``
for a small one, then the fstab line, the mount and the unit drop-ins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cloudfall.requirements import ServerRequirements, VolumeRequirement

if TYPE_CHECKING:
    from pathlib import Path

FACTS_PLAYBOOK = "logging-volumes.yml"
REQUESTS_VARIABLE = "cloudfall_volume_requests"
"""``logging-volumes.yml``'s variable: server id, then what to read."""
REPORT_DIRECTORY_VARIABLE = "cloudfall_volume_report_directory"
ACTIONS_VARIABLE = "cloudfall_volume_actions"
"""``logging.yml``'s variable: server id, then the actions its volume role runs."""
ERROR_REFUSED = "volume_refused"
ERROR_REPORT_MISSING = "volume_report_missing"
_EXTENT_SLACK = 64 * 1024**2
"""Room for raid1 metadata and extent rounding on each physical volume."""


class VolumeRefusedError(RuntimeError):
    """A volume the provider will not touch; nothing changed on any server."""

    def __init__(self, server: str, pool: str, reason: str) -> None:
        """Name the server, the pool and the reason."""
        super().__init__(f"{server}: pool {pool}: {reason}")
        self.server = server
        self.pool = pool
        self.reason = reason
        self.code = ERROR_REFUSED


class VolumeAction(StrEnum):
    """What the role does to one volume."""

    CREATE = "create"
    GROW = "grow"
    KEEP = "keep"


@dataclass(frozen=True, slots=True)
class PlannedVolume:
    """One volume and what the role does to it on its server."""

    server: str
    requirement: VolumeRequirement
    action: VolumeAction
    size_bytes: int

    def as_dict(self) -> dict[str, object]:
        """Serialize for the role and the answer."""
        spec = self.requirement
        return {
            "name": spec.name.value,
            "action": self.action.value,
            "volumeGroup": spec.pool.volume_group.value,
            "logicalVolume": spec.logical_volume,
            "type": spec.volume_type,
            "physicalVolumes": [item.value for item in spec.physical_volumes],
            "sizeBytes": self.size_bytes,
            "path": spec.path.value,
            "label": spec.name.value[:16],
            "units": [unit.value for unit in spec.units],
        }


def volume_requests(
    servers: tuple[ServerRequirements, ...],
) -> dict[str, list[dict[str, object]]]:
    """Return, by server, the volume groups, volumes and paths the host reads."""
    requests: dict[str, list[dict[str, object]]] = {}
    for server, spec in _volumes(servers):
        requests.setdefault(server, []).append(
            {
                "name": spec.name.value,
                "volumeGroup": spec.pool.volume_group.value,
                "logicalVolume": spec.logical_volume,
                "path": spec.path.value,
            }
        )
    return requests


def read_reports(
    directory: Path, servers: tuple[ServerRequirements, ...]
) -> dict[str, dict[str, object]]:
    """Read what ``logging-volumes.yml`` wrote: one JSON report per server."""
    reports: dict[str, dict[str, object]] = {}
    for server in {server for server, _ in _volumes(servers)}:
        source = directory / f"{server}.json"
        if not source.is_file():
            message = f"{server} wrote no volume report; expected {source}"
            raise VolumeRefusedError(server, "-", message)
        content = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            message = f"{source} is not a volume report"
            raise VolumeRefusedError(server, "-", message)
        reports[server] = content
    return reports


def normalize_report(raw: dict[str, object]) -> dict[str, object]:
    """Turn what the host printed (LVM's JSON reports, blkid, findmnt) into facts.

    ``raw`` holds ``vgs``, ``pvs`` and ``lvs`` as the JSON ``--reportformat
    json`` prints (``lvs`` with ``-a``, so a raid1 volume's hidden images are
    there), ``filesystems`` by LV name, and ``paths`` by mount path.
    """
    groups: dict[str, object] = {
        str(row["vg_name"]): {} for row in _rows(raw, "vgs", "vg")
    }
    physical = {
        str(row["pv_name"]): {
            "volumeGroup": str(row["vg_name"]) or None,
            "freeBytes": int(str(row["pv_free"]) or "0"),
        }
        for row in _rows(raw, "pvs", "pv")
    }
    # Keyed by volume group too: another group may hold an LV of the same name
    rows = {
        (str(row["vg_name"]), str(row["lv_name"])): row
        for row in _rows(raw, "lvs", "lv")
    }
    filesystems = raw.get("filesystems")
    logical: dict[str, object] = {}
    for (group, name), row in rows.items():
        if name.startswith("["):
            continue
        logical[f"{group}/{name}"] = {
            "type": str(row["segtype"]),
            "sizeBytes": int(str(row["lv_size"])),
            "devices": sorted(_devices(group, name, rows)),
            "mapper": str(row.get("lv_dm_path", "")) or None,
            "filesystem": (
                filesystems.get(name) if isinstance(filesystems, dict) else None
            ),
        }
    paths = raw.get("paths")
    return {
        "volumeGroups": groups,
        "physicalVolumes": physical,
        "logicalVolumes": logical,
        "paths": paths if isinstance(paths, dict) else {},
    }


def _rows(raw: dict[str, object], key: str, kind: str) -> list[dict[str, object]]:
    text = raw.get(key)
    if not isinstance(text, str):
        message = f"volume report has no {key} output"
        raise TypeError(message)
    report = json.loads(text)["report"]
    return [row for section in report for row in section.get(kind, [])]


def _devices(
    group: str, name: str, rows: dict[tuple[str, str], dict[str, object]]
) -> set[str]:
    """Return the physical volumes an LV sits on, through raid1's hidden images."""
    found: set[str] = set()
    for part in str(rows[group, name].get("devices", "")).split(","):
        device = part.strip().rsplit("(", 1)[0]
        if not device:
            continue
        if device.startswith("/dev/"):
            found.add(device)
            continue
        hidden = f"[{device}]"
        if (group, hidden) in rows:
            found |= _devices(group, hidden, rows)
    return found


def plan_volumes(
    servers: tuple[ServerRequirements, ...],
    reports: dict[str, dict[str, object]],
) -> list[PlannedVolume]:
    """Decide every volume's action, or refuse before anything changes."""
    planned: list[PlannedVolume] = []
    for server, spec in _volumes(servers):
        planned.append(_plan(server, spec, reports[server]))
    _check_free_space(planned, reports)
    return planned


def actions_by_server(
    planned: list[PlannedVolume],
) -> dict[str, list[dict[str, object]]]:
    """Group the planned actions the role runs, by server."""
    actions: dict[str, list[dict[str, object]]] = {}
    for item in planned:
        actions.setdefault(item.server, []).append(item.as_dict())
    return actions


def _plan(
    server: str, spec: VolumeRequirement, report: dict[str, object]
) -> PlannedVolume:
    pool = spec.pool.resource_id.value
    group = spec.pool.volume_group.value
    groups = _mapping(report, "volumeGroups")
    if group not in groups:
        reason = f"volume group {group} does not exist"
        raise VolumeRefusedError(server, pool, reason)
    physical = _mapping(report, "physicalVolumes")
    for device in spec.physical_volumes:
        entry = physical.get(device.value)
        owner = entry.get("volumeGroup") if isinstance(entry, dict) else None
        if owner != group:
            reason = f"physical volume {device.value} is not in {group}"
            raise VolumeRefusedError(server, pool, reason)
    wanted = spec.minimum_size.value
    logical = _mapping(report, "logicalVolumes")
    paths = _mapping(report, "paths")
    path = _mapping(paths, spec.path.value) if spec.path.value in paths else {}
    lv_path = f"/dev/{group}/{spec.logical_volume}"
    mounted_from = path.get("mountedFrom")
    key = f"{group}/{spec.logical_volume}"
    if key not in logical:
        if mounted_from is not None:
            reason = f"{spec.path.value} is already mounted from {mounted_from}"
            raise VolumeRefusedError(server, pool, reason)
        if path.get("empty") is False:
            reason = (
                f"{spec.path.value} holds files and is not a mount; mounting "
                f"{spec.logical_volume} over it would hide them"
            )
            raise VolumeRefusedError(server, pool, reason)
        return PlannedVolume(server, spec, VolumeAction.CREATE, wanted)
    held = _mapping(logical, key)
    _refuse_mismatch(server, pool, spec, held)
    if mounted_from is not None and mounted_from not in {lv_path, held.get("mapper")}:
        reason = f"{spec.path.value} is already mounted from {mounted_from}"
        raise VolumeRefusedError(server, pool, reason)
    if mounted_from is None and path.get("empty") is False:
        reason = (
            f"{spec.path.value} holds files and is not {spec.logical_volume}'s "
            "mount; mounting over it would hide them"
        )
        raise VolumeRefusedError(server, pool, reason)
    size = _integer(held, "sizeBytes")
    if size < wanted:
        return PlannedVolume(server, spec, VolumeAction.GROW, wanted)
    return PlannedVolume(server, spec, VolumeAction.KEEP, size)


def _refuse_mismatch(
    server: str,
    pool: str,
    spec: VolumeRequirement,
    held: dict[str, object],
) -> None:
    segment = held.get("type")
    if segment != spec.volume_type:
        reason = (
            f"{spec.logical_volume} is {segment}, but the requirement wants "
            f"{spec.volume_type}"
        )
        raise VolumeRefusedError(server, pool, reason)
    devices = held.get("devices")
    wanted = sorted(item.value for item in spec.physical_volumes)
    if not isinstance(devices, list) or sorted(str(item) for item in devices) != wanted:
        reason = f"{spec.logical_volume} sits on {devices}, not on {', '.join(wanted)}"
        raise VolumeRefusedError(server, pool, reason)
    filesystem = held.get("filesystem")
    if filesystem is None:
        # A run that stopped between lvcreate and mkfs leaves a bare LV; mkfs
        # runs only on an LV made in the same run, so it is never formatted here
        reason = (
            f"{spec.logical_volume} exists but has no filesystem; check it holds "
            "no data, then run mkfs.ext4 on it by hand or remove it"
        )
        raise VolumeRefusedError(server, pool, reason)
    if filesystem != "ext4":
        reason = f"{spec.logical_volume} holds {filesystem}, not ext4"
        raise VolumeRefusedError(server, pool, reason)


def _check_free_space(
    planned: list[PlannedVolume], reports: dict[str, dict[str, object]]
) -> None:
    """Refuse when the physical volumes cannot hold what is created or grown."""
    needed: dict[tuple[str, str], int] = {}
    for item in planned:
        if item.action is VolumeAction.KEEP:
            continue
        current = 0
        if item.action is VolumeAction.GROW:
            logical = _mapping(reports[item.server], "logicalVolumes")
            spec = item.requirement
            held = f"{spec.pool.volume_group.value}/{spec.logical_volume}"
            current = _integer(_mapping(logical, held), "sizeBytes")
        extra = item.size_bytes - current
        for device in item.requirement.physical_volumes:
            key = (item.server, device.value)
            needed[key] = needed.get(key, 0) + extra + _EXTENT_SLACK
    for (server, device_path), amount in needed.items():
        physical = _mapping(_mapping(reports[server], "physicalVolumes"), device_path)
        free = _integer(physical, "freeBytes")
        if free < amount:
            pool = next(
                item.requirement.pool.resource_id.value
                for item in planned
                if item.server == server
                and device_path
                in {pv.value for pv in item.requirement.physical_volumes}
            )
            reason = f"{device_path} has {free} bytes free; the volumes need {amount}"
            raise VolumeRefusedError(server, pool, reason)


def _volumes(
    servers: tuple[ServerRequirements, ...],
) -> list[tuple[str, VolumeRequirement]]:
    return [
        (server.server_id.value, requirement.spec)
        for server in servers
        for requirement in server.requirements
        if isinstance(requirement.spec, VolumeRequirement)
    ]


def _mapping(content: dict[str, object], key: str) -> dict[str, object]:
    value = content.get(key)
    if not isinstance(value, dict):
        message = f"volume report field {key!r} is not an object"
        raise TypeError(message)
    return value


def _integer(content: dict[str, object], key: str) -> int:
    value = content.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"volume report field {key!r} is not an integer"
        raise TypeError(message)
    return value
