"""ServerType became Baseline (#198, D-11).

The old kind, folder and Server field keep working for one minor release with
a warning, and ``cloudfall config upgrade`` rewrites them.
"""

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
DEPRECATED = ROOT / "config" / "tests" / "deprecated" / "server-type"
BOTH_FIELDS = ROOT / "config" / "tests" / "invalid" / "baseline-both-fields"
OLD_BASELINE = Path("server-types") / "debian-application.yaml"
NEW_BASELINE = Path("baselines") / "debian-application.yaml"
SERVER = Path("servers") / "h1.yaml"


def _run(
    capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, object]]:
    exit_code = main([*argv, "--schemas", str(SCHEMAS)])
    return exit_code, json.loads(capsys.readouterr().out)


def _copy(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(DEPRECATED, project)
    return project


def test_the_old_names_read_as_a_baseline_and_record_each_use() -> None:
    state = validate_config(DEPRECATED, SCHEMAS)

    assert state.counts_by_kind() == {"Baseline": 1, "Server": 1}
    inventory = PlatformInventory.from_state(state)
    assert inventory.servers[0].baseline_id == ResourceId("debian-application")
    assert inventory.baseline(ResourceId("debian-application")).resource_id == (
        ResourceId("debian-application")
    )
    assert [
        (use.path.relative_to(DEPRECATED), use.old, use.new)
        for use in state.deprecations
    ] == [
        (SERVER, "spec.serverType", "spec.baseline"),
        (OLD_BASELINE, "server-types/", "baselines/"),
        (OLD_BASELINE, "kind: ServerType", "kind: Baseline"),
    ]


def test_the_current_names_record_no_deprecation() -> None:
    state = validate_config(ROOT / "config" / "examples", SCHEMAS)

    assert state.deprecations == ()


def test_config_validate_warns_once_per_file_and_names_the_rewrite(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, payload = _run(
        capsys, "config", "validate", "--project", str(DEPRECATED)
    )

    assert exit_code == 0
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    assert [warning["code"] for warning in warnings] == ["DEPRECATED_NAME"] * 2
    by_path = {warning["context"]["path"]: warning for warning in warnings}
    assert set(by_path) == {SERVER.as_posix(), OLD_BASELINE.as_posix()}
    baseline = by_path[OLD_BASELINE.as_posix()]
    assert baseline["context"]["names"] == [
        {"old": "server-types/", "new": "baselines/"},
        {"old": "kind: ServerType", "new": "kind: Baseline"},
    ]
    assert "cloudfall config upgrade" in baseline["message"]
    assert baseline["context"]["fix"] == "cloudfall config upgrade"


def test_a_server_naming_both_fields_is_config_invalid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(ConfigValidationError) as error:
        validate_config(BOTH_FIELDS, SCHEMAS)
    assert error.value.issue.code == "server_baseline_conflict"
    assert error.value.issue.field_path == ("spec", "serverType")

    exit_code, payload = _run(
        capsys, "config", "validate", "--project", str(BOTH_FIELDS)
    )

    assert exit_code == 80
    error_body = payload["error"]
    assert isinstance(error_body, dict)
    assert error_body["code"] == "CONFIG_INVALID"
    assert error_body["context"]["code"] == "server_baseline_conflict"


def test_one_baseline_id_in_both_folders_is_a_duplicate(tmp_path: Path) -> None:
    project = _copy(tmp_path)
    (project / "baselines").mkdir()
    shutil.copy(project / OLD_BASELINE, project / NEW_BASELINE)

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project, SCHEMAS)

    assert error.value.issue.code == "resource_duplicate"
    assert "Baseline/debian-application" in error.value.issue.message


def test_config_upgrade_rewrites_only_the_old_names_and_a_second_run_is_a_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy(tmp_path)
    old_baseline = (project / OLD_BASELINE).read_text(encoding="utf-8")
    old_server = (project / SERVER).read_text(encoding="utf-8")

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 0
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["effect"] == "updated"
    assert data["upgraded"] == [
        {
            "path": SERVER.as_posix(),
            "moved_from": None,
            "renamed": ["spec.serverType -> spec.baseline"],
        },
        {
            "path": NEW_BASELINE.as_posix(),
            "moved_from": OLD_BASELINE.as_posix(),
            "renamed": ["kind: ServerType -> kind: Baseline"],
        },
    ]
    assert not (project / "server-types").exists()
    # Comments and every other line stay as they were.
    assert (project / NEW_BASELINE).read_text(encoding="utf-8") == old_baseline.replace(
        "kind: ServerType  #", "kind: Baseline  #"
    )
    assert (project / SERVER).read_text(encoding="utf-8") == old_server.replace(
        "  serverType: debian-application", "  baseline: debian-application"
    )
    state = validate_config(project, SCHEMAS)
    assert state.deprecations == ()
    assert state.get(ResourceKind.BASELINE, ResourceId("debian-application"))

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 0
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["effect"] == "noop"
    assert data["upgraded"] == []


def test_config_upgrade_keeps_a_quoted_kind_quoted(tmp_path: Path) -> None:
    project = _copy(tmp_path)
    path = project / OLD_BASELINE
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "kind: ServerType  #", 'kind: "ServerType"  #'
        ),
        encoding="utf-8",
    )

    exit_code = main(
        ["config", "upgrade", "--project", str(project), "--schemas", str(SCHEMAS)]
    )

    assert exit_code == 0
    assert 'kind: "Baseline"  #' in (project / NEW_BASELINE).read_text(encoding="utf-8")


def test_config_upgrade_refuses_a_project_that_does_not_validate(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    project = tmp_path / "project"
    shutil.copytree(BOTH_FIELDS, project)
    before = (project / SERVER).read_text(encoding="utf-8")

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 80
    assert payload["ok"] is False
    assert (project / SERVER).read_text(encoding="utf-8") == before


def test_add_server_type_is_a_deprecated_name_of_add_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "config" / "examples", project)

    exit_code, payload = _run(
        capsys, "add", "server-type", "debian-minimal", "--project", str(project)
    )

    assert exit_code == 0
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["added"] == [
        {
            "kind": "Baseline",
            "id": "debian-minimal",
            "path": "baselines/debian-minimal.yaml",
        }
    ]
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    assert warnings, "a deprecated command warns"


def test_add_server_reuses_a_baseline_still_in_the_old_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy(tmp_path)

    exit_code, payload = _run(
        capsys,
        "add",
        "server",
        "h2",
        "--address",
        "192.0.2.20",
        "--project",
        str(project),
    )

    assert exit_code == 0
    data = payload["data"]
    assert isinstance(data, dict)
    assert [added["kind"] for added in data["added"]] == ["Server"]
    assert not (project / "baselines").exists()


def test_add_server_takes_type_as_a_deprecated_name_of_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    shutil.copytree(ROOT / "config" / "examples", project)
    server = ("add", "server", "h3", "--address", "192.0.2.30")

    exit_code, payload = _run(
        capsys, *server, "--type", "debian-minimal", "--project", str(project)
    )

    assert exit_code == 0
    assert payload["warnings"]
    text = (project / "servers" / "h3.yaml").read_text(encoding="utf-8")
    assert "baseline: debian-minimal" in text
    assert "serverType" not in text

    exit_code, payload = _run(
        capsys,
        *server,
        "--type",
        "debian-minimal",
        "--baseline",
        "debian-minimal",
        "--project",
        str(project),
    )

    assert exit_code == 2
    assert payload["ok"] is False
