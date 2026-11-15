"""The project format version: read, written by init, checked by config validate."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from cloudfall.app import project_format_unsupported
from cloudfall.cli import main
from cloudfall.project_format import (
    PROJECT_FORMAT_UNSUPPORTED_EXIT,
    SUPPORTED_FORMATS,
    FormatErrorCode,
    FormatRange,
    ProjectFormat,
    ProjectFormatError,
    check_project_format,
)
from examples_project import EXAMPLES, copy_examples

SCHEMAS = EXAMPLES.parents[1] / "config" / "schemas" / "v1"
CURRENT = SUPPORTED_FORMATS.current.value
CLOUDFALL = (str(Path(sys.executable).with_name("cloudfall")),)
ENGINE = (str(Path(sys.executable).with_name("cloudfall-engine")),)
FORMAT_WARNINGS = ("PROJECT_FORMAT_OLD", "PROJECT_FORMAT_UNSUPPORTED")


def _validate(
    capsys: pytest.CaptureFixture[str], project: Path
) -> tuple[int, dict[str, object]]:
    exit_code = main(
        ["config", "validate", "--project", str(project), "--schemas", str(SCHEMAS)]
    )
    return exit_code, json.loads(capsys.readouterr().out)


def _codes(payload: dict[str, object]) -> list[str]:
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    return [warning["code"] for warning in warnings]


def _with_pyproject(tmp_path: Path, content: str) -> Path:
    project = copy_examples(tmp_path / "project")
    (project / "pyproject.toml").write_text(content, encoding="utf-8")
    return project


def test_s017_2_init_writes_the_current_format_and_validate_warns_nothing_of_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "fleet"

    assert main(["init", str(directory)]) == 0
    capsys.readouterr()

    pyproject = tomllib.loads(
        (directory / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["tool"]["cloudfall"] == {"format": CURRENT}
    # Declare a server: the examples' resources, beside what init wrote.
    examples = copy_examples(tmp_path / "examples")
    shutil.copytree(examples, directory, dirs_exist_ok=True)
    exit_code, payload = _validate(capsys, directory)
    assert exit_code == 0, payload
    assert not set(_codes(payload)) & set(FORMAT_WARNINGS)


@pytest.mark.parametrize(
    "pyproject",
    [
        None,
        '[project]\nname = "fleet"\n\n[tool.uv]\npackage = false\n',
    ],
    ids=["no-pyproject", "no-tool-cloudfall-table"],
)
def test_s017_3_a_format_0_project_validates_with_one_old_format_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], pyproject: str | None
) -> None:
    project = copy_examples(tmp_path / "project")
    if pyproject is not None:
        (project / "pyproject.toml").write_text(pyproject, encoding="utf-8")

    exit_code, payload = _validate(capsys, project)

    assert exit_code == 0, payload
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    [warning] = [item for item in warnings if item["code"] == "PROJECT_FORMAT_OLD"]
    assert warning["context"] == {
        "projectFormat": 0,
        "current": CURRENT,
        "fix": "cloudfall config upgrade",
    }
    assert "format 0" in warning["message"]
    assert f"format {CURRENT}" in warning["message"]
    assert "cloudfall config upgrade" in warning["message"]


def test_s017_6_a_project_below_oldest_is_refused_as_too_old(tmp_path: Path) -> None:
    project = _with_pyproject(tmp_path, "[tool.cloudfall]\nformat = 0\n")
    later = FormatRange(oldest=ProjectFormat(1), current=ProjectFormat(1))

    with pytest.raises(ProjectFormatError) as raised:
        check_project_format(project, later)
    refusal = project_format_unsupported(raised.value)

    assert refusal.name.value == "PROJECT_FORMAT_UNSUPPORTED"
    assert refusal.context == {
        "code": "project_format_too_old",
        "projectFormat": 0,
        "oldest": 1,
        "current": 1,
        "fix": "cloudfall config upgrade",
    }
    assert "cloudfall config upgrade" in refusal.message


@pytest.mark.parametrize(
    "pyproject",
    [
        '[tool.cloudfall]\nformat = "1"\n',
        "[tool.cloudfall]\nformat = -1\n",
        "[tool]\ncloudfall = 1\n",
        "[tool.cloudfall]\nformat = true\n",
        "[tool.cloudfall]\nformat = 1.0\n",
        "[tool.cloudfall]\n",
        "tool = 1\n",
        "[tool.cloudfall\n",
    ],
    ids=[
        "string",
        "negative",
        "not-a-table",
        "boolean",
        "float",
        "no-format-key",
        "tool-not-a-table",
        "not-toml",
    ],
)
def test_s017_7_a_malformed_format_stops_validate_as_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], pyproject: str
) -> None:
    project = _with_pyproject(tmp_path, pyproject)

    exit_code, payload = _validate(capsys, project)

    assert exit_code == PROJECT_FORMAT_UNSUPPORTED_EXIT
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "PROJECT_FORMAT_UNSUPPORTED"
    assert error["context"]["code"] == FormatErrorCode.INVALID.value
    assert (error["context"]["oldest"], error["context"]["current"]) == (0, CURRENT)


def test_s017_7_a_pyproject_that_is_not_utf8_stops_validate_as_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = copy_examples(tmp_path / "project")
    (project / "pyproject.toml").write_bytes(b"[tool.cloudfall]\nformat = 1 # \xff\n")

    exit_code, payload = _validate(capsys, project)

    assert exit_code == PROJECT_FORMAT_UNSUPPORTED_EXIT
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["context"]["code"] == FormatErrorCode.INVALID.value


def test_a_project_newer_than_current_stops_validate_as_too_new(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _with_pyproject(tmp_path, f"[tool.cloudfall]\nformat = {CURRENT + 1}\n")

    exit_code, payload = _validate(capsys, project)

    assert exit_code == PROJECT_FORMAT_UNSUPPORTED_EXIT
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["context"] == {
        "code": "project_format_too_new",
        "projectFormat": CURRENT + 1,
        "oldest": 0,
        "current": CURRENT,
    }
    assert "cloudfall==" in error["message"]
    assert "uv sync" in error["message"]


def test_a_project_at_the_current_format_validates_without_a_format_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _with_pyproject(tmp_path, f"[tool.cloudfall]\nformat = {CURRENT}\n")

    exit_code, payload = _validate(capsys, project)

    assert exit_code == 0, payload
    assert not set(_codes(payload)) & set(FORMAT_WARNINGS)


@pytest.mark.parametrize(
    "argv",
    [
        (*CLOUDFALL, "changelog"),
        (*CLOUDFALL, "--schema"),
        (*ENGINE, "manifest"),
    ],
    ids=["changelog", "schema", "engine-manifest"],
)
def test_s017_10_commands_that_read_no_project_run_in_a_too_new_project(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    project = _with_pyproject(tmp_path, f"[tool.cloudfall]\nformat = {CURRENT + 1}\n")

    done = _run_in(project, argv)

    assert done.returncode == 0, done.stdout + done.stderr


def test_s017_10_init_runs_in_a_too_new_project(tmp_path: Path) -> None:
    project = _with_pyproject(tmp_path, f"[tool.cloudfall]\nformat = {CURRENT + 1}\n")
    empty = tmp_path / "empty"
    empty.mkdir()

    done = _run_in(project, (*CLOUDFALL, "init", str(empty)))

    assert done.returncode == 0, done.stdout + done.stderr
    assert (empty / "pyproject.toml").is_file()


def _run_in(project: Path, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` inside ``project``, which is also the named project."""
    return subprocess.run(  # noqa: S603 - the venv's own console scripts.
        argv,
        cwd=project,
        env={**os.environ, "CLOUDFALL_PROJECT": str(project)},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
