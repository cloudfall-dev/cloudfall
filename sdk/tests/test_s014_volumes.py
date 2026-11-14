"""S-014: the controller decides each volume's action, or refuses before any change."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from cloudfall.inventory import PlatformInventory
from cloudfall.requirements import requirements_of
from cloudfall.validation import validate_config
from cloudfall.volumes import (
    VolumeAction,
    VolumeRefusedError,
    actions_by_server,
    normalize_report,
    plan_volumes,
    volume_requests,
)
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
GIB = 1024**3
PRIMARY = "/dev/nvme0n1p5"
SECONDARY = "/dev/nvme1n1p5"
BY_ID_PRIMARY = "/dev/disk/by-id/nvme-SAMSUNG_MZQL2960HCJR_S64FNE0R100001-part5"
BY_ID_SECONDARY = "/dev/disk/by-id/nvme-SAMSUNG_MZQL2960HCJR_S64FNE0R100002-part5"
RESOLVED: dict[str, str | None] = {
    PRIMARY: PRIMARY,
    SECONDARY: SECONDARY,
    BY_ID_PRIMARY: PRIMARY,
    BY_ID_SECONDARY: SECONDARY,
}
"""What ``realpath`` prints on the host for each path, kernel names unchanged."""
LOKI = "/var/lib/loki-cloudfall"
PROMETHEUS = "/var/lib/prometheus-cloudfall"


def _servers(project: Path = EXAMPLES) -> tuple[object, ...]:
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    return requirements_of(inventory)


def _by_id_project(tmp_path: Path, secondary: str = BY_ID_SECONDARY) -> Path:
    """Copy the examples with the pool named by stable ``/dev/disk/by-id`` paths."""
    project = copy_examples(tmp_path / "config")
    baseline = project / "baselines" / "debian-application.yaml"
    text = baseline.read_text(encoding="utf-8")
    assert f"primary: {PRIMARY}\n" in text
    assert f"secondary: {SECONDARY}\n" in text
    baseline.write_text(
        text.replace(f"primary: {PRIMARY}\n", f"primary: {BY_ID_PRIMARY}\n").replace(
            f"secondary: {SECONDARY}\n", f"secondary: {secondary}\n"
        ),
        encoding="utf-8",
    )
    return project


def _lvm(rows: dict[str, list[dict[str, str]]]) -> str:
    return json.dumps({"report": [rows]})


def _devices(resolved: dict[str, str | None]) -> dict[str, dict[str, str | None]]:
    """Return the report's ``devices``: what ``realpath -e`` made of each path."""
    return {
        path: {
            "device": device,
            "error": (
                None
                if device is not None
                else f"realpath: {path}: No such file or directory"
            ),
        }
        for path, device in resolved.items()
    }


def _raw(  # noqa: PLR0913 - one fixture, every knob a test turns
    *,
    free: int = 100 * GIB,
    lvs: list[dict[str, str]] | None = None,
    filesystems: dict[str, str | None] | None = None,
    paths: dict[str, dict[str, object]] | None = None,
    groups: tuple[str, ...] = ("vg_data",),
    primary_group: str = "vg_data",
    resolved: dict[str, str | None] | None = None,
    primary_pv: str = PRIMARY,
) -> dict[str, object]:
    return {
        "vgs": _lvm({"vg": [{"vg_name": name} for name in groups]}),
        "pvs": _lvm(
            {
                "pv": [
                    {
                        "pv_name": primary_pv,
                        "vg_name": primary_group,
                        "pv_free": str(free),
                    },
                    {"pv_name": SECONDARY, "vg_name": "vg_data", "pv_free": str(free)},
                ]
            }
        ),
        "lvs": _lvm({"lv": lvs or []}),
        "filesystems": filesystems or {},
        "paths": paths
        or {
            LOKI: {"mountedFrom": None, "empty": True},
            PROMETHEUS: {"mountedFrom": None, "empty": True},
        },
        "devices": _devices(RESOLVED if resolved is None else resolved),
    }


def _linear(name: str, size: int, device: str = PRIMARY) -> dict[str, str]:
    return {
        "lv_name": name,
        "vg_name": "vg_data",
        "segtype": "linear",
        "lv_size": str(size),
        "devices": f"{device}(0)",
        "lv_dm_path": f"/dev/mapper/vg_data-{name}",
    }


def _plan(
    raw: dict[str, object], project: Path = EXAMPLES
) -> dict[str, VolumeAction]:
    servers = _servers(project)
    planned = plan_volumes(servers, {"h1": normalize_report(raw)})  # type: ignore[arg-type]
    return {item.requirement.name.value: item.action for item in planned}


def _refusal(raw: dict[str, object], project: Path = EXAMPLES) -> str:
    with pytest.raises(VolumeRefusedError) as error:
        _plan(raw, project)
    assert error.value.server == "h1"
    assert error.value.pool == "data"
    return error.value.reason


def test_the_backend_reads_its_two_volumes() -> None:
    assert volume_requests(_servers()) == {  # type: ignore[arg-type]
        "h1": [
            {
                "name": "loki",
                "volumeGroup": "vg_data",
                "logicalVolume": "lv_loki",
                "path": LOKI,
                "physicalVolumes": [PRIMARY, SECONDARY],
            },
            {
                "name": "prometheus",
                "volumeGroup": "vg_data",
                "logicalVolume": "lv_prometheus",
                "path": PROMETHEUS,
                "physicalVolumes": [PRIMARY, SECONDARY],
            },
        ]
    }


def test_the_host_resolves_the_pools_stable_paths(tmp_path: Path) -> None:
    requests = volume_requests(_servers(_by_id_project(tmp_path)))  # type: ignore[arg-type]
    assert [item["physicalVolumes"] for item in requests["h1"]] == [
        [BY_ID_PRIMARY, BY_ID_SECONDARY],
        [BY_ID_PRIMARY, BY_ID_SECONDARY],
    ]


def test_s014_4_a_by_id_pool_creates_on_the_device_lvm_reports(tmp_path: Path) -> None:
    """#293: a stable path resolves on the host; lvcreate names LVM's own PV name."""
    servers = _servers(_by_id_project(tmp_path))
    planned = plan_volumes(servers, {"h1": normalize_report(_raw())})  # type: ignore[arg-type]

    actions = actions_by_server(planned)["h1"]
    assert [item["action"] for item in actions] == ["create", "create"]
    assert [item["physicalVolumes"] for item in actions] == [[PRIMARY], [PRIMARY]]


def test_s014_5_a_by_id_pool_keeps_and_grows_on_the_same_device(
    tmp_path: Path,
) -> None:
    project = _by_id_project(tmp_path)
    raw = _raw(
        lvs=[_linear("lv_loki", 10 * GIB), _linear("lv_prometheus", 4 * GIB)],
        filesystems={"lv_loki": "ext4", "lv_prometheus": "ext4"},
    )

    assert _plan(raw, project) == {
        "loki": VolumeAction.KEEP,
        "prometheus": VolumeAction.GROW,
    }
    planned = plan_volumes(_servers(project), {"h1": normalize_report(raw)})  # type: ignore[arg-type]
    (grow,) = [
        item for item in actions_by_server(planned)["h1"] if item["action"] == "grow"
    ]
    assert grow["physicalVolumes"] == [PRIMARY]


def test_a_physical_volume_lvm_names_by_another_link_still_matches(
    tmp_path: Path,
) -> None:
    """#293: realpath follows every link, so LVM's /dev/mapper name matches too."""
    mapper = "/dev/mapper/crypt-data0"
    resolved = {**RESOLVED, BY_ID_PRIMARY: "/dev/dm-0", mapper: "/dev/dm-0"}
    project = _by_id_project(tmp_path)
    raw = _raw(resolved=resolved, primary_pv=mapper)
    planned = plan_volumes(_servers(project), {"h1": normalize_report(raw)})  # type: ignore[arg-type]
    assert [item["physicalVolumes"] for item in actions_by_server(planned)["h1"]] == [
        [mapper],
        [mapper],
    ]
    kept = _raw(
        resolved=resolved,
        primary_pv=mapper,
        lvs=[_linear("lv_loki", 10 * GIB, mapper)],
        filesystems={"lv_loki": "ext4"},
    )
    assert _plan(kept, project)["loki"] is VolumeAction.KEEP


def test_s014_7_a_by_id_volume_on_the_other_disk_is_refused(tmp_path: Path) -> None:
    lvs = [_linear("lv_loki", 10 * GIB, SECONDARY)]
    reason = _refusal(_raw(lvs=lvs), _by_id_project(tmp_path))
    assert "sits on" in reason
    assert BY_ID_PRIMARY in reason


def test_s014_7_a_pool_path_missing_on_the_host_is_refused(tmp_path: Path) -> None:
    """#293: a path the host cannot resolve is named, never a silent mismatch."""
    resolved = {**RESOLVED, BY_ID_PRIMARY: None}
    reason = _refusal(_raw(resolved=resolved), _by_id_project(tmp_path))
    assert f"physical volume {BY_ID_PRIMARY} does not resolve" in reason
    assert "No such file or directory" in reason


def test_s014_7_a_kernel_name_missing_on_the_host_is_refused() -> None:
    resolved = {**RESOLVED, PRIMARY: None}
    reason = _refusal(_raw(resolved=resolved))
    assert f"physical volume {PRIMARY} does not resolve" in reason


def test_s014_4_a_missing_secondary_does_not_block_a_linear_volume() -> None:
    """Kernel names behave as before: a linear volume never reads the secondary."""
    resolved = {**RESOLVED, SECONDARY: None}
    assert _plan(_raw(resolved=resolved)) == {
        "loki": VolumeAction.CREATE,
        "prometheus": VolumeAction.CREATE,
    }


def test_s014_7_a_pool_whose_paths_name_one_device_is_refused(tmp_path: Path) -> None:
    """#293: two different stable paths may name one partition."""
    other = "/dev/disk/by-partuuid/0f6c2a5e-6b1d-4f1e-9a51-1c2d3e4f5a6b"
    resolved = {**RESOLVED, other: PRIMARY}
    reason = _refusal(_raw(resolved=resolved), _by_id_project(tmp_path, other))
    assert f"{BY_ID_PRIMARY} and {other} both resolve to {PRIMARY}" in reason


def test_s014_7_a_by_id_path_on_a_device_outside_the_group_is_refused(
    tmp_path: Path,
) -> None:
    reason = _refusal(_raw(primary_group="vg_other"), _by_id_project(tmp_path))
    assert f"physical volume {BY_ID_PRIMARY} ({PRIMARY}) is not in vg_data" in reason


def test_s014_4_missing_volumes_are_created_linear_on_primary() -> None:
    servers = _servers()
    planned = plan_volumes(servers, {"h1": normalize_report(_raw())})  # type: ignore[arg-type]

    actions = actions_by_server(planned)["h1"]
    assert [item["action"] for item in actions] == ["create", "create"]
    assert actions[0] == {
        "name": "loki",
        "action": "create",
        "volumeGroup": "vg_data",
        "logicalVolume": "lv_loki",
        "type": "linear",
        "physicalVolumes": [PRIMARY],
        "sizeBytes": 10 * GIB,
        "path": LOKI,
        "label": "loki",
        "units": ["loki.service"],
    }


def test_s014_5_an_existing_volume_is_kept_grown_but_never_shrunk() -> None:
    raw = _raw(
        lvs=[_linear("lv_loki", 10 * GIB), _linear("lv_prometheus", 4 * GIB)],
        filesystems={"lv_loki": "ext4", "lv_prometheus": "ext4"},
        paths={
            LOKI: {"mountedFrom": "/dev/mapper/vg_data-lv_loki", "empty": False},
            PROMETHEUS: {
                "mountedFrom": "/dev/mapper/vg_data-lv_prometheus",
                "empty": False,
            },
        },
    )

    assert _plan(raw) == {"loki": VolumeAction.KEEP, "prometheus": VolumeAction.GROW}
    bigger = _raw(
        lvs=[_linear("lv_loki", 50 * GIB), _linear("lv_prometheus", 10 * GIB)],
        filesystems={"lv_loki": "ext4", "lv_prometheus": "ext4"},
    )
    assert _plan(bigger) == {"loki": VolumeAction.KEEP, "prometheus": VolumeAction.KEEP}


def test_s014_6_a_raid1_volume_reads_its_disks_through_hidden_images() -> None:
    raw = _raw(
        lvs=[
            {
                "lv_name": "lv_garage",
                "vg_name": "vg_data",
                "segtype": "raid1",
                "lv_size": str(GIB),
                "devices": "lv_garage_rimage_0(0),lv_garage_rimage_1(0)",
                "lv_dm_path": "/dev/mapper/vg_data-lv_garage",
            },
            _linear("[lv_garage_rimage_0]", GIB, PRIMARY),
            _linear("[lv_garage_rimage_1]", GIB, SECONDARY),
        ]
    )

    facts = normalize_report(raw)

    garage = facts["logicalVolumes"]["vg_data/lv_garage"]  # type: ignore[index]
    assert garage["type"] == "raid1"
    assert garage["devices"] == [PRIMARY, SECONDARY]
    assert "vg_data/[lv_garage_rimage_0]" not in facts["logicalVolumes"]  # type: ignore[operator]


def test_s014_7_a_missing_volume_group_is_refused() -> None:
    assert "volume group vg_data does not exist" in _refusal(_raw(groups=("vg_other",)))


def test_s014_7_a_physical_volume_outside_the_group_is_refused() -> None:
    assert f"{PRIMARY} is not in vg_data" in _refusal(_raw(primary_group="vg_other"))


def test_s014_7_too_little_free_space_is_refused() -> None:
    assert "bytes free" in _refusal(_raw(free=GIB))


def test_s014_7_a_volume_of_another_type_is_refused() -> None:
    lvs = [{**_linear("lv_loki", 10 * GIB), "segtype": "raid1"}]
    assert "is raid1, but the requirement wants linear" in _refusal(_raw(lvs=lvs))


def test_s014_7_a_volume_on_other_disks_is_refused() -> None:
    lvs = [_linear("lv_loki", 10 * GIB, SECONDARY)]
    assert "sits on" in _refusal(_raw(lvs=lvs))


def test_s014_7_a_volume_with_another_filesystem_is_refused() -> None:
    lvs = [_linear("lv_loki", 10 * GIB)]
    raw = _raw(lvs=lvs, filesystems={"lv_loki": "xfs"})
    assert "holds xfs, not ext4" in _refusal(raw)


def test_s014_7_a_volume_without_a_filesystem_is_refused() -> None:
    # A run that stopped between lvcreate and mkfs leaves a bare LV; the next
    # run must refuse it up front, never mount or grow it, and never format it
    lvs = [_linear("lv_loki", 10 * GIB)]
    raw = _raw(lvs=lvs, filesystems={"lv_loki": None})
    assert "has no filesystem" in _refusal(raw)


def test_s014_7_a_same_named_volume_in_another_group_is_not_this_one() -> None:
    other = {**_linear("lv_loki", 4 * GIB, "/dev/sdz1"), "vg_name": "vg_old"}
    raw = _raw(groups=("vg_data", "vg_old"), lvs=[other])
    assert _plan(raw) == {
        "loki": VolumeAction.CREATE,
        "prometheus": VolumeAction.CREATE,
    }


def test_s014_7_a_path_with_files_is_never_mounted_over() -> None:
    raw = _raw(
        paths={
            LOKI: {"mountedFrom": None, "empty": False},
            PROMETHEUS: {"mountedFrom": None, "empty": True},
        }
    )
    assert "would hide them" in _refusal(raw)


def test_s014_7_a_path_mounted_from_another_device_is_refused() -> None:
    raw = _raw(
        paths={
            LOKI: {"mountedFrom": "/dev/sdz1", "empty": False},
            PROMETHEUS: {"mountedFrom": None, "empty": True},
        }
    )
    assert "already mounted from /dev/sdz1" in _refusal(raw)


ANSIBLE = ROOT / "engine" / "ansible"
VOLUMES_ROLE_TASKS = ANSIBLE / "roles" / "cloudfall_volumes" / "tasks" / "main.yml"
_NEVER_RUN = ("pvcreate", "vgcreate", "vgextend", "lvremove", "lvreduce", "wipefs")
_LVM_MODULES = ("lvg", "lvol", "filesystem", "parted")
"""Modules that would make, change or format LVM structure behind the role's back."""
_FORBIDDEN = (
    *_NEVER_RUN,
    *(f"{module}:" for module in _LVM_MODULES),
    *(f"community.general.{module}" for module in _LVM_MODULES),
)


def _ansible_sources() -> list[Path]:
    return sorted(
        path
        for path in ANSIBLE.rglob("*")
        if path.is_file() and path.suffix in {".yml", ".yaml", ".j2", ".sh", ".py"}
    )


def test_s014_9_no_playbook_or_role_runs_a_destructive_lvm_command() -> None:
    """S-014-9: the engine never makes or removes LVM structure, nor wipes a disk."""
    sources = _ansible_sources()
    assert VOLUMES_ROLE_TASKS in sources
    assert ANSIBLE / "playbooks" / "logging-volumes.yml" in sources
    offenders = [
        (path.relative_to(ROOT).as_posix(), word)
        for path in sources
        for word in _FORBIDDEN
        if re.search(rf"(?<![\w.-]){re.escape(word)}", path.read_text(encoding="utf-8"))
        # Loki's own config template has a ``filesystem:`` storage key.
        and not (word == "filesystem:" and path.suffix == ".j2")
    ]
    assert offenders == []


def test_s014_9_mkfs_runs_only_on_a_volume_created_in_the_same_run() -> None:
    """S-014-9: one mkfs, over exactly the volumes the run's lvcreate made."""
    sources = _ansible_sources()
    with_mkfs = [path for path in sources if "mkfs" in path.read_text(encoding="utf-8")]
    assert with_mkfs == [VOLUMES_ROLE_TASKS]
    tasks = yaml.safe_load(VOLUMES_ROLE_TASKS.read_text(encoding="utf-8"))
    (create,) = [task for task in tasks if "lvcreate" in str(task)]
    (mkfs,) = [task for task in tasks if "mkfs" in str(task)]
    created = (
        "{{ cloudfall_volumes_actions | selectattr('action', 'equalto', 'create') "
        "| list }}"
    )
    assert create["loop"] == mkfs["loop"] == created
    assert tasks.index(create) < tasks.index(mkfs)
    # The controller asks for ``create`` only where no such LV exists: an LV
    # it finds is kept or grown (S-014-5), a bare one is refused (S-014-7).
    lvs = [_linear("lv_loki", 10 * GIB), _linear("lv_prometheus", 10 * GIB)]
    raw = _raw(lvs=lvs, filesystems={"lv_loki": "ext4", "lv_prometheus": "ext4"})
    assert VolumeAction.CREATE not in _plan(raw).values()


COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"


def _volume_check(
    tmp_path: Path, edit: object, project: Path = EXAMPLES
) -> tuple[str, object]:
    import shutil  # noqa: PLC0415 - only these audit tests copy fixtures

    from cloudfall.audit import audit_inventory  # noqa: PLC0415
    from cloudfall.observation import load_observations  # noqa: PLC0415

    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    document = json.loads(h1.read_text(encoding="utf-8"))
    edit(document["spec"]["volumes"])  # type: ignore[operator]
    h1.write_text(json.dumps(document), encoding="utf-8")
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    server = next(item for item in report.servers if item.server_id == "h1")
    check = next(item for item in server.checks if item.check == "volume.loki")
    return check.status.value, check.observed


def _lvs(evidence: dict[str, object], **changes: str) -> None:
    report = json.loads(str(evidence["lvsJson"]))
    for row in report["report"][0]["lv"]:
        if row["lv_name"] == "lv_loki":
            row.update(changes)
    evidence["lvsJson"] = json.dumps(report)


def test_s014_8_the_compliant_fixture_audits_both_volumes_clean(
    tmp_path: Path,
) -> None:
    assert _volume_check(tmp_path, lambda _evidence: None)[0] == "compliant"


@pytest.mark.parametrize(
    "edit",
    [
        lambda evidence: evidence["mounts"].update({LOKI: None}),
        lambda evidence: _lvs(evidence, lv_size=str(GIB)),
        lambda evidence: _lvs(evidence, segtype="raid1"),
        lambda evidence: _lvs(evidence, lv_health_status="partial"),
        lambda evidence: _lvs(evidence, devices=f"{SECONDARY}(0)"),
    ],
    ids=["unmounted", "too-small", "wrong-type", "degraded", "other-disk"],
)
def test_s014_8_volume_drift(tmp_path: Path, edit: object) -> None:
    assert _volume_check(tmp_path, edit)[0] == "drift"


def test_s014_8_a_missing_volume_is_drift(tmp_path: Path) -> None:
    def remove(evidence: dict[str, object]) -> None:
        report = json.loads(str(evidence["lvsJson"]))
        report["report"][0]["lv"] = [
            row for row in report["report"][0]["lv"] if row["lv_name"] != "lv_loki"
        ]
        evidence["lvsJson"] = json.dumps(report)

    assert _volume_check(tmp_path, remove) == ("drift", {"present": False})


def test_s014_8_a_path_inspect_did_not_read_is_unknown(tmp_path: Path) -> None:
    """Q-1: no findmnt read for the path is not proof the volume is unmounted."""
    status, _observed = _volume_check(
        tmp_path, lambda evidence: evidence["mounts"].pop(LOKI)
    )
    assert status == "unknown"


def _resolved(
    resolved: dict[str, str | None],
) -> object:
    def edit(evidence: dict[str, object]) -> None:
        evidence["devices"] = _devices(resolved)

    return edit


def test_s014_8_a_by_id_pool_audits_clean_and_shows_its_device(
    tmp_path: Path,
) -> None:
    """#293: audit compares the device each declared path resolved to."""
    project = _by_id_project(tmp_path / "project")
    status, observed = _volume_check(tmp_path, _resolved(RESOLVED), project)
    assert status == "compliant"
    assert observed["resolvedPhysicalVolumes"] == {BY_ID_PRIMARY: PRIMARY}  # type: ignore[index]
    assert observed["physicalVolumes"] == [PRIMARY]  # type: ignore[index]


def test_s014_8_a_kernel_name_pool_with_resolved_devices_audits_clean(
    tmp_path: Path,
) -> None:
    assert _volume_check(tmp_path, _resolved(RESOLVED))[0] == "compliant"


@pytest.mark.parametrize(
    "resolved",
    [
        {**RESOLVED, BY_ID_PRIMARY: SECONDARY, BY_ID_SECONDARY: PRIMARY},
        {**RESOLVED, BY_ID_PRIMARY: None},
    ],
    ids=["swapped", "missing"],
)
def test_s014_8_a_by_id_pool_on_another_or_no_device_is_drift(
    tmp_path: Path, resolved: dict[str, str | None]
) -> None:
    project = _by_id_project(tmp_path / "project")
    assert _volume_check(tmp_path, _resolved(resolved), project)[0] == "drift"


def test_s014_8_unmounted_without_a_mapper_path_is_drift(tmp_path: Path) -> None:
    """Q-1: a missing ``lv_dm_path`` must not let an unmounted LV match None."""

    def edit(evidence: dict[str, object]) -> None:
        _lvs(evidence, lv_dm_path="")
        evidence["mounts"].update({LOKI: None})  # type: ignore[attr-defined]

    assert _volume_check(tmp_path, edit)[0] == "drift"
