"""An integer field refuses a whole-number float at `config validate` (#602)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cloudfall.cli import main
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"

_UPSTREAM = "    upstream:\n      address: 127.0.0.1\n      port: 8100\n"


def _project(tmp_path: Path, upstream: str) -> Path:
    """Copy the examples and replace their Domain's upstream block."""
    project = tmp_path / "config"
    copy_examples(project)
    path = project / "domains" / "crm-site.yaml"
    text = path.read_text(encoding="utf-8")
    assert _UPSTREAM in text
    path.write_text(text.replace(_UPSTREAM, upstream), encoding="utf-8")
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


def _port(value: str) -> str:
    return f"    upstream:\n      address: 127.0.0.1\n      port: {value}\n"


def _body_limit(value: str) -> str:
    return f"{_UPSTREAM}    maxRequestBodyMegabytes: {value}\n"


@pytest.mark.parametrize(
    ("upstream", "field"),
    [
        (_port("8100.0"), "spec.proxy.upstream.port"),
        (_port("true"), "spec.proxy.upstream.port"),
        (_body_limit("55.0"), "spec.proxy.maxRequestBodyMegabytes"),
    ],
    ids=["port-float", "port-bool", "body-limit-float"],
)
def test_a_float_or_bool_in_an_integer_field_is_refused_with_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], upstream: str, field: str
) -> None:
    exit_code, payload = _validate(_project(tmp_path, upstream), capsys)

    assert exit_code == 80
    assert payload["error"]["code"] == "CONFIG_INVALID"
    context = payload["error"]["context"]
    assert context["code"] == "schema_validation_failed"
    assert context["field"] == field


@pytest.mark.parametrize(
    "upstream", [_port("8100"), _body_limit("55")], ids=["port", "body-limit"]
)
def test_a_plain_integer_still_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], upstream: str
) -> None:
    exit_code, payload = _validate(_project(tmp_path, upstream), capsys)

    assert exit_code == 0
    assert payload["data"]["status"] == "ok"
