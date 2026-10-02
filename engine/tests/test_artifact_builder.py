"""Release artifact builder tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.artifact import ArtifactBuildError, build_artifact
from cloudfall_engine.cli import main

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
_GIT = shutil.which("git") or "git"


def _git(*arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixture repository setup.
        [
            _GIT,
            "-c",
            "user.email=test@example.test",
            "-c",
            "user.name=Cloudfall Test",
            *arguments,
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout


def _fixture_inventory(tmp_path: Path) -> PlatformInventory:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("-C", str(repository), "-c", "init.defaultBranch=main", "init", "--quiet")
    (repository / "app.py").write_text("print('crm')\n", encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        '[application]\nname = "crm-backend"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    _git("-C", str(repository), "add", ".")
    _git("-C", str(repository), "commit", "--quiet", "--message", "initial")

    project_directory = tmp_path / "config"
    shutil.copytree(EXAMPLES, project_directory)
    component_path = project_directory / "components" / "crm-backend.yaml"
    component = component_path.read_text(encoding="utf-8").replace(
        "https://github.com/example/crm-backend.git",
        f"file://{repository}",
    )
    component_path.write_text(component, encoding="utf-8")
    return PlatformInventory.from_state(validate_config(project_directory, SCHEMAS))


def _build(
    tmp_path: Path, ref: str, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    """Build through the CLI, so git runs under treaty's ``ctx.run``."""
    exit_code = main(
        [
            "artifact",
            "build",
            "crm-backend",
            "--ref",
            ref,
            "--project",
            str(tmp_path / "config"),
            "--schemas",
            str(SCHEMAS),
            "--output-dir",
            str(tmp_path / "artifacts"),
        ]
    )
    envelope = cast("dict[str, Any]", json.loads(capsys.readouterr().out))
    assert exit_code == 0, envelope["error"]
    return cast("dict[str, Any]", envelope["data"])


def _git_never_runs(argv: Sequence[str]) -> NoReturn:
    message = f"git must not run for refused input: {argv}"
    raise AssertionError(message)


def test_builder_packages_a_hashed_release_with_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fixture_inventory(tmp_path)

    built = _build(tmp_path, "main", capsys)

    archive_path = Path(built["archive"])
    metadata_path = Path(built["metadata"])
    assert archive_path.is_file()
    assert metadata_path.is_file()
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert digest == built["archive_sha256"]

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["kind"] == "Artifact"
    assert metadata["spec"]["component"] == "crm-backend"
    assert metadata["spec"]["release"] == built["release"]
    assert metadata["spec"]["gitCommit"] == built["git_commit"]
    assert metadata["spec"]["archiveSha256"] == digest
    assert metadata["spec"]["sizeBytes"] == archive_path.stat().st_size

    with tarfile.open(archive_path) as archive:
        names = archive.getnames()
    assert "./app.py" in names
    assert "./pyproject.toml" in names
    assert all(".git" not in Path(name).parts for name in names)


def test_builder_packages_a_non_default_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh clone must build refs beyond the default branch.

    The 2026-09-08 M2/M3 proving run failed here: bare branch names trigger
    git's remote-branch DWIM checkout, which conflicts with ``--detach``.
    """
    _fixture_inventory(tmp_path)
    repository = tmp_path / "repository"
    _git("-C", str(repository), "checkout", "--quiet", "-b", "feature")
    (repository / "app.py").write_text("print('feature')\n", encoding="utf-8")
    _git("-C", str(repository), "commit", "--quiet", "-am", "feature change")
    _git("-C", str(repository), "checkout", "--quiet", "main")

    built = _build(tmp_path, "feature", capsys)

    assert built["git_ref"] == "feature"
    assert Path(built["archive"]).is_file()


def test_builder_rejects_an_unknown_component(tmp_path: Path) -> None:
    inventory = _fixture_inventory(tmp_path)

    with pytest.raises(ArtifactBuildError) as error:
        build_artifact(
            inventory,
            "billing-backend",
            "main",
            tmp_path / "artifacts",
            SCHEMAS,
            run=_git_never_runs,
        )

    assert error.value.code == "artifact_component_missing"


def test_builder_rejects_an_unsafe_git_ref(tmp_path: Path) -> None:
    inventory = _fixture_inventory(tmp_path)

    with pytest.raises(ArtifactBuildError) as error:
        build_artifact(
            inventory,
            "crm-backend",
            "--upload-pack=/bin/false",
            tmp_path / "artifacts",
            SCHEMAS,
            run=_git_never_runs,
        )

    assert error.value.code == "artifact_ref_invalid"
