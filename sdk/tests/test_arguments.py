"""Strict argument parsing tests for the command-line entry points."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cloudfall.cli import main

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
SECRET = "postgresql://migrator:hunter2@db.example.test/crm"  # noqa: S105 - fake


def test_abbreviated_secret_file_flag_is_rejected_without_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _usage_error(
        [
            "data",
            "migrate",
            "postgresql-main",
            "--project",
            str(EXAMPLES),
            "--database",
            "crm",
            "--source-url",
            SECRET,
        ],
        capsys,
    )

    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "ARG_ERROR"
    assert "--source-url" in json.dumps(error)
    assert "hunter2" not in json.dumps(payload)


def _usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    """Run a command: a usage error is exit 2, as an envelope on stdout."""
    assert main(argv) == 2
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    assert payload["ok"] is False
    return payload


def test_unrecognized_option_values_are_not_echoed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _usage_error(
        [
            "config",
            "validate",
            "--project",
            str(EXAMPLES),
            "--token",
            "sk-live-secret",
            "--debug=verbose-secret",
        ],
        capsys,
    )

    message = json.dumps(payload["error"])
    assert "--token" in message
    assert "--debug" in message
    assert "secret" not in message


def test_missing_required_argument_is_a_json_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _usage_error(
        ["deploy", "crm-backend", "--project", str(EXAMPLES)], capsys
    )

    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "ARG_ERROR"
    assert "release" in json.dumps(error)


@pytest.mark.parametrize(
    ("argv", "field"),
    [
        (["operator", "show", "../ghost"], "proposal"),
        (["health", "Bad ID!"], "component"),
        (["backup", "run", "acme%2Fdb"], "service"),
        (["rollback", "crm-backend", "--release", "r1"], "release"),
    ],
)
def test_an_invalid_identifier_fails_before_the_command_runs(
    argv: list[str], field: str, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _usage_error([*argv, "--project", str(EXAMPLES)], capsys)

    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "ARG_ERROR"
    assert field in str(error["message"])


def test_import_render_rejects_an_invalid_application_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "render.yaml"
    blueprint.write_text("services: []\n", encoding="utf-8")

    payload = _usage_error(
        [
            "import",
            "render",
            str(blueprint),
            "--project",
            str(EXAMPLES),
            "--application",
            "acme%2Fx",
            "--server",
            "h1",
        ],
        capsys,
    )

    assert "application" in json.dumps(payload["error"])


def test_relative_output_leaving_the_project_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observed = tmp_path / "observed"
    observed.mkdir()

    payload = _usage_error(
        [
            "dashboard",
            "build",
            "--project",
            str(EXAMPLES),
            "--observed",
            str(observed),
            "--output-dir",
            "tmp/../../escape",
        ],
        capsys,
    )

    assert "escapes its base directory" in json.dumps(payload["error"])
    assert not (EXAMPLES.parent / "escape").exists()


def test_absolute_output_outside_the_project_is_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observed = tmp_path / "observed"
    observed.mkdir()
    output = tmp_path / "dashboard"

    exit_code = main(
        [
            "dashboard",
            "build",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--observed",
            str(observed),
            "--output-dir",
            str(output),
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "ok"
    assert (output / "index.html").is_file()
