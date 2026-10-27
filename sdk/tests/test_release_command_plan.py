"""The release command in the ``deploy`` and ``rollback`` plans (S-005-8, S-005-9).

The engine directory the CLI is pointed at is a stand-in whose playbooks
only leave a marker file on the controller, so a test sees whether
``ansible-playbook`` ran at all without contacting any host.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cloudfall.cli import main
from cloudfall.domain import ReleaseId, ResourceId
from cloudfall.lifecycle import EngineContext, preview_deploy, preview_rollback

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
RELEASE = "20260101T000000Z-abcdef0"
RELEASE_DIRECTORY = f"/srv/apps/crm/backend/releases/{RELEASE}"
ROLLBACK_RUN = (
    ENGINE / "ansible" / "roles" / "cloudfall_deploy" / "tasks" / "rollback.yml"
)


def _project(tmp_path: Path, release: str | None) -> Path:
    """Copy the examples, declaring ``release`` on crm-backend when given."""
    project = tmp_path / "config"
    shutil.copytree(EXAMPLES, project)
    if release is not None:
        path = project / "components" / "crm-backend.yaml"
        text = path.read_text(encoding="utf-8")
        text = text.replace("      - h1\n      - h2\n", "      - h2\n      - h1\n")
        text = text.replace("  healthCheck:\n", f"{release}  healthCheck:\n")
        path.write_text(text, encoding="utf-8")
    return project


_DECLARED = "  release:\n    command: [.venv/bin/alembic, upgrade, head]\n"


def _marker_engine(tmp_path: Path) -> Path:
    """Lay out an engine whose deploy and rollback playbooks only write a marker."""
    engine = tmp_path / "engine"
    playbooks = engine / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    shutil.copy(ENGINE / "ansible" / "ansible.cfg", engine / "ansible")
    for name in ("deploy.yml", "rollback.yml"):
        marker = tmp_path / f"ran-{name}"
        (playbooks / name).write_text(
            "---\n- name: Leave a marker\n  hosts: localhost\n"
            "  connection: local\n  gather_facts: false\n  tasks:\n"
            "    - name: Write the marker\n      ansible.builtin.copy:\n"
            f"        dest: {marker}\n        content: ran\n        mode: '0600'\n",
            encoding="utf-8",
        )
    return engine


def _write_artifact(tmp_path: Path) -> Path:
    artifact_directory = tmp_path / "artifacts"
    component_directory = artifact_directory / "crm-backend"
    component_directory.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('crm')\n", encoding="utf-8")
    archive_path = component_directory / f"{RELEASE}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source, arcname=".")
    metadata = {
        "apiVersion": "cloudfall/v1",
        "kind": "Artifact",
        "metadata": {"id": "crm-backend"},
        "spec": {
            "component": "crm-backend",
            "release": RELEASE,
            "gitRef": "main",
            "gitCommit": "a" * 40,
            "builtAt": "2026-01-01T00:00:00Z",
            "archive": archive_path.name,
            "archiveSha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "sizeBytes": archive_path.stat().st_size,
        },
    }
    (component_directory / f"{RELEASE}.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    return artifact_directory


def _cli(
    argv: list[str],
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    exit_code = main(
        [
            *argv,
            "--project",
            str(project),
            "--schemas",
            str(SCHEMAS),
            "--engine",
            str(_marker_engine(tmp_path)),
            "--inventory-file",
            str(tmp_path / "inventory.json"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return exit_code, payload


def _deploy_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], release: str | None
) -> tuple[int, dict[str, Any]]:
    return _cli(
        [
            "deploy",
            "crm-backend",
            "--release",
            RELEASE,
            "--artifacts",
            str(_write_artifact(tmp_path)),
        ],
        _project(tmp_path, release),
        tmp_path,
        capsys,
    )


def _ran_no_playbook(tmp_path: Path) -> bool:
    return not any(tmp_path.glob("ran-*")) and not (
        tmp_path / "inventory.json"
    ).exists()


def test_s005_8_the_deploy_plan_shows_the_release_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _deploy_plan(tmp_path, capsys, _DECLARED)

    assert exit_code == 0, payload
    data = payload["data"]
    assert data["status"] == "plan"
    assert payload["meta"]["dry_run"] is True
    assert data["release_command"] == {
        "command": [".venv/bin/alembic", "upgrade", "head"],
        "working_directory": RELEASE_DIRECTORY,
        "timeout_seconds": 300,
        "server": "h2",
    }
    assert (
        "release command .venv/bin/alembic upgrade head once on h2, before any "
        "server switches"
    ) in data["would_run"]
    assert _ran_no_playbook(tmp_path)


def test_s005_8_the_plan_resolves_a_declared_working_directory_and_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    declared = (
        f"{_DECLARED}    workingDirectory: services/api/\n    timeoutSeconds: 60\n"
    )

    exit_code, payload = _deploy_plan(tmp_path, capsys, declared)

    assert exit_code == 0, payload
    release_command = payload["data"]["release_command"]
    assert release_command["working_directory"] == f"{RELEASE_DIRECTORY}/services/api"
    assert release_command["timeout_seconds"] == 60
    assert _ran_no_playbook(tmp_path)


def test_s005_8_a_component_without_release_shows_a_null_release_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _deploy_plan(tmp_path, capsys, None)

    assert exit_code == 0, payload
    assert "release_command" in payload["data"]
    assert payload["data"]["release_command"] is None
    assert "release command" not in payload["data"]["would_run"]
    assert _ran_no_playbook(tmp_path)


def test_s005_9_the_rollback_plan_skips_the_release_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _cli(
        ["rollback", "crm-backend", "--release", RELEASE],
        _project(tmp_path, _DECLARED),
        tmp_path,
        capsys,
    )

    assert exit_code == 0, payload
    data = payload["data"]
    assert data["status"] == "plan"
    assert data["release_command_skipped"] is True
    assert "release command .venv/bin/alembic upgrade head is not run" in (
        data["would_run"]
    )
    assert _ran_no_playbook(tmp_path)


def test_s005_9_a_rollback_without_release_skips_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _cli(
        ["rollback", "crm-backend", "--release", RELEASE],
        _project(tmp_path, None),
        tmp_path,
        capsys,
    )

    assert exit_code == 0, payload
    assert payload["data"]["release_command_skipped"] is None
    assert "release command" not in payload["data"]["would_run"]


def test_s005_9_the_rollback_run_includes_no_release_command_task() -> None:
    text = ROLLBACK_RUN.read_text(encoding="utf-8")
    rollback_playbook = (ENGINE / "ansible" / "playbooks" / "rollback.yml").read_text(
        encoding="utf-8"
    )

    assert "release_command" not in text
    assert "systemd-run" not in text
    assert "tasks_from: rollback" in rollback_playbook
    assert "release_command" not in rollback_playbook
    included = [
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if "include_tasks" in line
    ]
    assert included == ["health_gate.yml"]
    health_gate = ROLLBACK_RUN.with_name("health_gate.yml").read_text(
        encoding="utf-8"
    )
    assert "release_command" not in health_gate


def test_s005_8_the_marker_engine_does_record_a_playbook_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: the same stand-in engine, run with --yes, leaves its marker."""
    exit_code, payload = _cli(
        ["rollback", "crm-backend", "--release", RELEASE, "--yes"],
        _project(tmp_path, _DECLARED),
        tmp_path,
        capsys,
    )

    assert exit_code == 0, payload
    assert (tmp_path / "ran-rollback.yml").read_text(encoding="utf-8") == "ran"


def test_s005_8_the_output_schemas_declare_the_new_plan_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["deploy", "--schema"]) == 0
    deploy = json.loads(capsys.readouterr().out)["data"]["output_schema"]
    assert main(["rollback", "--schema"]) == 0
    rollback = json.loads(capsys.readouterr().out)["data"]["output_schema"]

    release_command = deploy["properties"]["release_command"]
    assert release_command["type"] == ["object", "null"]
    assert set(release_command["required"]) == {
        "command",
        "working_directory",
        "timeout_seconds",
        "server",
    }
    assert "release_command" in deploy["required"]
    assert rollback["properties"]["release_command_skipped"] == {
        "type": ["boolean", "null"]
    }
    assert "release_command" not in rollback["properties"]


def test_s005_8_the_sdk_preview_carries_the_typed_release_command(
    tmp_path: Path,
) -> None:
    context = EngineContext(
        project_directory=_project(tmp_path, _DECLARED),
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=tmp_path / "inventory.json",
    )

    deploy = preview_deploy(
        context,
        ResourceId("crm-backend"),
        ReleaseId(RELEASE),
        _write_artifact(tmp_path),
    )
    rollback = preview_rollback(
        context, ResourceId("crm-backend"), ReleaseId(RELEASE)
    )

    assert deploy.release_command is not None
    assert deploy.release_command.server == ResourceId("h2")
    assert deploy.as_dict()["releaseCommand"] == {
        "command": [".venv/bin/alembic", "upgrade", "head"],
        "workingDirectory": RELEASE_DIRECTORY,
        "timeoutSeconds": 300,
        "server": "h2",
    }
    assert "releaseCommandSkipped" not in deploy.as_dict()
    assert rollback.release_command is None
    assert rollback.as_dict()["releaseCommandSkipped"] is True
