"""Receipts and operator proposals live in the committed ``record/`` (D-16, #219).

A project keeps them under ``record/`` by default, as decisions live in
``decisions/``; ``tmp/`` keeps only what can be rebuilt. ``cloudfall config
upgrade`` moves an existing project's receipts and proposals out of
``tmp/``, and no record written there holds a secret value.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cloudfall import app
from cloudfall.agent_server import McpServeArgs
from cloudfall.agent_tools import AgentConfig
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.lifecycle import EngineContext
from cloudfall.project import RECORD_DIRECTORY, RUNTIME_DIRECTORY, RecordStore
from cloudfall.secrets import SopsSecretProvider, render_environment
from cloudfall.validation import SchemaCatalog
from examples_project import copy_examples

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
PROPOSAL_FIXTURES = sorted(
    (ROOT / "config" / "tests" / "records" / "proposals").glob("*.json")
)
SECRET = "s3cr3t-value-that-must-never-be-committed"  # noqa: S105 - a test value


def _run(
    capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, object]]:
    exit_code = main([*argv, "--schemas", str(SCHEMAS)])
    return exit_code, json.loads(capsys.readouterr().out)


def _path_defaults(cls: type) -> Iterator[tuple[str, Path]]:
    for field in dataclasses.fields(cls):
        if isinstance(field.default, Path):
            yield f"{cls.__name__}.{field.name}", field.default


def _option_classes() -> list[type]:
    classes: list[type] = [McpServeArgs, AgentConfig]
    classes.extend(
        value
        for value in vars(app).values()
        if isinstance(value, type)
        and dataclasses.is_dataclass(value)
        and value.__module__ == app.__name__
    )
    return classes


def test_every_receipt_and_proposal_default_is_in_the_record() -> None:
    legacy = {store.legacy_directory: store for store in RecordStore}
    defaults = [
        (name, default.as_posix())
        for cls in _option_classes()
        for name, default in _path_defaults(cls)
    ]

    assert [(name, path) for name, path in defaults if path in legacy] == []
    assert {store.directory for store in RecordStore} <= {path for _, path in defaults}
    assert all(
        store.directory.startswith(f"{RECORD_DIRECTORY}/") for store in RecordStore
    )


def _project(tmp_path: Path) -> Path:
    return copy_examples(tmp_path / "project")


def _proposal_ids() -> list[str]:
    return [
        json.loads(path.read_text(encoding="utf-8"))["metadata"]["id"]
        for path in PROPOSAL_FIXTURES
    ]


def _legacy_receipts(project: Path) -> dict[str, bytes]:
    """Lay out a pre-0.8.0 project's receipts and proposals under tmp/."""
    written: dict[str, bytes] = {}
    proposals = project / RecordStore.PROPOSALS.legacy_directory
    proposals.mkdir(parents=True)
    for fixture, proposal_id in zip(PROPOSAL_FIXTURES, _proposal_ids(), strict=True):
        shutil.copy(fixture, proposals / f"{proposal_id}.json")
        written[f"{RecordStore.PROPOSALS.value}/{proposal_id}.json"] = (
            fixture.read_bytes()
        )
    for store, name in (
        (RecordStore.DEPLOYMENTS, "crm-site.json"),
        (RecordStore.RELEASES, "crm-backend.json"),
        (RecordStore.BACKUPS, "postgresql-main-backup.json"),
        (RecordStore.DATA_MIGRATIONS, "postgresql-main-crm.preflight.json"),
        (RecordStore.ENVIRONMENT_RECEIPTS, "crm-backend.json"),
    ):
        directory = project / store.legacy_directory
        directory.mkdir(parents=True)
        content = json.dumps({"store": store.value, "name": name}).encode()
        (directory / name).write_bytes(content)
        written[f"{store.value}/{name}"] = content
    return written


def _record(project: Path) -> dict[str, bytes]:
    root = project / RECORD_DIRECTORY
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_config_upgrade_moves_receipts_and_proposals_into_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    written = _legacy_receipts(project)
    observed = project / RUNTIME_DIRECTORY / "observed" / "h1.json"
    observed.parent.mkdir(parents=True)
    observed.write_text("{}", encoding="utf-8")

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["effect"] == "updated"
    moved = {entry["moved_from"]: entry["path"] for entry in data["upgraded"]}
    assert moved == {
        f"{RUNTIME_DIRECTORY}/{name}": f"{RECORD_DIRECTORY}/{name}" for name in written
    }
    assert _record(project) == written
    for store in RecordStore:
        assert not (project / store.legacy_directory).exists()
    # What can be rebuilt stays where it was.
    assert observed.read_text(encoding="utf-8") == "{}"

    exit_code, payload = _run(capsys, "operator", "list", "--project", str(project))

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    listed = {proposal["metadata"]["id"] for proposal in data["proposals"]}
    assert listed == set(_proposal_ids())

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["effect"] == "noop"
    assert data["upgraded"] == []
    assert _record(project) == written


def test_config_upgrade_drops_a_copy_already_in_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    legacy = project / RecordStore.RELEASES.legacy_directory / "crm-backend.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"release": "r1"}', encoding="utf-8")
    target = project / RecordStore.RELEASES.directory / "crm-backend.json"
    target.parent.mkdir(parents=True)
    shutil.copy(legacy, target)

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 0, payload
    assert not legacy.exists()
    assert target.read_text(encoding="utf-8") == '{"release": "r1"}'


def test_config_upgrade_refuses_to_overwrite_a_different_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    _legacy_receipts(project)
    legacy = project / RecordStore.RELEASES.legacy_directory / "crm-backend.json"
    target = project / RecordStore.RELEASES.directory / "crm-backend.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"release": "newer"}', encoding="utf-8")
    before = legacy.read_bytes()

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 6
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["context"]["code"] == "upgrade_target_exists"
    assert "tmp/releases/crm-backend.json" in error["message"]
    assert "record/releases/crm-backend.json" in error["message"]
    # Nothing moved: the plan is checked whole before the first write.
    assert legacy.read_bytes() == before
    assert target.read_text(encoding="utf-8") == '{"release": "newer"}'
    assert (project / RecordStore.PROPOSALS.legacy_directory).is_dir()
    assert not (project / RecordStore.PROPOSALS.directory).exists()


def test_config_upgrade_never_moves_a_record_through_a_symlink(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    (outside / "releases").mkdir(parents=True)
    (outside / "releases" / "crm-backend.json").write_text("{}", encoding="utf-8")
    (project / RUNTIME_DIRECTORY).symlink_to(outside)

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 6
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["context"]["code"] == "upgrade_symlink"
    assert (outside / "releases" / "crm-backend.json").is_file()
    assert not (project / RECORD_DIRECTORY).exists()


def test_an_explicit_proposal_directory_still_reads_tmp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    _legacy_receipts(project)

    exit_code, payload = _run(
        capsys,
        "operator",
        "list",
        "--project",
        str(project),
        "--proposals",
        RecordStore.PROPOSALS.legacy_directory,
    )

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert len(data["proposals"]) == len(PROPOSAL_FIXTURES)


RELEASE = "20261027T000000Z-abcdef1"
SECRET_OUTPUT = f"migrating with DATABASE_URL=postgres://app:{SECRET}@db/app"


def _old_release_receipt(component: str) -> str:
    """Return a release receipt as 0.7.0 wrote it, the command's output in it."""
    return json.dumps(
        {
            "apiVersion": "cloudfall/v1",
            "kind": "ReleaseReceipt",
            "metadata": {"id": component, "description": "Release receipt"},
            "spec": {
                "component": component,
                "release": RELEASE,
                "artifactSha256": "a" * 64,
                "servers": [
                    {
                        "server": "h1",
                        "release": RELEASE,
                        "previousRelease": None,
                        "deployedAt": "2026-10-27T00:00:00Z",
                        "healthCheckPassed": True,
                        "rolledBack": False,
                    }
                ],
                "releaseCommand": {
                    "server": "h1",
                    "command": ["bin/migrate"],
                    "workingDirectory": None,
                    "exitCode": 0,
                    "outputTail": SECRET_OUTPUT,
                },
            },
        },
        indent=4,
    )


@pytest.mark.parametrize(
    "store_directory",
    [RecordStore.RELEASES.legacy_directory, RecordStore.RELEASES.directory],
    ids=["moved-from-tmp", "already-in-record"],
)
def test_config_upgrade_moves_a_release_command_output_out_of_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], store_directory: str
) -> None:
    project = _project(tmp_path)
    old = project / store_directory / "crm-backend.json"
    old.parent.mkdir(parents=True)
    old.write_text(_old_release_receipt("crm-backend"), encoding="utf-8")
    output = (
        project / RecordStore.RELEASES.output_directory / f"crm-backend.{RELEASE}.log"
    )

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 0, payload
    receipt = project / RecordStore.RELEASES.directory / "crm-backend.json"
    reference = output.relative_to(project).as_posix()
    data = payload["data"]
    assert isinstance(data, dict)
    [upgraded] = data["upgraded"]
    assert upgraded["path"] == receipt.relative_to(project).as_posix()
    assert upgraded["renamed"] == [f"spec.releaseCommand.outputTail -> {reference}"]
    assert SECRET not in _everything_under(project / RECORD_DIRECTORY)
    document = json.loads(receipt.read_text(encoding="utf-8"))
    SchemaCatalog(SCHEMAS).validate_named("release-receipt.schema.json", document)
    assert document["spec"]["releaseCommand"] == {
        "server": "h1",
        "command": ["bin/migrate"],
        "workingDirectory": None,
        "exitCode": 0,
        "output": {
            "path": reference,
            "sha256": hashlib.sha256(SECRET_OUTPUT.encode()).hexdigest(),
        },
    }
    assert output.read_text(encoding="utf-8") == SECRET_OUTPUT
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["upgraded"] == []


def test_config_upgrade_refuses_to_replace_a_different_kept_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    old = project / RecordStore.RELEASES.legacy_directory / "crm-backend.json"
    old.parent.mkdir(parents=True)
    old.write_text(_old_release_receipt("crm-backend"), encoding="utf-8")
    output = (
        project / RecordStore.RELEASES.output_directory / f"crm-backend.{RELEASE}.log"
    )
    output.parent.mkdir(parents=True)
    output.write_text("another run's output", encoding="utf-8")

    exit_code, payload = _run(capsys, "config", "upgrade", "--project", str(project))

    assert exit_code == 6
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["context"]["code"] == "upgrade_target_exists"
    assert output.read_text(encoding="utf-8") == "another run's output"
    assert SECRET in old.read_text(encoding="utf-8")
    assert not (project / RECORD_DIRECTORY).exists()


def _everything_under(directory: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    )


def test_an_environment_receipt_in_the_record_names_keys_and_a_hash_only(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    secrets = tmp_path / "secrets"
    (secrets / "production" / "crm").mkdir(parents=True)
    (secrets / "production" / "shared.env").write_text(
        "LOG_LEVEL=warning\n", encoding="utf-8"
    )
    (secrets / "production" / "crm.env").write_text(
        "LOG_LEVEL=info\n", encoding="utf-8"
    )
    (secrets / "production" / "crm" / "backend.env").write_text(
        f"DATABASE_URL={SECRET}\n", encoding="utf-8"
    )
    context = EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=project / RUNTIME_DIRECTORY / "inventory.json",
    )
    output = project / RUNTIME_DIRECTORY / "env" / "crm-backend.env"

    result = render_environment(
        context,
        ResourceId("crm-backend"),
        SopsSecretProvider(
            secrets_directory=secrets, decrypt=lambda path: path.read_text("utf-8")
        ),
        output,
        receipt_directory=project / RecordStore.ENVIRONMENT_RECEIPTS.directory,
    )

    receipt = Path(str(result["receipt"]))
    assert receipt.parent == project / RecordStore.ENVIRONMENT_RECEIPTS.directory
    assert "DATABASE_URL" in json.loads(receipt.read_text("utf-8"))["spec"]["keys"]
    # The secret is in the rendered file under tmp/, never in the record.
    assert SECRET in output.read_text(encoding="utf-8")
    assert SECRET not in _everything_under(project / RECORD_DIRECTORY)
