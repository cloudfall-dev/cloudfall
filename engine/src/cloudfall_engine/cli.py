"""Command-line boundary for the Cloudfall execution engine.

Built on treaty: every run answers one JSON envelope on stdout, a failure
carries a declared exit code, and ``cloudfall-engine manifest`` describes
each command. Ansible and git run through ``ctx.run``; Ansible's play log
streams to stderr as plain text.
"""

import dataclasses
import json
import os
import shutil
import time
from dataclasses import dataclass
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

from cloudfall.ansible_data import as_ansible_data, unmarked
from cloudfall.commands import snake_case_keys
from cloudfall.domain import (
    ARTIFACT_CLONE_TIMEOUT_SECONDS,
    AbsolutePath,
    PostgresDatabaseName,
    PostgresRoleName,
    ResourceId,
    SecretKeyName,
)
from cloudfall.inventory import (
    OwnedPostgresDatabase,
    PlatformInventory,
    SecretValueReference,
)
from cloudfall.project import (
    PROJECT_DIRECTORY_VARIABLE,
    ProjectError,
    resolve_project_directory,
)
from cloudfall.resources import default_engine_directory, default_schema_directory
from cloudfall.secrets import (
    ERROR_DECRYPT_FAILED,
    ERROR_SOPS_MISSING,
    SecretsError,
    SopsSecretProvider,
    owned_role_passwords,
    staged_password_files,
)
from cloudfall.validation import ConfigValidationError, validate_config
from treaty import (
    App,
    Arg,
    CliExit,
    Ctx,
    Exit,
    ExitCodeName,
    External,
    Flag,
    Out,
    ParseError,
    Subprocess,
    Timeout,
)

from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.artifact import (
    GIT_MINIMUM_VERSION,
    GIT_REF_PATTERN,
    GIT_TIMEOUT_SECONDS,
    ArtifactBuildError,
    BuildStepTimeoutError,
    build_artifact,
)
from cloudfall_engine.playbook import (
    ANSIBLE_MINIMUM_VERSION,
    PlaybookError,
    PlaybookRun,
    bundled_playbooks,
    playbook_command,
    resolve_playbook,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cloudfall.domain import BuildTimeoutSeconds
    from cloudfall.secrets import OwnedRolePassword
    from treaty import Completed

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
    suggestion=(
        "read the Ansible log on stderr, or its end in error.context.output; "
        "fix the failing task, and run again"
    ),
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
    """Render the inventory, and write it when ``--output`` names a file.

    Every declared string is marked unsafe, so Ansible never templates it.
    """
    rendered = cast(
        "dict[str, object]",
        as_ansible_data(render_ansible_inventory(project.inventory)),
    )
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
    # Its own field, in place of treaty's --timeout (REQ-C-012), so the
    # handler can tell "not passed" (the computed limit) apart from 0.
    timeout: float | None = Flag(
        default=None,
        description=(
            "Seconds before the build aborts with TIMEOUT; 0 disables the limit "
            "(default: 1800 for the clone plus every declared step's "
            "timeoutSeconds)"
        ),
    )

    def __post_init__(self) -> None:
        """Refuse a ``--timeout`` treaty's own flag would refuse."""
        self.limit()

    def limit(self) -> Timeout | None:
        """Return the explicit ``--timeout``, ``None`` when not passed.

        ``Timeout(None)`` for 0, which disables the limit.
        """
        return None if self.timeout is None else Timeout.parse(self.timeout)


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
    description=(
        "Clone, build (the component's declared steps), package, and hash "
        "one component release"
    ),
    danger_level="mutating",
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID", "ARTIFACT_BUILD_FAILED"],
    # The limit depends on the component: BuildBudget applies the clone time
    # plus every declared step's timeout, or BuildArgs.timeout when passed.
    timeout=None,
    subprocess=Subprocess("git"),
    required_tools={"git": GIT_MINIMUM_VERSION},
    supports_raw_payload=True,
    child_log=True,
    examples=[
        (
            "Package the main branch of one component",
            "cloudfall-engine artifact build crm-backend --ref main",
        ),
    ],
)
def build(args: BuildArgs, ctx: Ctx, project: Project) -> BuiltRelease:
    """Clone the component at ``--ref``, build it, and package it.

    Build step output streams to stderr, never into the envelope on stdout.
    The run may take the clone time plus every declared step's timeout;
    an explicit ``--timeout`` replaces that limit.
    """
    budget = BuildBudget.start(
        args.limit(), _artifact_timeout(project, args.component)
    )
    try:
        built = build_artifact(
            project.inventory,
            str(args.component),
            args.ref,
            project.path(args.output_dir),
            args.schemas,
            run=partial(_run_git_or_tool, ctx, budget),
            run_step=partial(_run_build_step, ctx, budget),
            environment=dict(os.environ),
        )
    except ArtifactBuildError as error:
        raise Exit.ARTIFACT_BUILD_FAILED(
            error.detail, context={"code": error.code, **error.context}
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


def _artifact_timeout(project: Project, component_id: ResourceId) -> int:
    component = next(
        (
            candidate
            for candidate in project.inventory.components
            if candidate.resource_id == component_id
        ),
        None,
    )
    # An unknown component fails in build_artifact, before any child runs.
    if component is None:
        return ARTIFACT_CLONE_TIMEOUT_SECONDS
    return component.artifact_timeout_seconds


@dataclass(frozen=True, slots=True)
class BuildBudget:
    """The time limit of one ``artifact build`` run, shared by its children.

    ``seconds`` is ``None`` and ``deadline`` too when ``--timeout 0``
    disabled the limit; each child then keeps only its own.
    """

    seconds: float | None
    deadline: float | None

    @classmethod
    def start(cls, explicit: Timeout | None, computed: int) -> Self:
        """Start the clock: ``--timeout`` when passed, else ``computed``."""
        seconds = float(computed) if explicit is None else explicit.seconds
        if seconds is None:
            return cls(None, None)
        return cls(seconds, time.monotonic() + seconds)

    def timeout(self, seconds: int) -> Timeout:
        """Cut a child's own limit to what is left of the run's."""
        if self.deadline is None or self.seconds is None:
            return Timeout(seconds)
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CliExit(
                ExitCodeName("TIMEOUT"),
                f"artifact build used its {self.seconds:g}s limit; pass a larger "
                "--timeout, or 0 for none",
                context={"timeout_ms": int(self.seconds * 1000)},
            )
        return Timeout(min(float(seconds), left))


def _run_git_or_tool(
    ctx: Ctx, budget: BuildBudget, argv: Sequence[str]
) -> Completed:
    return ctx.run(argv, timeout=budget.timeout(GIT_TIMEOUT_SECONDS), check=False)


def _run_build_step(
    ctx: Ctx,
    budget: BuildBudget,
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: BuildTimeoutSeconds,
) -> Completed:
    """Run one build step, its output streamed to stderr as plain text."""
    try:
        return ctx.run(
            argv,
            cwd=cwd,
            timeout=budget.timeout(timeout.value),
            check=False,
            stream="always",
        )
    except CliExit as error:
        # Only the step's own limit is a step failure; running out of the
        # command's time stays treaty's TIMEOUT.
        own_limit = error.context.get("timeout_ms") == timeout.value * 1000
        if error.name == ExitCodeName("TIMEOUT") and own_limit:
            raise BuildStepTimeoutError(error.message) from error
        raise


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
    secrets_dir: Path = Flag(
        default=Path("secrets"),
        description=(
            "sops-encrypted secrets directory services.yml reads owner role "
            "passwords from (default: secrets)"
        ),
        secret=False,
    )

    def __post_init__(self) -> None:
        """Report a missing playbook, inventory, or role directory as input."""
        _playbook_run(self)


def _playbook_run(
    args: RunArgs, staged_vars: tuple[str, ...] = ()
) -> PlaybookRun:
    try:
        return PlaybookRun(
            playbook=resolve_playbook(args.playbook, args.engine),
            inventory_file=args.inventory,
            role_directories=args.roles,
            extra_vars=(*args.extra_vars, *staged_vars),
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
    owned_roles: list[dict[str, object]] = Out(
        default_factory=list, ordered=True, high_entropy=False
    )
    """Each owner role password services.yml set: its passwordRef, its key,
    and the value's SHA-256, never the value"""


SERVICES_PLAYBOOK = "services.yml"


def _sops_decrypt(ctx: Ctx) -> Callable[[Path], str]:
    """Decrypt one sops file through ``ctx.run``."""

    def decrypt(source: Path) -> str:
        binary = shutil.which("sops")
        if binary is None:
            message = (
                "the sops binary is not installed; install sops and age, and "
                "set SOPS_AGE_KEY_FILE to your age key"
            )
            raise SecretsError(ERROR_SOPS_MISSING, message)
        done = ctx.run([binary, "--decrypt", str(source)], check=False)
        if done.returncode != 0:
            message = f"sops could not decrypt {source}: {done.stderr.strip()[:300]}"
            raise SecretsError(ERROR_DECRYPT_FAILED, message)
        return done.stdout

    return decrypt


def _rendered_owned_databases(
    inventory_file: Path,
) -> tuple[tuple[ResourceId, OwnedPostgresDatabase], ...]:
    """Read every owned database, with its service, from a rendered inventory.

    An inventory that isn't a rendered Cloudfall one (YAML, INI, a directory,
    or JSON of another shape) declares none, so the run stays as it was.
    Only an entry that names a ``passwordRef`` must be well formed.
    """
    if not inventory_file.is_file():
        return ()
    try:
        # The rendered strings are marked unsafe for Ansible (#174).
        document = cast(
            "object",
            json.loads(
                inventory_file.read_text(encoding="utf-8"), object_hook=unmarked
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    hosts = _nested(document, "all", "children", "cloudfall_servers", "hosts")
    owned: list[tuple[ResourceId, OwnedPostgresDatabase]] = []
    for host in hosts.values():
        for service in _mappings(_nested(host).get("cloudfall_services")):
            if service.get("serviceKind") != "postgresql":
                continue
            databases = _nested(service, "postgresql").get("databases")
            owned.extend(
                (ResourceId.from_boundary(service.get("id")), _owned_database(database))
                for database in _mappings(databases)
                if "passwordRef" in database
            )
    return tuple(owned)


def _nested(value: object, *keys: str) -> Mapping[str, object]:
    """Follow ``keys`` through mappings; anything else reads as empty."""
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return cast("Mapping[str, object]", current) if isinstance(current, dict) else {}


def _mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        cast("Mapping[str, object]", item) for item in value if isinstance(item, dict)
    ]


def _owned_database(database: Mapping[str, object]) -> OwnedPostgresDatabase:
    reference = database["passwordRef"]
    if not isinstance(reference, dict):
        message = "an owned database's passwordRef is not a mapping"
        raise TypeError(message)
    return OwnedPostgresDatabase(
        name=PostgresDatabaseName.from_boundary(database.get("name")),
        owner=PostgresRoleName.from_boundary(database.get("owner")),
        password_ref=SecretValueReference(
            environment=ResourceId.from_boundary(reference.get("environment")),
            path=AbsolutePath.from_boundary(reference.get("path")),
            key=SecretKeyName.from_boundary(reference.get("key")),
        ),
    )


def _services_passwords(
    args: RunArgs, run: PlaybookRun, ctx: Ctx
) -> tuple[OwnedRolePassword, ...]:
    """Decrypt the owner passwords the bundled services.yml sets, before it starts."""
    if args.syntax_check or run.playbook != resolve_playbook(
        SERVICES_PLAYBOOK, args.engine
    ):
        return ()
    try:
        databases = _rendered_owned_databases(args.inventory)
    except (TypeError, ValueError) as error:
        message = f"the inventory declares a malformed owned database: {error}"
        raise ParseError(message, context={"flag": "inventory"}) from error
    if not databases:
        return ()
    provider = SopsSecretProvider(
        secrets_directory=args.secrets_dir, decrypt=_sops_decrypt(ctx)
    )
    try:
        return owned_role_passwords(databases, provider)
    except SecretsError as error:
        raise Exit.PRECONDITION(error.message, context={"code": error.code}) from error


@playbook.command(
    "run",
    description=(
        "Run one bundled or project playbook against the inventory; "
        "the Ansible log streams to stderr"
    ),
    danger_level="mutating",
    exit_codes=["PRECONDITION", "PLAYBOOK_FAILED"],
    timeout=None,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": ANSIBLE_MINIMUM_VERSION},
    child_log=True,
    external=False,
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
def run_playbook(args: RunArgs, ctx: Ctx) -> Played:
    """Run ansible-playbook in the foreground and report how it ended.

    The bundled services.yml gets each owner role password staged in a 0600
    file outside the project, removed when the run ends; only the file paths
    cross the command line.
    """
    passwords = _services_passwords(args, _playbook_run(args), ctx)
    with staged_password_files(passwords) as staged:
        played = _run_ansible(
            args, ctx, _playbook_run(args, (json.dumps(staged),) if staged else ())
        )
    return dataclasses.replace(
        played,
        owned_roles=[
            cast("dict[str, object]", snake_case_keys(password.as_dict()))
            for password in passwords
        ],
    )


def _run_ansible(args: RunArgs, ctx: Ctx, run: PlaybookRun) -> Played:
    try:
        command = playbook_command(run, args.engine)
    except PlaybookError as error:
        raise Exit.PRECONDITION(
            error.detail, context={"code": error.code}
        ) from error
    # Stdout belongs to the envelope, so treaty streams Ansible's play log to
    # stderr as plain text, redacted, terminal or not (a timer's journal or a
    # CI log keeps it), and keeps its tail for a failure.
    completed = ctx.run(
        command.argv, env=command.environment, check=False, stream="always"
    )
    if completed.returncode != 0:
        returncode = completed.returncode
        message = f"the playbook failed: ansible-playbook exited {returncode}"
        raise Exit.PLAYBOOK_FAILED(
            message,
            context={
                "returncode": returncode,
                "playbook": str(run.playbook),
                # The hosts wrote this text: the agent gets it marked untrusted.
                "output": External(completed.stdout),
            },
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
