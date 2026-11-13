"""S-014: the controller decides each volume's action, or refuses before any change."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
GIB = 1024**3
PRIMARY = "/dev/nvme0n1p5"
SECONDARY = "/dev/nvme1n1p5"
LOKI = "/var/lib/loki-cloudfall"
PROMETHEUS = "/var/lib/prometheus-cloudfall"


def _servers() -> tuple[object, ...]:
    inventory = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))
    return requirements_of(inventory)


def _lvm(rows: dict[str, list[dict[str, str]]]) -> str:
    return json.dumps({"report": [rows]})


def _raw(  # noqa: PLR0913 - one fixture, every knob a test turns
    *,
    free: int = 100 * GIB,
    lvs: list[dict[str, str]] | None = None,
    filesystems: dict[str, str | None] | None = None,
    paths: dict[str, dict[str, object]] | None = None,
    groups: tuple[str, ...] = ("vg_data",),
    primary_group: str = "vg_data",
) -> dict[str, object]:
    return {
        "vgs": _lvm({"vg": [{"vg_name": name} for name in groups]}),
        "pvs": _lvm(
            {
                "pv": [
                    {
                        "pv_name": PRIMARY,
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


def _plan(raw: dict[str, object]) -> dict[str, VolumeAction]:
    servers = _servers()
    planned = plan_volumes(servers, {"h1": normalize_report(raw)})  # type: ignore[arg-type]
    return {item.requirement.name.value: item.action for item in planned}


def _refusal(raw: dict[str, object]) -> str:
    with pytest.raises(VolumeRefusedError) as error:
        _plan(raw)
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
            },
            {
                "name": "prometheus",
                "volumeGroup": "vg_data",
                "logicalVolume": "lv_prometheus",
                "path": PROMETHEUS,
            },
        ]
    }


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

    garage = facts["logicalVolumes"]["lv_garage"]  # type: ignore[index]
    assert garage["type"] == "raid1"
    assert garage["devices"] == [PRIMARY, SECONDARY]
    assert "[lv_garage_rimage_0]" not in facts["logicalVolumes"]  # type: ignore[operator]


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
