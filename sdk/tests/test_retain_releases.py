"""A component's ``deployment.retainReleases`` (#224, D-15).

How many releases a deploy keeps on each server: an integer, at least 2,
5 when absent. The deploy role's cleanup is tested in the engine's
``test_deploy_rollback.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cloudfall.cli import main
from cloudfall.domain import RetainReleases
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
DECLARED = "    retainUntilCleanup: true\n"


def _project(tmp_path: Path, retain: object | None) -> Path:
    """Copy the examples with crm-backend's ``retainReleases`` set, or not."""
    project = tmp_path / "config"
    copy_examples(project)
    path = project / "components" / "crm-backend.yaml"
    text = path.read_text(encoding="utf-8")
    assert DECLARED in text
    if retain is not None:
        declared = f"{DECLARED}    retainReleases: {json.dumps(retain)}\n"
        path.write_text(text.replace(DECLARED, declared), encoding="utf-8")
    return project


def _validate(project: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    exit_code = main(
        ["config", "validate", "--project", str(project), "--schemas", str(SCHEMAS)]
    )
    return exit_code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("retain", [2, 5, 40])
def test_config_validate_accepts_a_count_of_at_least_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], retain: int
) -> None:
    exit_code, payload = _validate(_project(tmp_path, retain), capsys)

    assert exit_code == 0, payload


@pytest.mark.parametrize(
    "retain",
    [1, 0, -3, 2.5, "5", True],
    ids=["1", "0", "negative", "float", "string", "boolean"],
)
def test_config_validate_refuses_a_count_below_two_or_not_an_integer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], retain: object
) -> None:
    exit_code, payload = _validate(_project(tmp_path, retain), capsys)

    assert exit_code == 80, payload
    assert payload["error"]["context"]["code"] == "schema_validation_failed"


@pytest.mark.parametrize(("retain", "expected"), [(None, 5), (2, 2), (9, 9)])
def test_the_inventory_carries_the_count_five_when_absent(
    tmp_path: Path, retain: int | None, expected: int
) -> None:
    inventory = PlatformInventory.from_state(
        validate_config(_project(tmp_path, retain), SCHEMAS)
    )
    component = inventory.components[0]

    assert component.retain_releases == RetainReleases(expected)
    assert component.as_dict()["retainReleases"] == expected


@pytest.mark.parametrize("value", [1, 0, True, 2.0, "5"])
def test_the_value_object_refuses_what_the_schema_refuses(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        RetainReleases.from_boundary(value)


def test_the_value_object_defaults_to_five() -> None:
    assert RetainReleases.from_boundary(None) == RetainReleases(5)
