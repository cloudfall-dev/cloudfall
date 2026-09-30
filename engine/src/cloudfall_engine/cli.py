"""Command-line boundary for the Cloudfall execution engine.

Built on treaty: every run answers one JSON envelope on stdout, a failure
carries a declared exit code, and ``cloudfall-engine manifest`` describes
each command. Ansible's play log streams to stderr.
"""

import json
import os
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Self

from cloudfall.domain import ResourceId
from cloudfall.inventory import PlatformInventory
from cloudfall.project import (
    PROJECT_DIRECTORY_VARIABLE,
    ProjectError,
    resolve_project_directory,
)
from cloudfall.resources import default_engine_directory, default_schema_directory
from cloudfall.validation import ConfigValidationError, validate_config
from treaty import App, Arg, Ctx, Exit, Flag, Out, ParseError

from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.artifact import (
    GIT_REF_PATTERN,
    GIT_TIMEOUT_SECONDS,
    ArtifactBuildError,
    build_artifact,
)
from cloudfall_engine.playbook import (
    PlaybookError,
    PlaybookRun,
    bundled_playbooks,
    execute_playbook,
    resolve_playbook,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

app = App(
    "cloudfall-engine",
    version=version("cloudfall"),
    description="Run validated Cloudfall operations with Ansible",
)
app.scalar(
    ResourceId,
    parse=ResourceId.from_boundary,
    pattern=r"[a-z][a-z0-9]*(-[a-z0-9]+)*",
)
app.exit_code(
    "PROJECT_INVALID",
    79,
    description="No project directory could be resolved",
    retryable=False,
    side_effects="none",
    suggestion=(
        f"pass --project, set {PROJECT_DIRECTORY_VARIABLE}, "
        "or run inside a project"
    ),
)
app.exit_code(
    "CONFIG_INVALID",
    80,
    description="The project's resources failed validation",
    retryable=False,
    side_effects="none",
    suggestion="fix the resource error.context names, then run the command again",
)
app.exit_code(
    "ARTIFACT_BUILD_FAILED",
    81,
    description="The component could not be cloned or packaged",
    retryable=False,
    side_effects="none",
)
app.exit_code(
    "PLAYBOOK_FAILED",
    82,
    description="ansible-playbook exited non-zero; hosts may be partly converged",
    retryable=False,
    side_effects="partial",
    suggestion="read the Ansible log on stderr, fix the failing task, and run again",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectArgs:
    """Options every command that reads a project shares."""

    project: Path | None = Flag(
        default=None,
        description=(
            f"Project directory (default: ${PROJECT_DIRECTORY_VARIABLE}, "
            "else the current directory when it is a project)"
        ),
    )
    schemas: Path = Flag(
        default=default_schema_directory(),
        description="Versioned schema directory (default: bundled schemas)",
    )


@dataclass(frozen=True, slots=True)
class Project:
    """The resolved project and the inventory its resources validate to."""

    directory: Path
    inventory: PlatformInventory

    @classmethod
    def acquire(cls, args: ProjectArgs, _ctx: Ctx) -> Self:
        """Resolve and validate the project before the handler runs."""
        try:
            directory = resolve_project_directory(
                args.project, os.environ, Path.cwd()
            )
        except ProjectError as error:
            raise Exit.PROJECT_INVALID(
                error.detail, context={"code": error.code}
            ) from error
        try:
            state = validate_config(directory, args.schemas)
        except ConfigValidationError as error:
            raise _config_invalid(error) from error
        return cls(directory, PlatformInventory.from_state(state))

    def path(self, value: Path) -> Path:
        """Resolve a relative path inside the project, whatever the cwd."""
        return value if value.is_absolute() else self.directory / value


def _config_invalid(error: ConfigValidationError) -> Exception:
    return Exit.CONFIG_INVALID(error.issue.message, context=error.issue.as_dict())


inventory = app.group("inventory", description="Generate execution inventory")


@dataclass(frozen=True, slots=True)
class RenderArgs(ProjectArgs):
    """Arguments of ``inventory render``."""

    output: Path | None = Flag(
        default=None,
        description=(
            "Also write the inventory JSON to this file (relative to the project)"
        ),
    )


@dataclass(frozen=True, slots=True)
class RenderedInventory:
    """The rendered inventory and where it was written."""

    inventory: dict[str, object] = Out(ordered=True, high_entropy=False)
    """The Ansible JSON inventory, exactly as Ansible reads it"""
    output: Path | None = None
    """Where it was written, or null without --output"""


@inventory.command(
    "render",
    description="Render the validated config as Ansible JSON inventory",
    danger_level="safe",
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID"],
    examples=[
        (
            "Write the inventory Ansible reads",
            "cloudfall-engine inventory render --output tmp/ansible-inventory.json",
        ),
    ],
)
def render(args: RenderArgs, _ctx: Ctx, project: Project) -> RenderedInventory:
    """Render the inventory, and write it when ``--output`` names a file."""
    rendered = render_ansible_inventory(project.inventory)
    if args.output is None:
        return RenderedInventory(rendered)
    output = project.path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{json.dumps(rendered, sort_keys=True)}\n", encoding="utf-8")
    return RenderedInventory(rendered, output)


artifact = app.group("artifact", description="Build release artifacts")


@dataclass(frozen=True, slots=True)
class BuildArgs(ProjectArgs):
    """Arguments of ``artifact build``."""

    component: ResourceId = Arg(description="Component to package")
    ref: str = Flag(
        description="Git ref (branch, tag, or commit) to package",
        pattern=GIT_REF_PATTERN.pattern,
    )
    output_dir: Path = Flag(
        default=Path("tmp/artifacts"),
        description="Artifact output directory, relative to the project",
    )


@dataclass(frozen=True, slots=True)
class BuiltRelease:
    """One packaged and hashed release."""

    effect: str
    component: str
    release: str
    git_ref: str
    git_commit: str
    archive: Path
    metadata: Path
    archive_sha256: str
    size_bytes: int
    built_at: str = Out(volatile=True)


@artifact.command(
    "build",
    description="Clone, package, and hash one component release",
    danger_level="mutating",
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID", "ARTIFACT_BUILD_FAILED"],
    timeout=3 * GIT_TIMEOUT_SECONDS,
    supports_raw_payload=True,
    examples=[
        (
            "Package the main branch of one component",
            "cloudfall-engine artifact build crm-backend --ref main",
        ),
    ],
)
def build(args: BuildArgs, _ctx: Ctx, project: Project) -> BuiltRelease:
    """Clone the component at ``--ref`` and package it under the project."""
    try:
        built = build_artifact(
            project.inventory,
            str(args.component),
            args.ref,
            project.path(args.output_dir),
            args.schemas,
        )
    except ArtifactBuildError as error:
        raise Exit.ARTIFACT_BUILD_FAILED(
            error.detail, context={"code": error.code}
        ) from error
    except ConfigValidationError as error:
        raise _config_invalid(error) from error
    return BuiltRelease(
        effect="created",
        component=built.component_id,
        release=built.release,
        git_ref=built.git_ref,
        git_commit=built.git_commit,
        archive=built.archive_path,
        metadata=built.metadata_path,
        archive_sha256=built.archive_sha256,
        size_bytes=built.size_bytes,
        built_at=built.built_at,
    )


playbook = app.group(
    "playbook", description="Run Ansible playbooks with the engine configuration"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineArgs:
    """Options every playbook command shares."""

    engine: Path = Flag(
        default=default_engine_directory(),
        description=(
            "Engine directory containing the Ansible contracts "
            "(default: bundled engine)"
        ),
    )


@dataclass(frozen=True, slots=True)
class ListArgs(EngineArgs):
    """Arguments of ``playbook list``."""


@dataclass(frozen=True, slots=True)
class Playbooks:
    """The playbooks an engine directory bundles."""

    engine: Path
    playbooks: list[str] = Out(ordered=True)


@playbook.command(
    "list",
    description="List the playbooks bundled with the engine",
    danger_level="safe",
    exit_codes=["NOT_FOUND"],
    examples=[("List the bundled playbooks", "cloudfall-engine playbook list")],
)
def list_playbooks(args: ListArgs, _ctx: Ctx) -> Playbooks:
    """List the bundled playbooks by name."""
    try:
        names = bundled_playbooks(args.engine)
    except PlaybookError as error:
        raise Exit.NOT_FOUND(error.detail, context={"code": error.code}) from error
    return Playbooks(args.engine, list(names))


@dataclass(frozen=True, slots=True)
class RunArgs(EngineArgs):
    """Arguments of ``playbook run``, checked before anything runs."""

    playbook: str = Arg(
        description="Bundled playbook name (see playbook list) or a playbook path"
    )
    inventory: Path = Flag(
        default=Path("tmp/ansible-inventory.json"),
        description="Rendered inventory path",
    )
    roles: tuple[Path, ...] = Flag(
        default=(),
        description="Role directory searched before the bundled roles (repeatable)",
    )
    extra_vars: tuple[str, ...] = Flag(
        default=(),
        description="Passed through to ansible-playbook unchanged (repeatable)",
    )
    tags: tuple[str, ...] = Flag(
        default=(),
        description="Only run plays and tasks tagged with this value (repeatable)",
    )
    limit: str | None = Flag(
        default=None, description="Restrict the run to a host pattern"
    )
    dry_run: bool = Flag(
        default=False,
        description="Run in Ansible check mode: report changes, make none",
    )
    diff: bool = Flag(default=False, description="Show file diffs")
    syntax_check: bool = Flag(
        default=False, description="Only check the playbook syntax"
    )

    def __post_init__(self) -> None:
        """Report a missing playbook, inventory, or role directory as input."""
        _playbook_run(self)


def _playbook_run(args: RunArgs) -> PlaybookRun:
    try:
        return PlaybookRun(
            playbook=resolve_playbook(args.playbook, args.engine),
            inventory_file=args.inventory,
            role_directories=args.roles,
            extra_vars=args.extra_vars,
            tags=args.tags,
            limit=args.limit,
            check=args.dry_run,
            diff=args.diff,
            syntax_check=args.syntax_check,
        )
    except PlaybookError as error:
        raise ParseError(error.detail, context={"code": error.code}) from error


@dataclass(frozen=True, slots=True)
class Played:
    """A playbook run that ansible-playbook finished with exit 0."""

    effect: str
    playbook: Path
    inventory: Path


@playbook.command(
    "run",
    description=(
        "Run one bundled or project playbook against the inventory; "
        "the Ansible log streams to stderr"
    ),
    danger_level="mutating",
    exit_codes=["PRECONDITION", "PLAYBOOK_FAILED"],
    timeout=None,
    supports_raw_payload=True,
    examples=[
        (
            "Collect read-only server snapshots",
            "cloudfall-engine playbook run inspect "
            "--inventory tmp/ansible-inventory.json",
        ),
        (
            "Preview a converge without changing hosts",
            "cloudfall-engine playbook run time --dry-run --diff",
        ),
    ],
)
def run_playbook(args: RunArgs, _ctx: Ctx) -> Played:
    """Run ansible-playbook in the foreground and report how it ended."""
    run = _playbook_run(args)
    try:
        returncode = execute_playbook(run, args.engine)
    except PlaybookError as error:
        raise Exit.PRECONDITION(
            error.detail, context={"code": error.code}
        ) from error
    if returncode != 0:
        message = f"the playbook failed: ansible-playbook exited {returncode}"
        raise Exit.PLAYBOOK_FAILED(
            message,
            context={"returncode": returncode, "playbook": str(run.playbook)},
        )
    return Played(_effect(args), run.playbook, run.inventory_file)


def _effect(args: RunArgs) -> str:
    if args.syntax_check:
        return "noop"
    # Ansible reports what changed only in its log, so a live run claims the
    # converge rather than counting changed tasks.
    return "would_update" if args.dry_run else "updated"


def main(argv: Sequence[str]) -> int:
    """Run one engine command in-process and return its exit code."""
    return app.run(argv)


def run() -> None:
    """Installed engine console-script entry point."""
    app.main()
