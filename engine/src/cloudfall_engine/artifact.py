"""Build verifiable release artifacts from component repositories."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cloudfall.validation import (
    ConfigValidationError,
    SchemaCatalog,
    ValidationIssue,
)
from jsonschema.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cloudfall.domain import BuildTimeoutSeconds
    from cloudfall.inventory import (
        ComponentBuild,
        ComponentInventory,
        PlatformInventory,
    )
    from treaty import Completed

    Runner = Callable[[Sequence[str]], Completed]
    """Runs one argv (git, a tool's ``--version``) without raising on non-zero"""


class StepRunner(Protocol):
    """Runs one build step's argv in a directory, its output kept off stdout.

    It answers the completed run without raising on a non-zero exit, and
    raises ``BuildStepTimeoutError`` when the step outruns ``timeout``.
    """

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout: BuildTimeoutSeconds
    ) -> Completed:
        """Run ``argv`` in ``cwd`` for at most ``timeout``."""
        ...


_ARTIFACT_SCHEMA = "artifact.schema.json"
GIT_TIMEOUT_SECONDS = 600
GIT_MINIMUM_VERSION = "2.24.0"
"""The first git with ``--end-of-options``, which ref resolution passes"""
_SHORT_COMMIT_LENGTH = 7
GIT_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@^~-]{0,199}")
_TOOL_VERSION_PATTERN = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,7}")
BUILD_STEP_VARIABLES = ("PATH", "HOME", "LANG", "TMPDIR")
"""The only controller variables a build step sees, besides ``CI=true``"""
_ENV_TOOL = "env"
"""Starts each build step with ``env -i``, so it inherits nothing else"""
_STDERR_TAIL_CHARACTERS = 500
_ERROR_COMPONENT_MISSING = "artifact_component_missing"
_ERROR_SUBDIRECTORY_MISSING = "artifact_subdirectory_missing"
_ERROR_REF_INVALID = "artifact_ref_invalid"
_ERROR_GIT_MISSING = "artifact_git_missing"
_ERROR_GIT_FAILED = "artifact_git_failed"
_ERROR_TOOL_MISSING = "artifact_build_tool_missing"
_ERROR_TOOL_UNSUPPORTED = "artifact_build_tool_unsupported"
_ERROR_STEP_FAILED = "artifact_build_step_failed"
_ERROR_INCLUDE_MISSING = "artifact_include_missing"


class ArtifactBuildError(RuntimeError):
    """Fail-fast artifact build error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        """Record the failure code, human-readable detail, and its facts."""
        self.code = code
        self.detail = message
        self.context: dict[str, object] = dict(context or {})
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, object]:
        """Serialize the error envelope for system boundaries."""
        return {
            "status": "error",
            "error": {"code": self.code, "message": self.detail},
        }


class BuildStepTimeoutError(RuntimeError):
    """A build step ran past its own ``timeoutSeconds`` and was stopped."""


@dataclass(frozen=True, slots=True)
class _ResolvedTool:
    """A build tool found on the controller's ``PATH`` and the version it read."""

    name: str
    path: str
    version: str


@dataclass(frozen=True, slots=True)
class _BuildHost:
    """What build steps run with: the tools, ``env``, and the step variables."""

    tools: Mapping[str, _ResolvedTool]
    env_path: str
    variables: tuple[tuple[str, str], ...] = field(default=())

    def argv(self, command: Sequence[str]) -> tuple[str, ...]:
        """Start the step's tool through ``env -i`` with the step variables."""
        assignments = tuple(f"{name}={value}" for name, value in self.variables)
        tool = self.tools[command[0]].path
        return (self.env_path, "-i", *assignments, tool, *command[1:])


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


def build_artifact(  # noqa: PLR0913 - the build inputs plus its runners.
    inventory: PlatformInventory,
    component_id: str,
    git_ref: str,
    output_directory: Path,
    schema_directory: Path,
    *,
    run: Runner,
    run_step: StepRunner,
    environment: Mapping[str, str],
) -> BuiltArtifact:
    """Clone the component repository at a ref, build it, and package it.

    ``run`` starts git and each build tool's ``--version``; ``run_step``
    starts the build steps. The CLI passes ``ctx.run`` for both, so treaty
    owns their time limits and cancellation, and step output goes to stderr.
    ``environment`` is the controller's: build tools are looked up on its
    ``PATH``, and a step sees only its ``BUILD_STEP_VARIABLES`` and
    ``CI=true``. A component without ``build`` runs none of this.
    """
    component = _component(inventory, component_id)
    _validate_ref(git_ref)
    build = component.build
    host = None if build is None else _build_host(build, environment, run)
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
        tracked: tuple[str, ...] = ()
        if build is not None and host is not None:
            # The tracked files are read before any step can touch the index.
            tracked = _tracked_files(run, source_root)
            _run_steps(build, host, source_root, run_step)
            _require_includes(build, source_root)

        release = (
            f"{built_at.strftime('%Y%m%dT%H%M%SZ')}"
            f"-{commit[:_SHORT_COMMIT_LENGTH]}"
        )
        component_directory = output_directory / component_id
        component_directory.mkdir(parents=True, exist_ok=True)
        archive_path = component_directory / f"{release}.tar.gz"
        if build is None:
            _write_archive(source_root, archive_path)
        else:
            _write_built_archive(source_root, archive_path, tracked, build)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    size_bytes = archive_path.stat().st_size
    spec: dict[str, object] = {
        "component": component_id,
        "release": release,
        "gitRef": git_ref,
        "gitCommit": commit,
        "builtAt": built_at_text,
        "archive": archive_path.name,
        "archiveSha256": digest,
        "sizeBytes": size_bytes,
    }
    if build is not None and host is not None:
        spec["build"] = _build_record(build, host)
    metadata = {
        "apiVersion": "cloudfall/v1",
        "kind": "Artifact",
        "metadata": {
            "id": component_id,
            "description": f"Release artifact for {component_id}",
        },
        "spec": spec,
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


def _build_host(
    build: ComponentBuild, environment: Mapping[str, str], run: Runner
) -> _BuildHost:
    """Check every build tool before anything is cloned (S-002 step 1)."""
    search_path = environment.get("PATH", "")
    tools: dict[str, _ResolvedTool] = {}
    for tool in build.tools:
        name = tool.name.value
        path = _which(name, search_path)
        found = _tool_version(run, name, path)
        minimum = tool.minimum_version.value
        if _version_key(found) < _version_key(minimum):
            detail = (
                f"build tool {name} is {found}, older than the {minimum} "
                "spec.buildTools requires"
            )
            raise ArtifactBuildError(
                _ERROR_TOOL_UNSUPPORTED,
                detail,
                {"tool": name, "found": found, "minimum": minimum},
            )
        tools[name] = _ResolvedTool(name=name, path=path, version=found)
    variables = tuple(
        (name, environment[name])
        for name in BUILD_STEP_VARIABLES
        if name in environment
    )
    return _BuildHost(
        tools=tools,
        env_path=_which(_ENV_TOOL, search_path),
        variables=(*variables, ("CI", "true")),
    )


def _which(name: str, search_path: str) -> str:
    path = shutil.which(name, path=search_path)
    if path is None:
        detail = f"build tool {name} is not on the controller's PATH"
        raise ArtifactBuildError(_ERROR_TOOL_MISSING, detail, {"tool": name})
    if "=" in path:
        # ``env -i`` would read the path as a variable assignment.
        detail = f"build tool {name} resolves to a path with '=': {path}"
        raise ArtifactBuildError(_ERROR_TOOL_UNSUPPORTED, detail, {"tool": name})
    return path


def _tool_version(run: Runner, name: str, path: str) -> str:
    completed = run((path, "--version"))
    match = (
        _TOOL_VERSION_PATTERN.search(completed.stdout)
        or _TOOL_VERSION_PATTERN.search(completed.stderr)
        if completed.returncode == 0
        else None
    )
    if match is None:
        detail = (
            f"build tool {name} printed no version for --version "
            f"(exit code {completed.returncode})"
        )
        raise ArtifactBuildError(
            _ERROR_TOOL_UNSUPPORTED, detail, {"tool": name, "found": None}
        )
    return match.group(0)


def _version_key(version: str) -> tuple[int, ...]:
    parts = [int(part) for part in version.split(".")]
    return tuple(parts + [0] * (8 - len(parts)))


def _tracked_files(run: Runner, source_root: Path) -> tuple[str, ...]:
    listed = _run_git(run, "-C", str(source_root), "ls-files", "-z")
    return tuple(path for path in listed.split("\0") if path)


def _run_steps(
    build: ComponentBuild,
    host: _BuildHost,
    source_root: Path,
    run_step: StepRunner,
) -> None:
    for index, step in enumerate(build.steps):
        command = step.command.value
        context: dict[str, object] = {
            "step": index,
            "workingDirectory": step.working_directory.value,
            "argv": list(command),
        }
        label = f"build step {index} ({shlex.join(command)})"
        directory = source_root / step.working_directory.value
        if not directory.is_dir():
            detail = (
                f"{label}: working directory "
                f"{step.working_directory.value} does not exist"
            )
            raise ArtifactBuildError(
                _ERROR_STEP_FAILED, detail, {**context, "exitCode": None}
            )
        try:
            completed = run_step(
                host.argv(command), cwd=directory, timeout=step.timeout_seconds
            )
        except BuildStepTimeoutError as error:
            detail = (
                f"{label} ran past its {step.timeout_seconds.value}s timeout "
                "and was stopped"
            )
            raise ArtifactBuildError(
                _ERROR_STEP_FAILED,
                detail,
                {
                    **context,
                    "exitCode": None,
                    "timeoutSeconds": step.timeout_seconds.value,
                },
            ) from error
        if completed.returncode != 0:
            tail = completed.stderr.strip()[-_STDERR_TAIL_CHARACTERS:]
            detail = f"{label} exited with code {completed.returncode}"
            if tail:
                detail = f"{detail}: {tail}"
            raise ArtifactBuildError(
                _ERROR_STEP_FAILED,
                detail,
                {**context, "exitCode": completed.returncode},
            )


def _require_includes(build: ComponentBuild, source_root: Path) -> None:
    for include in build.include:
        if not os.path.lexists(source_root / include.value):
            detail = (
                f"artifact.include path {include.value} does not exist "
                "after the build steps"
            )
            raise ArtifactBuildError(
                _ERROR_INCLUDE_MISSING, detail, {"path": include.value}
            )


def _write_built_archive(
    source_root: Path,
    archive_path: Path,
    tracked: Sequence[str],
    build: ComponentBuild,
) -> None:
    """Package the tracked files plus every ``artifact.include`` path.

    Whatever else the steps left behind (``node_modules/``, caches) stays out.
    A tracked file a step deleted is not packaged.
    """
    entries: set[Path] = set()
    for name in tracked:
        relative = Path(name)
        if os.path.lexists(source_root / relative):
            entries.add(relative)
    for include in build.include:
        entries.update(_include_entries(source_root, Path(include.value)))
    for entry in tuple(entries):
        entries.update(entry.parents)
    entries.discard(Path())
    ordered = sorted(
        (entry for entry in entries if ".git" not in entry.parts),
        key=lambda entry: entry.parts,
    )
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source_root, arcname=".", recursive=False, filter=_owned_by_root)
        for entry in ordered:
            archive.add(
                source_root / entry,
                arcname=f"./{entry.as_posix()}",
                recursive=False,
                filter=_owned_by_root,
            )


def _include_entries(source_root: Path, include: Path) -> set[Path]:
    top = source_root / include
    if top.is_symlink() or not top.is_dir():
        return {include}
    entries = {include}
    for directory, directories, files in os.walk(top):
        base = Path(directory).relative_to(source_root)
        entries.update(base / name for name in (*directories, *files))
    return entries


def _owned_by_root(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    return info


def _build_record(build: ComponentBuild, host: _BuildHost) -> dict[str, object]:
    """``spec.build`` of the metadata: the steps as run and the tool versions."""
    return {
        "steps": [
            {
                "workingDirectory": step.working_directory.value,
                "command": list(step.command.value),
            }
            for step in build.steps
        ],
        "tools": {name: tool.version for name, tool in host.tools.items()},
    }
