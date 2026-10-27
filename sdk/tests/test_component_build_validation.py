"""Validation of a Component's build steps, build tools and artifact includes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.domain import ResourceId, ResourceKind
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import ConfigValidationError, validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"

_BUILD_BLOCK = """\
  buildTools:
    node: "22.0.0"
    pnpm: "9.0.0"
  build:
    - workingDirectory: web
      command: [pnpm, install, --frozen-lockfile]
    - workingDirectory: web
      command: [pnpm, build]
  artifact:
    include: [web/dist]
"""


def _project_with_component_spec(tmp_path: Path, extra: str) -> Path:
    project_directory = tmp_path / "config"
    shutil.copytree(EXAMPLES, project_directory)
    component_path = project_directory / "components" / "crm-backend.yaml"
    component_path.write_text(
        component_path.read_text(encoding="utf-8") + extra, encoding="utf-8"
    )
    return project_directory


def test_s002_1_component_with_build_steps_validates(tmp_path: Path) -> None:
    project_directory = _project_with_component_spec(tmp_path, _BUILD_BLOCK)

    state = validate_config(project_directory, SCHEMAS)

    component = next(
        item
        for item in PlatformInventory.from_state(state).components
        if item.resource_id == ResourceId("crm-backend")
    )
    assert component.build is not None
    assert component.as_dict()["buildTools"] == {"node": "22.0.0", "pnpm": "9.0.0"}
    assert component.as_dict()["build"] == [
        {
            "workingDirectory": "web",
            "command": ["pnpm", "install", "--frozen-lockfile"],
            "timeoutSeconds": 900,
        },
        {
            "workingDirectory": "web",
            "command": ["pnpm", "build"],
            "timeoutSeconds": 900,
        },
    ]
    assert component.as_dict()["artifact"] == {"include": ["web/dist"]}


def test_s002_1_existing_component_without_build_is_unchanged() -> None:
    state = validate_config(EXAMPLES, SCHEMAS)

    assert state.get(ResourceKind.COMPONENT, ResourceId("crm-backend"))
    for component in PlatformInventory.from_state(state).components:
        assert component.build is None
        serialized = component.as_dict()
        assert {"build", "buildTools", "artifact"}.isdisjoint(serialized)


def test_s002_1_build_step_timeout_accepts_its_bounds(tmp_path: Path) -> None:
    project_directory = _project_with_component_spec(
        tmp_path,
        _BUILD_BLOCK.replace(
            "command: [pnpm, build]",
            "command: [pnpm, build]\n      timeoutSeconds: 3600",
        ),
    )

    state = validate_config(project_directory, SCHEMAS)

    component = PlatformInventory.from_state(state).components[0]
    assert component.build is not None
    assert component.build.steps[1].timeout_seconds.value == 3600


def test_s002_11_step_running_an_undeclared_tool_is_refused(
    tmp_path: Path,
) -> None:
    project_directory = _project_with_component_spec(
        tmp_path, _BUILD_BLOCK.replace("command: [pnpm, build]", "command: [npx, vite]")
    )

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "component_build_tool_undeclared"
    assert "'npx'" in error.value.issue.message
    assert error.value.issue.field_path == ("spec", "build", 1, "command", 0)


def test_s002_11_cli_refuses_an_undeclared_tool_as_config_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_directory = _project_with_component_spec(
        tmp_path, _BUILD_BLOCK.replace("command: [pnpm, build]", "command: [npx, vite]")
    )

    exit_code = main(
        ["config", "validate", "--project", str(project_directory), "--schemas",
         str(SCHEMAS)]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 80
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert payload["error"]["context"]["code"] == "component_build_tool_undeclared"


def test_s002_11_build_without_build_tools_is_refused(tmp_path: Path) -> None:
    tools = '  buildTools:\n    node: "22.0.0"\n    pnpm: "9.0.0"\n'
    block = _BUILD_BLOCK.replace(tools, "")
    project_directory = _project_with_component_spec(tmp_path, block)

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "component_build_tool_undeclared"


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("workingDirectory: web\n      command: [pnpm, build]",
         "workingDirectory: /web\n      command: [pnpm, build]"),
        ("workingDirectory: web\n      command: [pnpm, build]",
         "workingDirectory: web/../..\n      command: [pnpm, build]"),
        ("workingDirectory: web\n      command: [pnpm, build]",
         "workingDirectory: ..\n      command: [pnpm, build]"),
        ("include: [web/dist]", "include: [/srv/web/dist]"),
        ("include: [web/dist]", "include: [web/../../etc]"),
    ],
    ids=[
        "absolute-working-directory",
        "climbing-working-directory",
        "parent-working-directory",
        "absolute-include",
        "climbing-include",
    ],
)
def test_s002_11_absolute_or_climbing_paths_are_refused(
    tmp_path: Path, original: str, replacement: str
) -> None:
    project_directory = _project_with_component_spec(
        tmp_path, _BUILD_BLOCK.replace(original, replacement)
    )

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "schema_validation_failed"
    assert error.value.issue.field_path[:2] in {("spec", "build"), ("spec", "artifact")}


@pytest.mark.parametrize(
    ("extra", "dependent"),
    [
        ('  buildTools:\n    node: "22.0.0"\n', "buildTools"),
        ("  artifact:\n    include: [web/dist]\n", "artifact"),
    ],
    ids=["build-tools-alone", "artifact-alone"],
)
def test_s002_11_build_tools_or_artifact_without_build_is_refused(
    tmp_path: Path, extra: str, dependent: str
) -> None:
    project_directory = _project_with_component_spec(tmp_path, extra)

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "schema_validation_failed"
    assert "'build'" in error.value.issue.message
    assert dependent in error.value.issue.message


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("command: [pnpm, build]", "command: pnpm build"),
        ("command: [pnpm, build]", "command: []"),
        ("[pnpm, build]", "[pnpm, build]\n      timeoutSeconds: 0"),
        ("[pnpm, build]", "[pnpm, build]\n      timeoutSeconds: 3601"),
    ],
    ids=["shell-string", "empty-argv", "timeout-zero", "timeout-too-long"],
)
def test_s002_11_malformed_steps_are_refused(
    tmp_path: Path, original: str, replacement: str
) -> None:
    project_directory = _project_with_component_spec(
        tmp_path, _BUILD_BLOCK.replace(original, replacement)
    )

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "schema_validation_failed"
