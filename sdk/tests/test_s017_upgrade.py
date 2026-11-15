"""``cloudfall config upgrade`` brings a project to the current format (S-017)."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.project_format import (
    PROJECT_FORMAT_UNSUPPORTED_EXIT,
    SUPPORTED_FORMATS,
    FormatRange,
    ProjectFormat,
    read_project_format,
)
from cloudfall.upgrade import UpgradeError, upgrade_project
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
DEPRECATED = ROOT / "config" / "tests" / "deprecated" / "server-type"
CURRENT = SUPPORTED_FORMATS.current.value
PYPROJECT = (
    "# The fleet's own pin.\n"
    "[project]\n"
    'name = "fleet"\n'
    'dependencies = ["cloudfall==0.8.0"]  # kept as written\n'
    "\n"
    "[tool.uv]\n"
    "package = false\n"
)


def _run(
    capsys: pytest.CaptureFixture[str], command: str, project: Path
) -> tuple[int, dict[str, object]]:
    exit_code = main(
        ["config", command, "--project", str(project), "--schemas", str(SCHEMAS)]
    )
    return exit_code, json.loads(capsys.readouterr().out)


def _contents(directory: Path) -> dict[Path, bytes]:
    files = sorted(directory.rglob("*"))
    return {path: path.read_bytes() for path in files if path.is_file()}


def _deprecated(tmp_path: Path, pyproject: str | None) -> Path:
    project = tmp_path / "project"
    shutil.copytree(DEPRECATED, project)
    if pyproject is not None:
        (project / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return project


def test_s017_8_upgrade_rewrites_the_old_names_and_writes_the_current_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _deprecated(tmp_path, PYPROJECT)

    exit_code, payload = _run(capsys, "upgrade", project)

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["effect"] == "updated"
    assert (data["from_format"], data["format"]) == (0, CURRENT)
    assert {entry["path"]: entry["moved_from"] for entry in data["upgraded"]} == {
        "servers/h1.yaml": None,
        "baselines/debian-application.yaml": "server-types/debian-application.yaml",
    }
    assert not (project / "server-types").exists()
    baseline = (project / "baselines" / "debian-application.yaml").read_text(
        encoding="utf-8"
    )
    assert "kind: Baseline" in baseline
    assert "ServerType" not in baseline
    server = (project / "servers" / "h1.yaml").read_text(encoding="utf-8")
    assert "  baseline: debian-application" in server
    assert "serverType" not in server
    # Every line of pyproject.toml stays; only the format table is added.
    written = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert written == f"{PYPROJECT}\n[tool.cloudfall]\nformat = {CURRENT}\n"

    exit_code, payload = _run(capsys, "validate", project)

    assert exit_code == 0, payload
    assert payload["warnings"] == []

    before = _contents(project)
    exit_code, payload = _run(capsys, "upgrade", project)

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["effect"] == "noop"
    assert data["upgraded"] == []
    assert (data["from_format"], data["format"]) == (CURRENT, CURRENT)
    assert _contents(project) == before


@pytest.mark.parametrize(
    ("pyproject", "expected"),
    [
        (None, f"[tool.cloudfall]\nformat = {CURRENT}\n"),
        ("", f"[tool.cloudfall]\nformat = {CURRENT}\n"),
        (
            '[project]\r\nname = "fleet"',
            '[project]\r\nname = "fleet"\r\n\r\n'
            f"[tool.cloudfall]\r\nformat = {CURRENT}\r\n",
        ),
        (
            "[tool.cloudfall]\nformat = 0  # written by hand\n\n[tool.uv]\n",
            f"[tool.cloudfall]\nformat = {CURRENT}  # written by hand\n\n[tool.uv]\n",
        ),
        (
            '[ "tool" . cloudfall ]\nformat=0\n',
            f'[ "tool" . cloudfall ]\nformat={CURRENT}\n',
        ),
    ],
    ids=["no-file", "empty-file", "crlf-no-final-newline", "format-0", "quoted-header"],
)
def test_s017_8_the_format_is_written_keeping_every_other_byte(
    tmp_path: Path, pyproject: str | None, expected: str
) -> None:
    project = copy_examples(tmp_path / "project")
    path = project / "pyproject.toml"
    if pyproject is not None:
        path.write_bytes(pyproject.encode("utf-8"))

    result = upgrade_project(project, SUPPORTED_FORMATS)

    assert result.changed
    assert result.files == ()
    assert path.read_bytes() == expected.encode("utf-8")
    assert read_project_format(project, SUPPORTED_FORMATS).value == CURRENT


@pytest.mark.parametrize(
    "pyproject",
    [
        "[tool]\ncloudfall = { format = 0 }\n",
        "tool.cloudfall.format = 0\n",
    ],
    ids=["inline-table", "dotted-keys"],
)
def test_a_format_it_cannot_write_in_place_is_refused_with_nothing_changed(
    tmp_path: Path, pyproject: str
) -> None:
    project = _deprecated(tmp_path, pyproject)
    before = _contents(project)

    with pytest.raises(UpgradeError) as raised:
        upgrade_project(project, SUPPORTED_FORMATS)

    assert raised.value.code == "upgrade_format_unwritable"
    assert _contents(project) == before


def test_s017_9_upgrade_runs_on_a_project_below_oldest(tmp_path: Path) -> None:
    # proves: S-017-9
    project = _deprecated(tmp_path, "[tool.cloudfall]\nformat = 0\n")
    later = FormatRange(oldest=ProjectFormat(1), current=ProjectFormat(1))

    result = upgrade_project(project, later)

    assert (result.from_format, result.format) == (ProjectFormat(0), ProjectFormat(1))
    assert [upgraded.path for upgraded in result.files] == [
        "servers/h1.yaml",
        "baselines/debian-application.yaml",
    ]
    assert read_project_format(project, later) == ProjectFormat(1)
    assert not (project / "server-types").exists()


def test_s017_9_upgrade_refuses_a_too_new_project_changing_no_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _deprecated(tmp_path, f"[tool.cloudfall]\nformat = {CURRENT + 1}\n")
    before = _contents(project)

    exit_code, payload = _run(capsys, "upgrade", project)

    assert exit_code == PROJECT_FORMAT_UNSUPPORTED_EXIT
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "PROJECT_FORMAT_UNSUPPORTED"
    assert error["context"] == {
        "code": "project_format_too_new",
        "projectFormat": CURRENT + 1,
        "oldest": SUPPORTED_FORMATS.oldest.value,
        "current": CURRENT,
    }
    assert _contents(project) == before


def test_upgrade_refuses_an_invalid_format_changing_no_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _deprecated(tmp_path, '[tool.cloudfall]\nformat = "0"\n')
    before = _contents(project)

    exit_code, payload = _run(capsys, "upgrade", project)

    assert exit_code == PROJECT_FORMAT_UNSUPPORTED_EXIT
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["context"]["code"] == "project_format_invalid"
    assert _contents(project) == before


def test_a_format_0_project_with_nothing_to_rename_still_gets_its_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = copy_examples(tmp_path / "project")

    exit_code, payload = _run(capsys, "upgrade", project)

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["effect"] == "updated"
    assert data["upgraded"] == []
    pyproject = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject == {"tool": {"cloudfall": {"format": CURRENT}}}


def test_a_project_at_the_current_format_is_a_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = copy_examples(tmp_path / "project")
    (project / "pyproject.toml").write_text(
        f"[tool.cloudfall]\nformat = {CURRENT}\n", encoding="utf-8"
    )
    before = _contents(project)

    exit_code, payload = _run(capsys, "upgrade", project)

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["effect"] == "noop"
    assert _contents(project) == before
