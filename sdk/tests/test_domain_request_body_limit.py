"""A Domain's request body limit: `spec.proxy.maxRequestBodyMegabytes` (#600)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cloudfall.cli import main
from cloudfall.domain import RequestBodyMegabytes
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"

_UPSTREAM = "    upstream:\n      address: 127.0.0.1\n      port: 8100\n"


def _project(tmp_path: Path, value: str) -> Path:
    """Copy the examples and give their Domain ``maxRequestBodyMegabytes``."""
    project = tmp_path / "config"
    copy_examples(project)
    path = project / "domains" / "crm-site.yaml"
    text = path.read_text(encoding="utf-8")
    assert _UPSTREAM in text
    text = text.replace(
        _UPSTREAM, f"{_UPSTREAM}    maxRequestBodyMegabytes: {value}\n"
    )
    path.write_text(text, encoding="utf-8")
    return project


def _validate(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    exit_code = main(
        [
            "config",
            "validate",
            "--project",
            str(project),
            "--schemas",
            str(SCHEMAS),
        ]
    )
    return exit_code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("megabytes", [1, 55, 10240])
def test_a_body_limit_from_1_to_10240_passes_and_serializes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], megabytes: int
) -> None:
    project = _project(tmp_path, str(megabytes))

    exit_code, payload = _validate(project, capsys)

    assert exit_code == 0
    assert payload["data"]["status"] == "ok"
    domain = PlatformInventory.from_state(validate_config(project, SCHEMAS)).domains[0]
    assert domain.proxy.max_request_body == RequestBodyMegabytes(megabytes)
    proxy: dict[str, Any] = domain.as_dict()["proxy"]  # type: ignore[assignment]
    assert proxy["maxRequestBodyMegabytes"] == megabytes


@pytest.mark.parametrize("value", ["0", "-1", "10241", '"55m"', "55.5", "true"])
def test_a_body_limit_outside_the_schema_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    exit_code, payload = _validate(_project(tmp_path, value), capsys)

    assert exit_code == 80
    assert payload["error"]["code"] == "CONFIG_INVALID"
    context = payload["error"]["context"]
    assert context["code"] == "schema_validation_failed"
    assert context["field"] == "spec.proxy.maxRequestBodyMegabytes"


def test_a_domain_without_a_body_limit_serializes_as_before() -> None:
    domain = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS)).domains[0]

    assert domain.proxy.max_request_body is None
    proxy: dict[str, Any] = domain.as_dict()["proxy"]  # type: ignore[assignment]
    assert "maxRequestBodyMegabytes" not in proxy


def test_the_value_object_matches_the_schema() -> None:
    assert RequestBodyMegabytes.from_boundary(1).value == 1
    assert RequestBodyMegabytes.from_boundary(10240).value == 10240
    for value in (0, -1, 10241):
        with pytest.raises(ValueError, match="request body limit must be 1 to 10240"):
            RequestBodyMegabytes(value)
    for raw in (True, "55m", 55.0, None):
        with pytest.raises(TypeError, match="request body limit must be an integer"):
            RequestBodyMegabytes.from_boundary(raw)
