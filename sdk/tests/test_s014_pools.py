"""S-014: storage pools on a Baseline, and the volumes that go into them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.domain import (
    AbsolutePath,
    BlockDevicePath,
    ByteSize,
    ResourceId,
    SystemdUnitName,
    VolumeGroupName,
)
from cloudfall.inventory import StoragePool
from cloudfall.requirements import RequirementName, VolumePlacement, VolumeRequirement
from cloudfall.validation import ConfigValidationError, validate_config
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
POOLS = (
    "    pools:\n"
    "      - id: data\n"
    "        volumeGroup: vg_data\n"
    "        primary: /dev/nvme0n1p5\n"
    "        secondary: /dev/nvme1n1p5\n"
)


def _project(tmp_path: Path, pools: str = POOLS, stack: str | None = None) -> Path:
    project = copy_examples(tmp_path / "config")
    baseline = project / "baselines" / "debian-application.yaml"
    text = baseline.read_text(encoding="utf-8")
    assert POOLS in text
    baseline.write_text(text.replace(POOLS, pools), encoding="utf-8")
    if stack is not None:
        path = project / "logging-stacks" / "operations.yaml"
        old, new = stack.split("=>")
        content = path.read_text(encoding="utf-8")
        assert old in content
        path.write_text(content.replace(old, new), encoding="utf-8")
    return project


def _code(project: Path) -> str:
    with pytest.raises(ConfigValidationError) as error:
        validate_config(project, SCHEMAS)
    return error.value.issue.code


def test_s014_1_a_baseline_with_pools_validates() -> None:
    validate_config(EXAMPLES, SCHEMAS)


@pytest.mark.parametrize(
    "pools",
    [
        POOLS.replace("/dev/nvme0n1p5", "nvme0n1p5"),
        POOLS.replace("/dev/nvme1n1p5", "/srv/disk"),
        POOLS + "      - id: more\n        volumeGroup: vg_data\n"
        "        primary: /dev/sdb1\n",
        POOLS.replace("/dev/nvme0n1p5", "/dev/../etc/passwd"),
        POOLS.replace("vg_data", "'..'"),
        POOLS.replace("/dev/nvme1n1p5", "/dev/nvme0n1p5"),
    ],
)
def test_s014_1_malformed_pools_are_refused(tmp_path: Path, pools: str) -> None:
    project = _project(tmp_path, pools)

    with pytest.raises(ConfigValidationError):
        validate_config(project, SCHEMAS)


@pytest.mark.parametrize("name", [".", ".."])
def test_volume_group_names_refuse_dot_names(name: str) -> None:
    with pytest.raises(ValueError, match="volume group"):
        VolumeGroupName(name)


def test_s014_2_a_backend_baseline_without_pools_is_refused(tmp_path: Path) -> None:
    assert _code(_project(tmp_path, "")) == "volume_requirement_pool_missing"


def test_s014_2_an_unknown_pool_is_refused(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        stack="      type: filesystem\n=>      type: filesystem\n      pool: fast\n",
    )

    assert _code(project) == "volume_requirement_pool_unknown"


def test_s014_2_two_pools_need_a_name(tmp_path: Path) -> None:
    two = POOLS + (
        "      - id: fast\n        volumeGroup: vg_fast\n        primary: /dev/sdb1\n"
    )

    assert _code(_project(tmp_path, two)) == "volume_requirement_pool_ambiguous"


def test_s014_3_requirements_show_names_pool_lv_and_type(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["requirements", "show", "--project", str(EXAMPLES)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    volumes = {
        item["name"]: item["spec"]
        for item in payload["data"]["servers"][0]["requirements"]
        if item["type"] == "volume"
    }
    assert {name: spec["logical_volume"] for name, spec in volumes.items()} == {
        "loki": "lv_loki",
        "prometheus": "lv_prometheus",
    }
    assert volumes["loki"]["pool"] == "data"
    assert volumes["loki"]["volume_group"] == "vg_data"
    assert volumes["loki"]["type"] == "linear"
    assert volumes["loki"]["physical_volumes"] == ["/dev/nvme0n1p5"]


# proves: S-014-6
def test_a_mirrored_volume_spans_both_physical_volumes() -> None:
    pool = StoragePool(
        ResourceId("data"),
        VolumeGroupName("vg_data"),
        BlockDevicePath("/dev/nvme0n1p5"),
        BlockDevicePath("/dev/nvme1n1p5"),
    )
    volume = VolumeRequirement(
        RequirementName("garage"),
        AbsolutePath("/srv/storage/garage"),
        ByteSize(1),
        VolumePlacement.MIRRORED,
        (SystemdUnitName("garage.service"),),
        pool,
    )

    assert volume.volume_type == "raid1"
    assert [item.value for item in volume.physical_volumes] == [
        "/dev/nvme0n1p5",
        "/dev/nvme1n1p5",
    ]


# proves: S-014-7
def test_a_mirrored_volume_needs_a_secondary() -> None:
    pool = StoragePool(
        ResourceId("data"),
        VolumeGroupName("vg_data"),
        BlockDevicePath("/dev/nvme0n1p5"),
        None,
    )

    with pytest.raises(ValueError, match="no secondary"):
        VolumeRequirement(
            RequirementName("garage"),
            AbsolutePath("/srv/storage/garage"),
            ByteSize(1),
            VolumePlacement.MIRRORED,
            (SystemdUnitName("garage.service"),),
            pool,
        )


def test_device_paths_refuse_parent_steps() -> None:
    with pytest.raises(ValueError, match="block device"):
        BlockDevicePath("/dev/../etc/passwd")
