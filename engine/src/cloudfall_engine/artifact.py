"""Build verifiable release artifacts from component repositories."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.validation import (
    ConfigValidationError,
    SchemaCatalog,
    ValidationIssue,
)
from jsonschema.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cloudfall.inventory import ComponentInventory, PlatformInventory
    from treaty import Completed

    Runner = Callable[[Sequence[str]], Completed]
    """Runs one git argv to completion without raising on a non-zero exit"""

_ARTIFACT_SCHEMA = "artifact.schema.json"
GIT_TIMEOUT_SECONDS = 600
GIT_MINIMUM_VERSION = "2.24.0"
"""The first git with ``--end-of-options``, which ref resolution passes"""
_SHORT_COMMIT_LENGTH = 7
GIT_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@^~-]{0,199}")
_ERROR_COMPONENT_MISSING = "artifact_component_missing"
_ERROR_SUBDIRECTORY_MISSING = "artifact_subdirectory_missing"
_ERROR_REF_INVALID = "artifact_ref_invalid"
_ERROR_GIT_MISSING = "artifact_git_missing"
_ERROR_GIT_FAILED = "artifact_git_failed"


class ArtifactBuildError(RuntimeError):
    """Fail-fast artifact build error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        """Record the failure code and human-readable detail."""
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, object]:
        """Serialize the error envelope for system boundaries."""
        return {
            "status": "error",
            "error": {"code": self.code, "message": self.detail},
        }


@dataclass(frozen=True, slots=True)
class BuiltArtifact:
    """One built and hashed release artifact."""

    component_id: str
    release: str
    git_ref: str
    git_commit: str
    built_at: str
    archive_path: Path
    metadata_path: Path
    archive_sha256: str
    size_bytes: int

    def as_dict(self) -> dict[str, object]:
        """Serialize the build result for CLI consumers."""
        return {
            "status": "ok",
            "component": self.component_id,
            "release": self.release,
            "gitRef": self.git_ref,
            "gitCommit": self.git_commit,
            "builtAt": self.built_at,
            "archive": str(self.archive_path),
            "metadata": str(self.metadata_path),
            "archiveSha256": self.archive_sha256,
            "sizeBytes": self.size_bytes,
        }


def build_artifact(  # noqa: PLR0913 - the build inputs plus its git runner.
    inventory: PlatformInventory,
    component_id: str,
    git_ref: str,
    output_directory: Path,
    schema_directory: Path,
    *,
    run: Runner,
) -> BuiltArtifact:
    """Clone the component repository at a ref and package a release.

    ``run`` starts git: the CLI passes ``ctx.run``, so treaty owns its time
    limit, locale, and cancellation.
    """
    component = _component(inventory, component_id)
    _validate_ref(git_ref)
    built_at = datetime.now(tz=UTC)
    built_at_text = built_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    with tempfile.TemporaryDirectory(prefix="cloudfall-artifact-") as workdir:
        checkout = Path(workdir) / "source"
        _run_git(
            run, "clone", "--quiet", component.repository.url.value, str(checkout)
        )
        # A fresh clone has local branches only for the default branch, and
        # bare branch names trigger git's remote-branch DWIM, which conflicts
        # with --detach. Resolving the ref to a commit first supports
        # branches, tags, and commits uniformly.
        commit = _resolve_ref_commit(run, checkout, git_ref)
        _run_git(
            run,
            "-C",
            str(checkout),
            "checkout",
            "--quiet",
            "--detach",
            commit,
            "--",
        )
        source_root = checkout
        if component.repository.subdirectory is not None:
            source_root = checkout / component.repository.subdirectory
            if not source_root.is_dir():
                detail = (
                    "declared repository subdirectory does not exist: "
                    f"{component.repository.subdirectory}"
                )
                raise ArtifactBuildError(
                    _ERROR_SUBDIRECTORY_MISSING, detail
                )

        release = (
            f"{built_at.strftime('%Y%m%dT%H%M%SZ')}"
            f"-{commit[:_SHORT_COMMIT_LENGTH]}"
        )
        component_directory = output_directory / component_id
        component_directory.mkdir(parents=True, exist_ok=True)
        archive_path = component_directory / f"{release}.tar.gz"
        _write_archive(source_root, archive_path)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    size_bytes = archive_path.stat().st_size
    metadata = {
        "apiVersion": "cloudfall/v1",
        "kind": "Artifact",
        "metadata": {
            "id": component_id,
            "description": f"Release artifact for {component_id}",
        },
        "spec": {
            "component": component_id,
            "release": release,
            "gitRef": git_ref,
            "gitCommit": commit,
            "builtAt": built_at_text,
            "archive": archive_path.name,
            "archiveSha256": digest,
            "sizeBytes": size_bytes,
        },
    }
    catalog = SchemaCatalog(schema_directory)
    try:
        catalog.validate_named(_ARTIFACT_SCHEMA, metadata)
    except ValidationError as error:
        issue = ValidationIssue(
            code="artifact_metadata_invalid",
            message=error.message,
        )
        raise ConfigValidationError(issue) from error
    metadata_path = archive_path.with_suffix("").with_suffix(".json")
    metadata_path.write_text(
        f"{json.dumps(metadata, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    return BuiltArtifact(
        component_id=component_id,
        release=release,
        git_ref=git_ref,
        git_commit=commit,
        built_at=built_at_text,
        archive_path=archive_path,
        metadata_path=metadata_path,
        archive_sha256=digest,
        size_bytes=size_bytes,
    )


def _component(
    inventory: PlatformInventory, component_id: str
) -> ComponentInventory:
    component = next(
        (
            candidate
            for candidate in inventory.components
            if candidate.resource_id.value == component_id
        ),
        None,
    )
    if component is None:
        detail = f"component does not exist: {component_id}"
        raise ArtifactBuildError(_ERROR_COMPONENT_MISSING, detail)
    return component


def _validate_ref(git_ref: str) -> None:
    if not GIT_REF_PATTERN.fullmatch(git_ref):
        detail = f"git ref contains unsupported characters: {git_ref!r}"
        raise ArtifactBuildError(_ERROR_REF_INVALID, detail)


def _resolve_ref_commit(run: Runner, checkout: Path, git_ref: str) -> str:
    candidates = (git_ref, f"origin/{git_ref}")
    for candidate in candidates:
        try:
            return _run_git(
                run,
                "-C",
                str(checkout),
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{candidate}^{{commit}}",
            ).strip()
        except ArtifactBuildError as error:
            if error.code != _ERROR_GIT_FAILED:
                raise
    detail = f"git ref does not resolve to a commit: {git_ref!r}"
    raise ArtifactBuildError(_ERROR_GIT_FAILED, detail)


def _run_git(run: Runner, *arguments: str) -> str:
    binary = shutil.which("git")
    if binary is None:
        detail = "git is not installed on the build host"
        raise ArtifactBuildError(_ERROR_GIT_MISSING, detail)
    completed = run((binary, *arguments))
    if completed.returncode != 0:
        detail = (
            f"git {' '.join(arguments[:2])} failed: {completed.stderr.strip()}"
        )
        raise ArtifactBuildError(_ERROR_GIT_FAILED, detail)
    return completed.stdout


def _write_archive(source_root: Path, archive_path: Path) -> None:
    def _skip_git(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if ".git" in Path(info.name).parts:
            return None
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        return info

    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source_root, arcname=".", filter=_skip_git)
